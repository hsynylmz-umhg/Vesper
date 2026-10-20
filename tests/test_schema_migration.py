"""
Şema v2 göçü (v0.5.0 Checkpoint A) testleri — GERÇEK sqlite3, sahte SQLite YOK.

ÖNEMLİ GÜVENLİK KURALLARI:
  * Her test KENDİ geçici veritabanı dizinini (tempfile) kullanır. Hiçbir test
    data/vesper.db dosyasını (veya üretim veritabanını) açmaz, okumaz, hash'lemez.
  * Bu dosya vesper.engine / vesper.config içe aktarmaz ve config.DB_PATH'e dokunan
    varsayılan bir WebinarAssistant kurmaz; yalnızca izole Database(geçici_yol)
    örnekleri kullanır (bkz. TestStaticGuards).
  * v1 fixture şeması, üretimdeki storage.py sabitlerinden BAĞIMSIZ, bu dosyada
    elle yazılmış değişmez bir kopyadır; storage.py ileride değişse bile v1
    fixture'ı değişmez.
  * Tüm bağlantılar açıkça kapatılır (Windows geçici dosyayı açık bağlantıyla silmez).

Çalıştırma:
    python -m unittest discover -s tests -t .
"""

import ast
from contextlib import closing
import gc
import hashlib
import logging
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _ROOT)

from vesper import storage
from vesper.repository import SessionRepository
from vesper.storage import SCHEMA_VERSION, Database, SchemaMigrationError, SchemaVersionError


def setUpModule():
    # Beklenen hata günlükleri (reddedilen göçler) test çıktısını kirletmesin.
    logging.getLogger("storage").addHandler(logging.NullHandler())
    # Hangi SQLite derlemesiyle çalışıldığı, composite-FK/WAL davranışının kanıtı için kayda geçer.
    print(
        "\n[test_schema_migration] python %s | sqlite3.sqlite_version %s"
        % (sys.version.split()[0], sqlite3.sqlite_version)
    )


# --------------------------------------------------------------------------- #
# BAĞIMSIZ v1 fixture şeması (storage.py'den import EDİLMEZ)
# --------------------------------------------------------------------------- #
V1_FIXTURE_DDL = (
    "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL, started_at TEXT NOT NULL, "
    "ended_at TEXT, status TEXT NOT NULL, source_type TEXT NOT NULL, source_name TEXT, "
    "source_language TEXT, summary_language TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE segments (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, sequence INTEGER NOT NULL, "
    "started_at TEXT NOT NULL, ended_at TEXT NOT NULL, text TEXT NOT NULL, language TEXT, "
    "created_at TEXT NOT NULL, UNIQUE (session_id, sequence))",
    "CREATE TABLE summaries (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, kind TEXT NOT NULL, "
    "content TEXT NOT NULL, model TEXT, created_at TEXT NOT NULL)",
    "CREATE TABLE schema_metadata (schema_version INTEGER NOT NULL)",
    "CREATE INDEX idx_segments_session ON segments(session_id, sequence)",
    "CREATE INDEX idx_summaries_session ON summaries(session_id, kind, created_at)",
    "CREATE INDEX idx_sessions_started ON sessions(started_at DESC)",
)

V2_OBJECT_NAMES = ("artifacts", "idx_segments_id_session", "idx_artifacts_session", "idx_artifacts_segment")
BACKUP_NAME_RE = re.compile(r"^(?P<stem>.+)\.pre-v2\.\d{8}T\d{12}Z\.[0-9a-f]{8}\.db$")


def make_v1_database(path, history=True):
    """Bağımsız v1 DDL'iyle, WAL modunda, tarihsel satırlı (ya da boş) v1 veritabanı kurar."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        for statement in V1_FIXTURE_DDL:
            conn.execute(statement)
        conn.execute("INSERT INTO schema_metadata VALUES (1)")
        if history:
            seed_history(conn)
        conn.commit()
    finally:
        conn.close()


def seed_history(conn):
    sessions = [
        ("sess-a", "Pazartesi Webinarı — şğüöçı İ", "2026-01-01T10:00:00+00:00", "2026-01-01T11:00:00+00:00",
         "completed", "system_audio", "Hoparlör (Realtek)", "tr", "tr", "2026-01-01T10:00:00+00:00",
         "2026-01-01T11:00:00+00:00"),
        ("sess-b", "Kesilen oturum", "2026-02-01T10:00:00+00:00", None,
         "interrupted", "system_audio", None, None, "tr", "2026-02-01T10:00:00+00:00",
         "2026-02-01T10:30:00+00:00"),
        ("sess-c", "Boş oturum", "2026-03-01T10:00:00+00:00", None,
         "active", "system_audio", None, "en", "tr", "2026-03-01T10:00:00+00:00",
         "2026-03-01T10:00:00+00:00"),
    ]
    conn.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)", sessions)
    segments = [
        ("sess-a", 1, "t0", "t1", "Merhaba, bugün İstanbul'dan bağlanıyoruz.", "tr", "c1"),
        ("sess-a", 2, "t1", "t2", "Şu an öğleden sonra, çok güzel bir gün.", "tr", "c2"),
        ("sess-a", 3, "t2", "t3", "Hello everyone, English part.", None, "c3"),
        ("sess-b", 1, "t0", "t1", "Kesilmeden önce söylenen şey", None, "c4"),
        ("sess-b", 2, "t1", "t2", "ikinci parça", None, "c5"),
    ]
    conn.executemany(
        "INSERT INTO segments (session_id, sequence, started_at, ended_at, text, language, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        segments,
    )
    conn.executemany(
        "INSERT INTO summaries (session_id, kind, content, model, created_at) VALUES (?,?,?,?,?)",
        [
            ("sess-a", "live", "canlı özet", "gemma4:e2b", "2026-01-01T10:30:00+00:00"),
            ("sess-a", "final", "## Genel Özet\n\nÖzet", "qwen2.5:3b", "2026-01-01T11:00:00+00:00"),
        ],
    )


def tamper(path, *statements, foreign_keys=True):
    """Fixture veritabanını ham SQL ile bozar/değiştirir (test hazırlığı)."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA foreign_keys=%s" % ("ON" if foreign_keys else "OFF"))
        for statement in statements:
            conn.execute(statement)
        conn.commit()
    finally:
        conn.close()


def snapshot(path):
    """Tüm kullanıcı tablolarının satırları, nesne listesi ve sqlite_sequence (bağımsız okuma)."""
    conn = sqlite3.connect(str(path))
    try:
        objects = sorted(
            conn.execute(
                "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
            ).fetchall()
        )
        data = {"objects": objects}
        without_rowid = {row[1] for row in conn.execute("PRAGMA main.table_list") if row[4]}
        for kind, name in objects:
            if kind == "table":
                order = "rowid"
                if name in without_rowid:
                    primary = sorted((row[5], row[1]) for row in conn.execute(
                        'PRAGMA main.table_xinfo("%s")' % name.replace('"', '""'),
                    ) if row[5])
                    order = ", ".join('"%s"' % column.replace('"', '""') for _, column in primary)
                data["table:" + name] = [
                    tuple(row) for row in conn.execute('SELECT * FROM "%s" ORDER BY %s' % (name.replace('"', '""'), order))
                ]
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'").fetchone():
            data["sequence"] = [
                tuple(row) for row in conn.execute("SELECT name, seq FROM sqlite_sequence ORDER BY name")
            ]
        return data
    finally:
        conn.close()


def history_part(snap):
    keys = ("table:sessions", "table:segments", "table:summaries", "table:schema_metadata")
    return {k: snap.get(k) for k in keys}


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def list_backups(db_path):
    backup_dir = Path(db_path).parent / "backups"
    if not backup_dir.exists():
        return [], []
    finals = sorted(p for p in backup_dir.iterdir() if p.suffix == ".db")
    others = sorted(p for p in backup_dir.iterdir() if p.suffix != ".db")
    return finals, others


def native_autoincrement(conn, table):
    """Bağımsız beklenti: yalnızca fixture'ın bellek kopyasında silinen id'nin yeniden kullanımını sınar.

    Üretim SQL okuyucusunu kullanmaz; özgün geçici fixture'a bile yazmaz.
    Yalnızca dondurulmuş, INTEGER id anahtarlı tablolar için kullanılır.
    """
    columns = conn.execute('PRAGMA main.table_xinfo("%s")' % table.replace('"', '""')).fetchall()
    if not any(row[1] == "id" and row[2].upper() == "INTEGER" and row[5] == 1 for row in columns):
        return False
    memory = sqlite3.connect(":memory:")
    try:
        conn.backup(memory)
        memory.execute("PRAGMA foreign_keys=OFF")
        writable = [row for row in columns if row[6] == 0]
        names = ', '.join('"%s"' % row[1].replace('"', '""') for row in writable)
        values = [1 if row[2].upper() == "INTEGER" else "fingerprint-probe" for row in writable]
        statement = 'INSERT INTO "%s" (%s) VALUES (%s)' % (
            table.replace('"', '""'), names, ', '.join('?' for _ in values),
        )
        memory.execute('DELETE FROM "%s"' % table.replace('"', '""'))
        memory.execute(statement, values)
        memory.execute('DELETE FROM "%s" WHERE id = 1' % table.replace('"', '""'))
        values[next(i for i, row in enumerate(writable) if row[1] == "id")] = None
        return memory.execute(statement, values).lastrowid != 1
    finally:
        memory.close()


def structure_signature(path):
    """Bağımsız (storage.py kullanmayan) yapısal parmak izi: otomatik dizin adları kararlı sayılmaz."""
    conn = sqlite3.connect(str(path))
    try:
        objects = sorted(
            conn.execute(
                "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
            ).fetchall()
        )
        tables = {}
        options = {row[1]: (row[2], row[4], row[5]) for row in conn.execute("PRAGMA main.table_list")}
        for kind, name in objects:
            if kind != "table":
                continue
            columns = [tuple(r)[1:] for r in conn.execute('PRAGMA main.table_xinfo("%s")' % name.replace('"', '""'))]
            fks = {}
            for r in conn.execute('PRAGMA foreign_key_list("%s")' % name):
                fks.setdefault(r[0], []).append((r[1], r[2], r[3], r[4], r[5], r[6], r[7]))
            named, auto = {}, []
            for r in conn.execute('PRAGMA index_list("%s")' % name).fetchall():
                key_cols = tuple(
                    (x[2], x[3], x[4]) for x in conn.execute('PRAGMA index_xinfo("%s")' % r[1]) if x[5] == 1
                )
                if r[3] == "c":
                    named[r[1]] = (r[2], r[4], key_cols)
                else:
                    auto.append((r[3], r[2], key_cols))
            tables[name] = {
                "columns": columns,
                "fks": sorted(fks.values()),
                "named": sorted(named.items()),
                "auto": sorted(auto),
                "autoincrement": native_autoincrement(conn, name),
                "options": options[name],
                "triggers": conn.execute(
                    "SELECT name, tbl_name, sql FROM main.sqlite_schema "
                    "WHERE type='trigger' AND tbl_name = ? COLLATE NOCASE ORDER BY name", (name,),
                ).fetchall(),
            }
        return {"objects": objects, "tables": tables}
    finally:
        conn.close()


class MigrationTestCase(unittest.TestCase):
    """Her test için taze geçici dizin; thread-local bağlantılar açıkça kapatılır."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self._databases = []

    def tearDown(self):
        for db in self._databases:
            db.close()
        gc.collect()
        self._tmp.cleanup()

    def new_db(self, path, allow_migration=None):
        """allow_migration verilmezse kurucu VARSAYILANI kullanılır (üretimdeki Database(yol) çağrısının
        biçimi); böylece varsayılanın 'reddet' olması dolaylı olarak da sınanır."""
        kwargs = {} if allow_migration is None else {"allow_migration": allow_migration}
        db = Database(path, **kwargs)
        self._databases.append(db)
        return db

    def _db_dir(self, name):
        """Her veritabanına KENDİ dizinini verir: yedekler db_path.parent/'backups' altına
        yazıldığı için, aynı dizini paylaşan veritabanlarının yedekleri birbirine karışmasın."""
        self._dir_counter = getattr(self, "_dir_counter", 0) + 1
        directory = self.tmp / ("d%02d_%s" % (self._dir_counter, Path(name).stem))
        directory.mkdir()
        return directory

    def v1_path(self, name="v1.db", history=True):
        path = self._db_dir(name) / name
        make_v1_database(path, history=history)
        return path

    def migrated_path(self, name="m.db", history=True):
        path = self.v1_path(name, history=history)
        self.new_db(path, allow_migration=True).init_schema()
        return path

    def fresh_path(self, name="f.db"):
        path = self._db_dir(name) / name
        self.new_db(path).init_schema()
        return path

    # --- ortak doğrulamalar -------------------------------------------------
    def assert_unchanged_after(self, path, action, expected_exception=None, reason=None):
        """action() reddedilmeli; dosya baytları, tüm satırlar ve nesne listesi DEĞİŞMEMELİ."""
        before_hash = sha256_file(path)
        before = snapshot(path)
        with self.assertRaises(expected_exception or SchemaMigrationError) as ctx:
            action()
        if reason is not None:
            self.assertEqual(ctx.exception.reason, reason)
        self.assertEqual(sha256_file(path), before_hash, "ana veritabanı dosyası değişmemeliydi")
        self.assertEqual(snapshot(path), before, "veritabanı içeriği/nesneleri değişmemeliydi")
        return ctx.exception

    def assert_v2_structure(self, path):
        conn = sqlite3.connect(str(path))
        try:
            column_rows = conn.execute("PRAGMA main.table_xinfo(artifacts)").fetchall()
            columns = [(r[1], r[2], r[3], r[5]) for r in column_rows]
            self.assertTrue(column_rows)
            self.assertTrue(all(r[6] == 0 for r in column_rows))
            options = conn.execute("PRAGMA main.table_list(artifacts)").fetchall()
            self.assertEqual([(r[0], r[1], r[2], r[4], r[5]) for r in options],
                             [("main", "artifacts", "table", 0, 0)])
            self.assertEqual(
                columns,
                [
                    ("id", "INTEGER", 0, 1),
                    ("session_id", "TEXT", 1, 0),
                    ("kind", "TEXT", 1, 0),
                    ("content", "TEXT", 1, 0),
                    ("capture_method", "TEXT", 1, 0),
                    ("source_segment_id", "INTEGER", 0, 0),
                    ("created_at", "TEXT", 1, 0),
                ],
            )
            groups = {}
            for r in conn.execute("PRAGMA foreign_key_list(artifacts)"):
                groups.setdefault(r[0], []).append((r[1], r[2], r[3], r[4], r[5], r[6]))
            self.assertEqual(
                sorted(groups.values()),
                [
                    [(0, "segments", "source_segment_id", "id", "NO ACTION", "NO ACTION"),
                     (1, "segments", "session_id", "session_id", "NO ACTION", "NO ACTION")],
                    [(0, "sessions", "session_id", "id", "NO ACTION", "CASCADE")],
                ],
            )
            index_list = {r[1]: r for r in conn.execute("PRAGMA index_list(segments)")}
            self.assertIn("idx_segments_id_session", index_list)
            self.assertEqual(index_list["idx_segments_id_session"][2], 1, "benzersiz olmalı")
            self.assertEqual(
                [r[2] for r in conn.execute("PRAGMA index_info(idx_segments_id_session)")],
                ["id", "session_id"],
            )
            artifact_indexes = {r[1]: r for r in conn.execute("PRAGMA index_list(artifacts)")}
            self.assertEqual(
                [r[2] for r in conn.execute("PRAGMA index_info(idx_artifacts_session)")],
                ["session_id", "created_at", "id"],
            )
            self.assertEqual(artifact_indexes["idx_artifacts_session"][2], 0)
            self.assertEqual(
                [r[2] for r in conn.execute("PRAGMA index_info(idx_artifacts_segment)")],
                ["source_segment_id", "session_id"],
            )
            self.assertEqual(
                [tuple(r) for r in conn.execute("SELECT schema_version, typeof(schema_version) FROM schema_metadata")],
                [(2, "integer")],
            )
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertTrue(native_autoincrement(conn, "artifacts"))
        finally:
            conn.close()

    def assert_valid_v1_backup(self, backup_path, original_snapshot):
        self.assertEqual(sidecar_names(backup_path), [], "yedek tek dosya olmalı")
        snap = snapshot(backup_path)
        self.assertEqual(history_part(snap), history_part(original_snapshot))
        self.assertEqual(snap.get("sequence"), original_snapshot.get("sequence"))
        self.assertFalse([n for _, n in snap["objects"] if n in V2_OBJECT_NAMES], "yedekte v2 nesnesi olmamalı")
        conn = sqlite3.connect(str(backup_path))
        try:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            conn.close()


def sidecar_names(path):
    path = Path(path)
    return sorted(p.name for p in path.parent.glob(path.name + "-*"))


# =========================================================================== #
# A. Taze (fresh) v2 kurulumu
# =========================================================================== #
class TestFreshInitialization(MigrationTestCase):
    def test_constructor_performs_no_io(self):
        path = self.tmp / "nested" / "x.db"
        Database(path)
        self.assertFalse(path.exists())
        self.assertFalse(path.parent.exists())

    def test_fresh_nonexistent_database_becomes_exactly_v2(self):
        path = self.fresh_path()
        snap = snapshot(path)
        self.assertEqual(snap["table:schema_metadata"], [(2,)])
        self.assertEqual(SCHEMA_VERSION, 2)
        self.assert_v2_structure(path)
        for table in ("sessions", "segments", "summaries", "artifacts"):
            self.assertEqual(snap["table:" + table], [])

    def test_fresh_structure_matches_independent_v1_plus_v2_expectation(self):
        # Bağımsız v1 fixture + elle yazılmış v2 DDL ile kurulan veritabanıyla aynı yapı.
        reference = self.tmp / "reference.db"
        conn = sqlite3.connect(str(reference))
        try:
            for statement in V1_FIXTURE_DDL:
                conn.execute(statement)
            conn.execute("INSERT INTO schema_metadata VALUES (2)")
            conn.execute("CREATE UNIQUE INDEX idx_segments_id_session ON segments(id, session_id)")
            conn.execute(
                "CREATE TABLE artifacts (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, "
                "kind TEXT NOT NULL, content TEXT NOT NULL, capture_method TEXT NOT NULL, "
                "source_segment_id INTEGER, created_at TEXT NOT NULL, "
                "FOREIGN KEY (source_segment_id, session_id) REFERENCES segments(id, session_id))"
            )
            conn.execute("CREATE INDEX idx_artifacts_session ON artifacts(session_id, created_at, id)")
            conn.execute("CREATE INDEX idx_artifacts_segment ON artifacts(source_segment_id, session_id)")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(structure_signature(self.fresh_path()), structure_signature(reference))

    def test_fresh_zero_byte_file_is_initialized(self):
        path = self.tmp / "zero.db"
        path.write_bytes(b"")
        self.new_db(path).init_schema()
        self.assert_v2_structure(path)

    def test_fresh_needs_no_authorization_and_creates_no_backup(self):
        db = self.new_db(self.tmp / "auth.db")
        self.assertFalse(db.allow_migration)
        db.init_schema()
        self.assertFalse((self.tmp / "backups").exists())

    def test_no_second_schema_version_authority_is_written(self):
        path = self.fresh_path()
        conn = sqlite3.connect(str(path))
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
        finally:
            conn.close()


# =========================================================================== #
# B. Tarihsel verinin korunması
# =========================================================================== #
class TestHistoricalPreservation(MigrationTestCase):
    def test_every_historical_row_and_counter_is_preserved_exactly(self):
        path = self.v1_path()
        before = snapshot(path)
        self.assertEqual(len(before["table:sessions"]), 3)
        self.assertEqual(len(before["table:segments"]), 5)
        self.assertEqual(len(before["table:summaries"]), 2)
        self.new_db(path, allow_migration=True).init_schema()
        after = snapshot(path)
        for key in ("table:sessions", "table:segments", "table:summaries"):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(after["table:schema_metadata"], [(2,)])
        self.assertEqual(after["table:artifacts"], [])
        # AUTOINCREMENT sayaçları (segments/summaries) aynı kalır; artifacts henüz yazılmadı.
        self.assertEqual(
            [r for r in after["sequence"] if r[0] in ("segments", "summaries")],
            [r for r in before["sequence"] if r[0] in ("segments", "summaries")],
        )
        self.assert_v2_structure(path)

    def test_turkish_text_survives_byte_for_byte(self):
        path = self.migrated_path()
        conn = sqlite3.connect(str(path))
        try:
            title = conn.execute("SELECT title FROM sessions WHERE id = 'sess-a'").fetchone()[0]
            text = conn.execute("SELECT text FROM segments WHERE session_id='sess-a' AND sequence=2").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(title, "Pazartesi Webinarı — şğüöçı İ")
        self.assertEqual(text, "Şu an öğleden sonra, çok güzel bir gün.")


# =========================================================================== #
# C. Göç yetkilendirme kapısı
# =========================================================================== #
class TestMigrationAuthorizationGate(MigrationTestCase):
    def test_default_is_deny_and_flag_must_be_a_real_bool(self):
        self.assertFalse(Database(self.tmp / "a.db").allow_migration)
        self.assertTrue(Database(self.tmp / "b.db", allow_migration=True).allow_migration)
        self.assertFalse(Database(self.tmp / "c.db", allow_migration=False).allow_migration)
        for bad in ("yes", "false", 1, 0, None, []):
            with self.subTest(value=bad):
                with self.assertRaises(TypeError):
                    Database(self.tmp / "d.db", allow_migration=bad)

    def test_unauthorized_init_on_v1_raises_and_changes_nothing(self):
        path = self.v1_path()
        exc = self.assert_unchanged_after(
            path,
            lambda: self.new_db(path).init_schema(),
            expected_exception=SchemaMigrationError,
            reason="authorization_required",
        )
        self.assertNotIsInstance(exc, SchemaVersionError)
        self.assertIn("allow_migration=True", str(exc))
        self.assertFalse((path.parent / "backups").exists(), "yedek oluşturulmamalı")
        snap = snapshot(path)
        self.assertEqual(snap["table:schema_metadata"], [(1,)])
        self.assertFalse([n for _, n in snap["objects"] if n in V2_OBJECT_NAMES])

    def test_refusal_is_never_converted_into_fresh_initialization(self):
        path = self.v1_path()
        size_before = path.stat().st_size
        with self.assertRaises(SchemaMigrationError):
            self.new_db(path).init_schema()
        self.assertEqual(path.stat().st_size, size_before)
        self.assertEqual(len(snapshot(path)["table:sessions"]), 3, "tarihsel veri yerinde olmalı")

    def test_empty_v1_is_also_refused_without_authorization(self):
        path = self.v1_path("empty.db", history=False)
        self.assert_unchanged_after(
            path, lambda: self.new_db(path).init_schema(),
            expected_exception=SchemaMigrationError, reason="authorization_required",
        )

    def test_authorization_is_not_inferred_from_environment_or_path(self):
        environment = {
            "VESPER_ALLOW_MIGRATION": "1", "VESPER_MIGRATE": "true", "VESPER_TEST": "1",
            "VESPER_DEBUG": "1", "PYTHONDEVMODE": "1", "CI": "true", "TESTING": "1",
        }
        path = self.v1_path("vesper_test_allow_migration_debug.db")
        with mock.patch.dict(os.environ, environment):
            self.assert_unchanged_after(
                path, lambda: self.new_db(path).init_schema(),
                expected_exception=SchemaMigrationError, reason="authorization_required",
            )

    def test_authorization_is_per_instance_never_global(self):
        first = self.v1_path("first.db")
        second = self.v1_path("second.db")
        self.new_db(first, allow_migration=True).init_schema()
        self.assertEqual(snapshot(first)["table:schema_metadata"], [(2,)])
        self.assert_unchanged_after(
            second, lambda: self.new_db(second).init_schema(),
            expected_exception=SchemaMigrationError, reason="authorization_required",
        )

    def test_authorized_init_performs_validated_migration(self):
        path = self.v1_path()
        original = snapshot(path)
        self.new_db(path, allow_migration=True).init_schema()
        self.assert_v2_structure(path)
        finals, others = list_backups(path)
        self.assertEqual(len(finals), 1)
        self.assertEqual(others, [])
        self.assert_valid_v1_backup(finals[0], original)

    def test_valid_v2_opens_without_authorization_and_without_backup(self):
        for builder in (self.fresh_path, self.migrated_path):
            with self.subTest(origin=builder.__name__):
                path = builder("v2_%s.db" % builder.__name__)
                backups_before = list_backups(path)
                before = snapshot(path)
                self.new_db(path).init_schema()  # allow_migration=False
                self.assertEqual(snapshot(path), before)
                self.assertEqual(list_backups(path), backups_before)

    def test_session_repository_on_unauthorized_v1_raises_and_changes_nothing(self):
        path = self.v1_path()
        self.assert_unchanged_after(
            path, lambda: SessionRepository(self.new_db(path)),
            expected_exception=SchemaMigrationError, reason="authorization_required",
        )

    def test_unversioned_legacy_v1_requires_authorization_then_migrates(self):
        path = self.v1_path("legacy.db")
        tamper(path, "DELETE FROM schema_metadata")
        self.assert_unchanged_after(
            path, lambda: self.new_db(path).init_schema(),
            expected_exception=SchemaMigrationError, reason="authorization_required",
        )
        original = snapshot(path)
        self.new_db(path, allow_migration=True).init_schema()
        self.assert_v2_structure(path)
        finals, _ = list_backups(path)
        self.assertEqual(len(finals), 1)
        self.assert_valid_v1_backup(finals[0], original)  # yedekte schema_metadata hâlâ boş

    def test_authorized_unversioned_database_without_history_makes_no_backup(self):
        path = self.v1_path("legacy_empty.db", history=False)
        tamper(path, "DELETE FROM schema_metadata")
        self.new_db(path, allow_migration=True).init_schema()
        self.assert_v2_structure(path)
        self.assertEqual(list_backups(path), ([], []))


# =========================================================================== #
# D. Göç idempotansı
# =========================================================================== #
class TestMigrationIdempotence(MigrationTestCase):
    def test_repeated_init_never_reapplies_or_duplicates(self):
        path = self.v1_path()
        db = self.new_db(path, allow_migration=True)
        db.init_schema()
        finals, _ = list_backups(path)
        names = [p.name for p in finals]
        after_first = snapshot(path)
        for _ in range(3):
            db.init_schema()
        self.new_db(path, allow_migration=True).init_schema()
        self.new_db(path, allow_migration=False).init_schema()
        self.assertEqual([p.name for p in list_backups(path)[0]], names, "yeni yedek oluşmamalı")
        self.assertEqual(snapshot(path), after_first)
        self.assertEqual(snapshot(path)["table:schema_metadata"], [(2,)])
        self.assert_v2_structure(path)


# =========================================================================== #
# E. İşlemsel (transactional) geri alma
# =========================================================================== #
def failing_ddl(nth):
    """nth. DDL çağrısında istisna fırlatan Database._execute_ddl_statement sarmalayıcısı."""
    original = Database._execute_ddl_statement
    calls = {"n": 0}

    def wrapper(db, conn, statement):
        calls["n"] += 1
        if calls["n"] == nth:
            raise RuntimeError("enjekte edilen DDL hatası #%d" % nth)
        return original(db, conn, statement)

    return wrapper, calls


class TestTransactionalRollback(MigrationTestCase):
    def _assert_failed_migration_rolled_back(self, path, original, exc, db):
        self.assertIsInstance(exc, SchemaMigrationError)
        self.assertNotIsInstance(exc, SchemaVersionError)
        self.assertEqual(exc.reason, "migration_ddl_failed")
        self.assertEqual(snapshot(path), original, "v1 ve tarihsel satırlar aynen korunmalı")
        self.assertEqual(snapshot(path)["table:schema_metadata"], [(1,)])
        self.assertFalse([n for _, n in snapshot(path)["objects"] if n in V2_OBJECT_NAMES])
        self.assertFalse(db._local.schema_ready, "başarısız hazırlık hazır sayılamaz")
        self.assertIsNone(db._local.conn, "başarısız hazırlık bağlantısı iptal edilmeli")
        self.assertEqual(exc.transaction_outcome, "rolled_back")
        finals, others = list_backups(path)
        self.assertEqual(len(finals), 1, "tamamlanmış yedek korunmalı")
        self.assertEqual(others, [])
        self.assert_valid_v1_backup(finals[0], original)

    def _retry_succeeds_and_keeps_first_backup(self, path, original):
        first_backup = list_backups(path)[0][0]
        first_hash = sha256_file(first_backup)
        self.new_db(path, allow_migration=True).init_schema()
        self.assert_v2_structure(path)
        finals, _ = list_backups(path)
        self.assertEqual(len(finals), 2, "yeniden deneme yeni, farklı adlı bir yedek almalı")
        self.assertEqual(sha256_file(first_backup), first_hash, "ilk yedek ezilmemeli")
        for backup in finals:
            self.assert_valid_v1_backup(backup, original)

    def test_failure_after_first_ddl_statement_rolls_back_to_v1(self):
        path = self.v1_path()
        original = snapshot(path)
        db = self.new_db(path, allow_migration=True)
        wrapper, calls = failing_ddl(2)
        with mock.patch.object(Database, "_execute_ddl_statement", wrapper):
            with self.assertRaises(SchemaMigrationError) as ctx:
                db.init_schema()
        self.assertEqual(calls["n"], 2, "ilk DDL çalışmış, ikincisi patlamış olmalı")
        self.assertIsInstance(ctx.exception.__cause__, RuntimeError)
        self._assert_failed_migration_rolled_back(path, original, ctx.exception, db)
        self._retry_succeeds_and_keeps_first_backup(path, original)

    def test_failure_on_last_ddl_statement_rolls_back_everything(self):
        path = self.v1_path()
        original = snapshot(path)
        db = self.new_db(path, allow_migration=True)
        wrapper, calls = failing_ddl(4)
        with mock.patch.object(Database, "_execute_ddl_statement", wrapper):
            with self.assertRaises(SchemaMigrationError) as ctx:
                db.init_schema()
        self.assertEqual(calls["n"], 4)
        self._assert_failed_migration_rolled_back(path, original, ctx.exception, db)

    def test_failure_during_version_update_rolls_back_all_ddl(self):
        path = self.v1_path()
        original = snapshot(path)
        db = self.new_db(path, allow_migration=True)

        def broken_version_write(self_db, conn, insert):
            raise sqlite3.OperationalError("enjekte edilen sürüm güncelleme hatası")

        with mock.patch.object(Database, "_write_schema_version", broken_version_write):
            with self.assertRaises(SchemaMigrationError) as ctx:
                db.init_schema()
        self._assert_failed_migration_rolled_back(path, original, ctx.exception, db)
        self._retry_succeeds_and_keeps_first_backup(path, original)

    def test_failed_post_verification_rolls_back_everything(self):
        path = self.v1_path()
        original = snapshot(path)
        db = self.new_db(path, allow_migration=True)

        def failing_verification(self_db, conn):
            raise SchemaMigrationError("enjekte edilen doğrulama hatası", reason="migration_ddl_failed")

        with mock.patch.object(Database, "_verify_v2_in_transaction", failing_verification):
            with self.assertRaises(SchemaMigrationError) as ctx:
                db.init_schema()
        self._assert_failed_migration_rolled_back(path, original, ctx.exception, db)

    def test_failed_fresh_initialization_leaves_an_empty_database_and_can_be_retried(self):
        path = self.tmp / "fresh_fail.db"
        db = self.new_db(path)
        wrapper, calls = failing_ddl(3)
        with mock.patch.object(Database, "_execute_ddl_statement", wrapper):
            with self.assertRaises(SchemaMigrationError) as ctx:
                db.init_schema()
        self.assertEqual(ctx.exception.reason, "migration_ddl_failed")
        self.assertEqual(snapshot(path)["objects"], [], "kısmi şema kalmamalı")
        self.assertFalse(db._local.schema_ready)
        self.assertIsNone(db._local.conn)
        self.assertEqual(ctx.exception.transaction_outcome, "rolled_back")
        self.new_db(path).init_schema()
        self.assert_v2_structure(path)


# =========================================================================== #
# F. Sürüm 99 (daha yeni) reddi
# =========================================================================== #
class TestNewerVersionRefusal(MigrationTestCase):
    def test_newer_versions_are_always_refused_and_never_modified(self):
        for version in (3, 99):
            for allow in (False, True):
                with self.subTest(version=version, allow_migration=allow):
                    path = self.v1_path("newer_%d_%s.db" % (version, allow))
                    tamper(path, "UPDATE schema_metadata SET schema_version = %d" % version)
                    exc = self.assert_unchanged_after(
                        path,
                        lambda p=path, a=allow: self.new_db(p, allow_migration=a).init_schema(),
                        expected_exception=SchemaVersionError,
                        reason="newer_schema",
                    )
                    self.assertIn("v%d" % version, str(exc))
                    self.assertEqual(list_backups(path), ([], []))

    def test_newer_version_with_v2_structure_is_refused_too(self):
        path = self.migrated_path()
        tamper(path, "UPDATE schema_metadata SET schema_version = 99")
        self.assert_unchanged_after(
            path, lambda: self.new_db(path).init_schema(),
            expected_exception=SchemaVersionError, reason="newer_schema",
        )

    def test_session_repository_refuses_newer_database(self):
        path = self.v1_path()
        tamper(path, "UPDATE schema_metadata SET schema_version = 99")
        self.assert_unchanged_after(
            path, lambda: SessionRepository(self.new_db(path, allow_migration=True)),
            expected_exception=SchemaVersionError, reason="newer_schema",
        )


# =========================================================================== #
# G. Metadata tutarsızlıkları (yetki verilse bile reddedilir)
# =========================================================================== #
class TestMetadataInconsistencies(MigrationTestCase):
    def test_inconsistent_metadata_is_refused_even_when_migration_is_authorized(self):
        cases = {
            "schema_metadata tablosu eksik": ("DROP TABLE schema_metadata",),
            "yinelenen eşit satırlar": ("INSERT INTO schema_metadata VALUES (1)",),
            "yinelenen farklı satırlar": ("INSERT INTO schema_metadata VALUES (2)",),
            "sürüm sıfır": ("UPDATE schema_metadata SET schema_version = 0",),
            "sürüm negatif": ("UPDATE schema_metadata SET schema_version = -5",),
            "sürüm metin": ("UPDATE schema_metadata SET schema_version = 'abc'",),
            "sürüm gerçel sayı": ("UPDATE schema_metadata SET schema_version = 1.5",),
            "boş metadata + eksik dizin": ("DELETE FROM schema_metadata", "DROP INDEX idx_summaries_session"),
            "boş metadata + fazladan tablo": ("DELETE FROM schema_metadata", "CREATE TABLE fazladan (x TEXT)"),
            "boş metadata + fazladan dizin": ("DELETE FROM schema_metadata", "CREATE INDEX fazladan_idx ON sessions(title)"),
        }
        for label, statements in cases.items():
            with self.subTest(case=label):
                path = self.v1_path("meta_%d.db" % abs(hash(label)))
                tamper(path, *statements)
                exc = self.assert_unchanged_after(
                    path,
                    lambda p=path: self.new_db(p, allow_migration=True).init_schema(),
                    expected_exception=SchemaVersionError,
                    reason="inconsistent_schema",
                )
                self.assertTrue(str(exc))
                self.assertEqual(list_backups(path), ([], []), "reddedilen durumda yedek alınmamalı")

    def test_non_sqlite_file_is_refused_and_untouched(self):
        path = self.tmp / "garbage.db"
        path.write_bytes(b"Bu bir SQLite veritabani degil. " * 40)
        before = sha256_file(path)
        with self.assertRaises(SchemaVersionError):
            self.new_db(path, allow_migration=True).init_schema()
        self.assertEqual(sha256_file(path), before)

    def test_unrecognized_empty_metadata_without_authorization_reports_inconsistency_not_authorization(self):
        path = self.v1_path()
        tamper(path, "DELETE FROM schema_metadata", "CREATE TABLE fazladan (x TEXT)")
        self.assert_unchanged_after(
            path, lambda: self.new_db(path).init_schema(),
            expected_exception=SchemaVersionError, reason="inconsistent_schema",
        )


# =========================================================================== #
# H, I, J. Bileşik yabancı anahtar, oturum silme zinciri, tekil segment silme
# =========================================================================== #
class TestCompositeForeignKeyBehaviour(MigrationTestCase):
    def _v2_databases(self):
        return {"fresh": self.fresh_path("fk_fresh.db"), "migrated": self.migrated_path("fk_migrated.db", history=False)}

    @staticmethod
    def _seed(conn):
        for sid in ("s1", "s2"):
            conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (sid, "t", "a", None, "active", "system_audio", None, None, None, "x", "x"))
        ids = {}
        for sid in ("s1", "s2"):
            for seq in (1, 2):
                cur = conn.execute(
                    "INSERT INTO segments (session_id, sequence, started_at, ended_at, text, language, created_at) "
                    "VALUES (?,?,?,?,?,NULL,?)", (sid, seq, "a", "b", "t", "c"))
                ids[(sid, seq)] = cur.lastrowid
        conn.commit()
        return ids

    @staticmethod
    def _artifact(conn, session_id, segment_id):
        conn.execute(
            "INSERT INTO artifacts (session_id, kind, content, capture_method, source_segment_id, created_at) "
            "VALUES (?,?,?,?,?,?)", (session_id, "link", "https://x.test", "manual_entry", segment_id, "now"))

    def _each(self, check):
        for origin, path in self._v2_databases().items():
            with self.subTest(origin=origin):
                conn = sqlite3.connect(str(path))
                try:
                    conn.execute("PRAGMA foreign_keys=ON")
                    check(conn, self._seed(conn))
                finally:
                    conn.close()

    def test_same_session_reference_null_and_invalid_references(self):
        def check(conn, ids):
            self._artifact(conn, "s1", ids[("s1", 1)])            # aynı oturum: kabul
            self._artifact(conn, "s1", None)                       # NULL: kabul
            for bad_segment in (ids[("s2", 1)], 987654):           # başka oturum / olmayan segment
                with self.assertRaises(sqlite3.IntegrityError):
                    self._artifact(conn, "s1", bad_segment)
            conn.rollback()
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0], 0)
        self._each(check)

    def test_foreign_key_check_detects_cross_session_row_written_with_enforcement_off(self):
        path = self.fresh_path("fkoff.db")
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            ids = self._seed(conn)
            conn.execute("PRAGMA foreign_keys=OFF")
            self._artifact(conn, "s1", ids[("s2", 1)])
            conn.commit()
            conn.execute("PRAGMA foreign_keys=ON")
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            conn.close()
        self.assertEqual([v[0] for v in violations], ["artifacts"])

    def test_session_delete_cascades_through_segments_and_artifacts(self):
        def check(conn, ids):
            self._artifact(conn, "s1", ids[("s1", 1)])
            self._artifact(conn, "s1", ids[("s1", 2)])
            self._artifact(conn, "s1", None)
            self._artifact(conn, "s2", ids[("s2", 1)])
            conn.commit()
            conn.execute("DELETE FROM sessions WHERE id = 's1'")
            conn.commit()
            count = lambda sql: conn.execute(sql).fetchone()[0]
            self.assertEqual(count("SELECT COUNT(*) FROM segments WHERE session_id='s1'"), 0)
            self.assertEqual(count("SELECT COUNT(*) FROM artifacts WHERE session_id='s1'"), 0)
            self.assertEqual(count("SELECT COUNT(*) FROM segments WHERE session_id='s2'"), 2)
            self.assertEqual(count("SELECT COUNT(*) FROM artifacts WHERE session_id='s2'"), 1)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self._each(check)

    def test_individually_referenced_segment_cannot_be_deleted(self):
        def check(conn, ids):
            self._artifact(conn, "s1", ids[("s1", 1)])
            conn.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM segments WHERE id = ?", (ids[("s1", 1)],))
            conn.rollback()
            conn.execute("DELETE FROM segments WHERE id = ?", (ids[("s1", 2)],))   # bağlı olmayan: silinebilir
            conn.commit()
            # önce bağı kopar (NULL), sonra silinebilir
            conn.execute("UPDATE artifacts SET source_segment_id = NULL")
            conn.execute("DELETE FROM segments WHERE id = ?", (ids[("s1", 1)],))
            conn.commit()
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self._each(check)

    def test_deleting_an_artifact_leaves_its_segment_alone(self):
        def check(conn, ids):
            self._artifact(conn, "s1", ids[("s1", 1)])
            conn.commit()
            conn.execute("DELETE FROM artifacts")
            conn.commit()
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM segments WHERE id = ?", (ids[("s1", 1)],)).fetchone()[0], 1)
        self._each(check)

    def test_artifact_ids_are_never_reused(self):
        path = self.fresh_path("autoinc.db")
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            self._seed(conn)
            self._artifact(conn, "s1", None)
            first_id = conn.execute("SELECT MAX(id) FROM artifacts").fetchone()[0]
            conn.execute("DELETE FROM artifacts")
            self._artifact(conn, "s1", None)
            second_id = conn.execute("SELECT MAX(id) FROM artifacts").fetchone()[0]
            conn.commit()
        finally:
            conn.close()
        self.assertGreater(second_id, first_id)


# =========================================================================== #
# K. Yedek bütünlüğü
# =========================================================================== #
class TestBackupIntegrity(MigrationTestCase):
    def test_backup_is_independent_complete_single_file_snapshot(self):
        path = self.v1_path("vesper.db")
        original = snapshot(path)
        self.new_db(path, allow_migration=True).init_schema()
        finals, others = list_backups(path)
        self.assertEqual((len(finals), others), (1, []))
        backup = finals[0]
        self.assertEqual(backup.parent, path.parent / "backups", "yol db_path.parent/backups'tan türetilmeli")
        match = BACKUP_NAME_RE.match(backup.name)
        self.assertIsNotNone(match, backup.name)
        self.assertEqual(match.group("stem"), "vesper")
        self.assert_valid_v1_backup(backup, original)
        conn = sqlite3.connect(str(backup))
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "delete")
            self.assertEqual(
                [tuple(r) for r in conn.execute("SELECT schema_version, typeof(schema_version) FROM schema_metadata")],
                [(1, "integer")],
            )
        finally:
            conn.close()

    def test_backup_dir_follows_the_database_location_not_a_hardcoded_path(self):
        path = self.tmp / "deep" / "er" / "custom_name.db"
        path.parent.mkdir(parents=True)
        make_v1_database(path)
        self.new_db(path, allow_migration=True).init_schema()
        finals, _ = list_backups(path)
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0].parent, path.parent / "backups")
        self.assertTrue(finals[0].name.startswith("custom_name.pre-v2."))

    def test_no_backup_for_fresh_empty_v1_v2_or_refused_states(self):
        fresh = self.fresh_path("fresh.db")
        self.assertEqual(list_backups(fresh), ([], []))
        empty_v1 = self.v1_path("empty.db", history=False)
        self.new_db(empty_v1, allow_migration=True).init_schema()
        self.assertEqual(list_backups(empty_v1), ([], []))
        v2 = self.migrated_path("again.db")
        names = [p.name for p in list_backups(v2)[0]]
        self.assertEqual(len(names), 1)                      # göç sırasında alınan tek yedek
        self.new_db(v2).init_schema()                        # geçerli v2'yi yeniden açmak yedek almaz
        self.new_db(v2, allow_migration=True).init_schema()
        self.assertEqual([p.name for p in list_backups(v2)[0]], names)
        refused = self.v1_path("refused.db")
        with self.assertRaises(SchemaMigrationError):
            self.new_db(refused).init_schema()
        self.assertEqual(list_backups(refused), ([], []))

    def test_backup_names_are_unique_and_pre_existing_files_are_untouched(self):
        path = self.v1_path()
        backup_dir = path.parent / "backups"
        backup_dir.mkdir()
        decoy = backup_dir / "kullanici_yedegi.db"
        decoy.write_bytes(b"kullanicinin kendi yedegi")
        stranded = backup_dir / "eski.pre-v2.20200101T000000000000Z.deadbeef.db.partial"
        stranded.write_bytes(b"onceki calismadan kalan")
        self.new_db(path, allow_migration=True).init_schema()
        self.assertEqual(decoy.read_bytes(), b"kullanicinin kendi yedegi")
        self.assertEqual(stranded.read_bytes(), b"onceki calismadan kalan")
        self.assertEqual(len([p for p in backup_dir.iterdir() if BACKUP_NAME_RE.match(p.name)]), 1)


# =========================================================================== #
# L. Yedek başarısızlığı göçü engeller
# =========================================================================== #
class TestBackupFailure(MigrationTestCase):
    def _assert_backup_failure_blocks_migration(self, path, action):
        original = snapshot(path)
        original_hash = sha256_file(path)
        ddl_calls = []
        real_ddl = Database._execute_ddl_statement

        def counting_ddl(db, conn, statement):
            ddl_calls.append(statement)
            return real_ddl(db, conn, statement)

        with mock.patch.object(Database, "_execute_ddl_statement", counting_ddl):
            with self.assertRaises(SchemaMigrationError) as ctx:
                action()
        self.assertEqual(ctx.exception.reason, "backup_failed")
        self.assertNotIsInstance(ctx.exception, SchemaVersionError)
        self.assertEqual(ddl_calls, [], "yedek başarısızsa DDL ÇALIŞTIRILMAMALI")
        self.assertEqual(sha256_file(path), original_hash)
        self.assertEqual(snapshot(path), original)
        return ctx.exception

    def test_unusable_backup_directory_blocks_migration(self):
        path = self.v1_path()
        blocker = path.parent / "backups"
        blocker.write_text("ben bir klasör değilim")
        self._assert_backup_failure_blocks_migration(path, lambda: self.new_db(path, allow_migration=True).init_schema())
        self.assertEqual(blocker.read_text(), "ben bir klasör değilim")

    def test_backup_copy_failure_blocks_migration_and_leaves_no_temp_files(self):
        path = self.v1_path()

        def failing_copy(src, dst):
            Path(dst).write_bytes(b"yarim kalmis yedek")
            raise OSError("disk dolu (enjekte)")

        with mock.patch.object(storage, "_online_backup_copy", failing_copy):
            self._assert_backup_failure_blocks_migration(path, lambda: self.new_db(path, allow_migration=True).init_schema())
        finals, others = list_backups(path)
        self.assertEqual((finals, others), ([], []), "bu denemenin geçici dosyaları temizlenmeli, final yedek olmamalı")

    def test_backup_verification_mismatch_blocks_migration(self):
        path = self.v1_path()
        real_copy = storage._online_backup_copy

        def corrupting_copy(src, dst):
            real_copy(src, dst)
            conn = sqlite3.connect(str(dst))
            try:
                conn.execute("DELETE FROM segments WHERE id = (SELECT MIN(id) FROM segments)")
                conn.commit()
            finally:
                conn.close()

        with mock.patch.object(storage, "_online_backup_copy", corrupting_copy):
            exc = self._assert_backup_failure_blocks_migration(
                path, lambda: self.new_db(path, allow_migration=True).init_schema())
        self.assertIn("doğrulaması başarısız", str(exc.__cause__))
        self.assertEqual(list_backups(path), ([], []))

    def test_progress_deadline_is_best_effort_and_failure_blocks_migration(self):
        path = self.v1_path()
        with mock.patch.object(storage, "BACKUP_PROGRESS_DEADLINE_SEC", -1.0):
            exc = self._assert_backup_failure_blocks_migration(
                path, lambda: self.new_db(path, allow_migration=True).init_schema())
        self.assertIn("en iyi çaba", str(exc.__cause__))
        self.assertEqual(list_backups(path), ([], []))

    def test_failed_attempt_never_deletes_foreign_files(self):
        path = self.v1_path()
        backup_dir = path.parent / "backups"
        backup_dir.mkdir()
        user_backup = backup_dir / "kullanici.pre-v2.20200101T000000000000Z.cafebabe.db"
        user_backup.write_bytes(b"tamamlanmis kullanici yedegi")
        stranded = backup_dir / "kalan.partial"
        stranded.write_bytes(b"onceki calismadan kalan gecici dosya")

        def failing_copy(src, dst):
            raise OSError("enjekte")

        with mock.patch.object(storage, "_online_backup_copy", failing_copy):
            self._assert_backup_failure_blocks_migration(path, lambda: self.new_db(path, allow_migration=True).init_schema())
        self.assertEqual(user_backup.read_bytes(), b"tamamlanmis kullanici yedegi")
        self.assertEqual(stranded.read_bytes(), b"onceki calismadan kalan gecici dosya")


# =========================================================================== #
# M. WAL'de bekleyen (henüz checkpoint edilmemiş) tarihsel veri
# =========================================================================== #
class TestWalResidentData(MigrationTestCase):
    def _add_wal_resident_rows(self, path):
        writer = sqlite3.connect(str(path))
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")   # satırlar WAL'de KALSIN
        writer.execute("PRAGMA foreign_keys=ON")
        writer.execute("INSERT INTO sessions VALUES ('wal-sess','WAL oturumu','a',NULL,'active','system_audio',"
                       "NULL,NULL,'tr','x','x')")
        for seq in range(1, 31):
            writer.execute(
                "INSERT INTO segments (session_id, sequence, started_at, ended_at, text, language, created_at) "
                "VALUES ('wal-sess', ?, 'a', 'b', ?, NULL, 'c')", (seq, "WAL'de bekleyen satır %d çğış" % seq))
        writer.execute("INSERT INTO summaries (session_id, kind, content, model, created_at) "
                       "VALUES ('wal-sess','live','wal özet','m','c')")
        writer.commit()
        return writer

    def test_backup_includes_committed_rows_that_exist_only_in_the_wal(self):
        path = self.v1_path()
        writer = self._add_wal_resident_rows(path)
        try:
            wal = Path(str(path) + "-wal")
            self.assertTrue(wal.exists() and wal.stat().st_size > 0, "ön koşul: satırlar WAL'de olmalı")
            expected = snapshot(path)
            self.assertEqual(len(expected["table:segments"]), 35)
            self.new_db(path, allow_migration=True).init_schema()
            finals, _ = list_backups(path)
            self.assertEqual(len(finals), 1)
            self.assert_valid_v1_backup(finals[0], expected)
            self.assertEqual(len(snapshot(finals[0])["table:segments"]), 35)
        finally:
            writer.close()
        self.assert_v2_structure(path)

    def test_raw_file_copy_would_have_missed_the_wal_rows_negative_control(self):
        path = self.v1_path()
        writer = self._add_wal_resident_rows(path)
        try:
            raw = self.tmp / "raw_copy.db"
            try:
                raw.write_bytes(path.read_bytes())
            except OSError as exc:
                self.skipTest("işletim sistemi açık veritabanının ham kopyasına izin vermedi: %s" % exc)
            conn = sqlite3.connect(str(raw))
            try:
                try:
                    raw_segments = conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
                except sqlite3.DatabaseError:
                    raw_segments = -1
            finally:
                conn.close()
            self.assertLess(raw_segments, 35, "senaryo WAL'i denemiyor: ham kopya tüm satırları içeriyor")
        finally:
            writer.close()


# =========================================================================== #
# N. Eşzamanlı başlatma
# =========================================================================== #
RACE_CHILD = textwrap.dedent(
    """
    import os, sys, time
    sys.path.insert(0, sys.argv[1])
    from vesper.storage import Database
    path, go = sys.argv[2], sys.argv[3]
    deadline = time.time() + 30
    while not os.path.exists(go) and time.time() < deadline:
        time.sleep(0.001)
    db = Database(path, allow_migration=True)
    try:
        db.init_schema()
    finally:
        db.close()
    """
)

CRASH_CHILD = textwrap.dedent(
    """
    import os, sys
    sys.path.insert(0, sys.argv[1])
    from vesper.storage import Database
    mode, path = sys.argv[2], sys.argv[3]
    if mode.startswith("ddl"):
        target = int(mode[3:])
        original = Database._execute_ddl_statement
        calls = {"n": 0}
        def crashing(self, conn, statement):
            calls["n"] += 1
            if calls["n"] == target:
                os._exit(9)
            return original(self, conn, statement)
        Database._execute_ddl_statement = crashing
    elif mode == "version":
        def crash_version(self, conn, insert):
            os._exit(9)
        Database._write_schema_version = crash_version
    Database(path, allow_migration=True).init_schema()
    sys.exit(0)
    """
)


def run_child(code, *args, wait=True):
    proc = subprocess.Popen([sys.executable, "-c", code, _ROOT] + [str(a) for a in args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if not wait:
        return proc
    return finish_child(proc)


def finish_child(proc):
    try:
        out, err = proc.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise AssertionError("alt süreç zaman aşımına uğradı (sert sonlandırıldı)")
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


class TestConcurrentInitialization(MigrationTestCase):
    def _run_instance(self, name, db, errors):
        try:
            db.init_schema()
        except BaseException as exc:  # noqa: BLE001 - testin sonucu olarak toplanır
            errors[name] = exc
        finally:
            db.close()

    def test_two_authorized_instances_converge_with_exactly_one_backup(self):
        path = self.v1_path()
        original = snapshot(path)
        entered, release = threading.Event(), threading.Event()
        real_copy = storage._online_backup_copy

        def blocking_copy(src, dst):
            entered.set()
            if not release.wait(60):
                raise RuntimeError("test zaman aşımı")
            return real_copy(src, dst)

        errors = {}
        db_a, db_b = Database(path, allow_migration=True), Database(path, allow_migration=True)
        with mock.patch.object(storage, "_online_backup_copy", blocking_copy):
            thread_a = threading.Thread(target=self._run_instance, args=("a", db_a, errors))
            thread_a.start()
            self.assertTrue(entered.wait(60), "A yedek aşamasına ulaşmadı")
            thread_b = threading.Thread(target=self._run_instance, args=("b", db_b, errors))
            thread_b.start()
            time.sleep(0.4)
            self.assertTrue(thread_b.is_alive(), "B, A yazma kilidini tutarken tamamlanmamalı")
            release.set()
            thread_a.join(60)
            thread_b.join(60)
        self.assertFalse(thread_a.is_alive() or thread_b.is_alive())
        self.assertEqual(errors, {})
        self.assert_v2_structure(path)
        finals, others = list_backups(path)
        self.assertEqual((len(finals), others), (1, []), "tam olarak BİR yedek")
        self.assert_valid_v1_backup(finals[0], original)

    def test_unauthorized_instance_does_not_wait_for_or_start_a_migration(self):
        path = self.v1_path()
        entered, release = threading.Event(), threading.Event()
        real_copy = storage._online_backup_copy

        def blocking_copy(src, dst):
            entered.set()
            if not release.wait(60):
                raise RuntimeError("test zaman aşımı")
            return real_copy(src, dst)

        errors = {}
        db_a, db_b = Database(path, allow_migration=True), Database(path, allow_migration=False)
        with mock.patch.object(storage, "_online_backup_copy", blocking_copy):
            thread_a = threading.Thread(target=self._run_instance, args=("a", db_a, errors))
            thread_a.start()
            self.assertTrue(entered.wait(60))
            thread_b = threading.Thread(target=self._run_instance, args=("b", db_b, errors))
            thread_b.start()
            thread_b.join(20)
            self.assertFalse(thread_b.is_alive(), "yetkisiz örnek kilidi BEKLEMEMELİ")
            self.assertIsInstance(errors.get("b"), SchemaMigrationError)
            self.assertEqual(errors["b"].reason, "authorization_required")
            release.set()
            thread_a.join(60)
        self.assertNotIn("a", errors)
        self.assert_v2_structure(path)
        self.assertEqual(len(list_backups(path)[0]), 1)

    def test_many_authorized_instances_race_without_partial_state(self):
        for round_no in range(3):
            with self.subTest(round=round_no):
                path = self.v1_path("race_%d.db" % round_no)
                original = snapshot(path)
                barrier = threading.Barrier(4)
                errors = {}

                def worker(name):
                    db = Database(path, allow_migration=True)
                    try:
                        barrier.wait(30)
                        db.init_schema()
                    except BaseException as exc:  # noqa: BLE001
                        errors[name] = exc
                    finally:
                        db.close()

                threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(90)
                self.assertFalse(any(t.is_alive() for t in threads))
                self.assertEqual(errors, {})
                self.assert_v2_structure(path)
                finals, others = list_backups(path)
                self.assertEqual((len(finals), others), (1, []))
                self.assert_valid_v1_backup(finals[0], original)

    def test_competing_processes_converge_to_one_migration_and_one_backup(self):
        for round_no in range(3):
            with self.subTest(round=round_no):
                path = self.v1_path("proc_%d.db" % round_no)
                original = snapshot(path)
                go = self.tmp / ("go_%d" % round_no)
                procs = [run_child(RACE_CHILD, path, go, wait=False) for _ in range(2)]
                time.sleep(0.2)
                go.write_text("go")
                results = [finish_child(p) for p in procs]
                for code, out, err in results:
                    self.assertEqual(code, 0, err)
                self.assert_v2_structure(path)
                finals, others = list_backups(path)
                self.assertEqual((len(finals), others), (1, []))
                self.assert_valid_v1_backup(finals[0], original)


# =========================================================================== #
# Süreç çökmesi (COMMIT'ten önce) şemayı v1 olarak kurtarılabilir bırakır
# =========================================================================== #
class TestCrashRecovery(MigrationTestCase):
    def test_hard_process_exit_before_commit_leaves_recoverable_v1(self):
        for mode in ("ddl1", "ddl3", "version"):
            with self.subTest(crash_point=mode):
                path = self.v1_path("crash_%s.db" % mode)
                original = snapshot(path)
                code, out, err = run_child(CRASH_CHILD, mode, path)
                self.assertEqual(code, 9, "alt süreç kasıtlı olarak sert sonlanmalıydı: %s" % err)
                after_crash = snapshot(path)
                self.assertEqual(history_part(after_crash), history_part(original))
                self.assertEqual(after_crash["objects"], original["objects"], "v2 nesnesi sızmamalı")
                conn = sqlite3.connect(str(path))
                try:
                    self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                finally:
                    conn.close()
                finals, others = list_backups(path)
                self.assertEqual((len(finals), others), (1, []), "yedek DDL'den önce tamamlanmış olmalı")
                self.assert_valid_v1_backup(finals[0], original)
                first_hash = sha256_file(finals[0])
                self.new_db(path, allow_migration=True).init_schema()      # kurtarma: yeniden dene
                self.assert_v2_structure(path)
                self.assertEqual(len(list_backups(path)[0]), 2)
                self.assertEqual(sha256_file(finals[0]), first_hash, "ilk yedek korunmalı")


# =========================================================================== #
# O. Taze v2 ile göç edilmiş v2 yapısal olarak eşdeğer
# =========================================================================== #
class TestFreshMigratedEquivalence(MigrationTestCase):
    def test_fresh_and_migrated_v2_have_equivalent_structure(self):
        fresh = self.fresh_path("eq_fresh.db")
        migrated = self.migrated_path("eq_migrated.db")
        self.assertEqual(structure_signature(fresh), structure_signature(migrated))

    def test_migrated_empty_v1_is_equivalent_too(self):
        migrated = self.migrated_path("eq_empty.db", history=False)
        self.assertEqual(structure_signature(self.fresh_path("eq_fresh2.db")), structure_signature(migrated))


# =========================================================================== #
# P. Birden fazla SessionRepository başlatması
# =========================================================================== #
class TestMultipleRepositoryInitializations(MigrationTestCase):
    def test_repeated_repository_construction_is_safe_and_makes_no_extra_backups(self):
        path = self.v1_path()
        db = self.new_db(path, allow_migration=True)
        first = SessionRepository(db)
        names = [p.name for p in list_backups(path)[0]]
        self.assertEqual(len(names), 1)
        for _ in range(5):
            SessionRepository(db)
        for _ in range(3):
            SessionRepository(self.new_db(path))                       # yetkisiz örnek de v2'yi açar
        self.assertEqual([p.name for p in list_backups(path)[0]], names)
        self.assertEqual(snapshot(path)["table:schema_metadata"], [(2,)])
        self.assertEqual(len(first.list_sessions()), 3)

    def test_repository_works_normally_on_a_fresh_v2_database(self):
        repo = SessionRepository(self.new_db(self.tmp / "repo.db"))
        session_id = repo.create_session("Deneme")
        repo.append_segment(session_id, 1, "t0", "t1", "merhaba")
        self.assertEqual(repo.get_full_transcript_text(session_id), "merhaba")
        repo.delete_session(session_id)
        self.assertIsNone(repo.get_session(session_id))


# =========================================================================== #
# Q. Mevcut yabancı anahtar ihlali göçü reddettirir
# =========================================================================== #
class TestExistingForeignKeyViolationRefusal(MigrationTestCase):
    def test_pre_existing_violations_abort_migration_without_repair(self):
        cases = {
            "yetim segment": "INSERT INTO segments (session_id, sequence, started_at, ended_at, text, language, "
                             "created_at) VALUES ('hayalet', 1, 'a', 'b', 'yetim', NULL, 'c')",
            "yetim özet": "INSERT INTO summaries (session_id, kind, content, model, created_at) "
                          "VALUES ('hayalet', 'live', 'yetim', NULL, 'c')",
        }
        for label, statement in cases.items():
            with self.subTest(case=label):
                path = self.v1_path("fk_%d.db" % abs(hash(label)))
                tamper(path, statement, foreign_keys=False)
                exc = self.assert_unchanged_after(
                    path,
                    lambda p=path: self.new_db(p, allow_migration=True).init_schema(),
                    expected_exception=SchemaMigrationError,
                    reason="foreign_key_violations",
                )
                self.assertNotIsInstance(exc, SchemaVersionError)
                self.assertEqual(list_backups(path), ([], []), "ihlal varken yedek alınmamalı")
                self.assertTrue(any("yetim" in str(r) for r in snapshot(path)["table:segments"] + snapshot(path)["table:summaries"]),
                                "ihlalli satır onarılmadan yerinde kalmalı")


# =========================================================================== #
# R. Sürüm/şema uyuşmazlığı ve çakışmalar
# =========================================================================== #
class TestVersionSchemaMismatchRefusal(MigrationTestCase):
    def _refused(self, path, reason="inconsistent_schema", authorized=True):
        return self.assert_unchanged_after(
            path,
            lambda: self.new_db(path, allow_migration=authorized).init_schema(),
            expected_exception=SchemaVersionError,
            reason=reason,
        )

    def test_version_two_on_v1_structure_is_refused(self):
        for authorized in (False, True):
            with self.subTest(authorized=authorized):
                path = self.v1_path("claims_v2_%s.db" % authorized)
                tamper(path, "UPDATE schema_metadata SET schema_version = 2")
                exc = self._refused(path, authorized=authorized)
                self.assertIn("artifacts", str(exc))

    def test_version_two_missing_the_composite_parent_unique_index_is_refused(self):
        path = self.migrated_path("no_unique.db")
        tamper(path, "DROP INDEX idx_segments_id_session")
        exc = self._refused(path, authorized=False)
        self.assertIn("idx_segments_id_session", str(exc))

    def test_version_two_missing_artifacts_is_refused(self):
        path = self.migrated_path("no_artifacts.db")
        tamper(path, "DROP TABLE artifacts")
        exc = self._refused(path, authorized=False)
        self.assertIn("artifacts", str(exc))

    def test_version_two_with_wrong_structures_is_refused(self):
        artifacts_tail = "created_at TEXT NOT NULL"
        variants = {
            "bileşik FK yerine yalnızca tekil FK": (
                "DROP TABLE artifacts",
                "CREATE TABLE artifacts (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL "
                "REFERENCES sessions(id) ON DELETE CASCADE, kind TEXT NOT NULL, content TEXT NOT NULL, "
                "capture_method TEXT NOT NULL, source_segment_id INTEGER REFERENCES segments(id), "
                + artifacts_tail + ")"),
            "bileşik FK sütun eşleşmesi yanlış": (
                "DROP TABLE artifacts",
                "CREATE TABLE artifacts (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL "
                "REFERENCES sessions(id) ON DELETE CASCADE, kind TEXT NOT NULL, content TEXT NOT NULL, "
                "capture_method TEXT NOT NULL, source_segment_id INTEGER, " + artifacts_tail + ", "
                "FOREIGN KEY (session_id, source_segment_id) REFERENCES segments(session_id, sequence))"),
            "bileşik FK ON DELETE SET NULL": (
                "DROP TABLE artifacts",
                "CREATE TABLE artifacts (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL "
                "REFERENCES sessions(id) ON DELETE CASCADE, kind TEXT NOT NULL, content TEXT NOT NULL, "
                "capture_method TEXT NOT NULL, source_segment_id INTEGER, " + artifacts_tail + ", "
                "FOREIGN KEY (source_segment_id, session_id) REFERENCES segments(id, session_id) "
                "ON DELETE SET NULL)"),
            "benzersiz dizin benzersiz değil": (
                "DROP INDEX idx_segments_id_session",
                "CREATE INDEX idx_segments_id_session ON segments(id, session_id)"),
            "dizin sütun sırası ters": (
                "DROP INDEX idx_segments_id_session",
                "CREATE UNIQUE INDEX idx_segments_id_session ON segments(session_id, id)"),
            "artifacts fazladan sütun": ("ALTER TABLE artifacts ADD COLUMN ekstra TEXT",),
            "artifacts dizini eksik": ("DROP INDEX idx_artifacts_segment",),
            "artifacts NOT NULL eksik": (
                "DROP TABLE artifacts",
                "CREATE TABLE artifacts (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL "
                "REFERENCES sessions(id) ON DELETE CASCADE, kind TEXT, content TEXT NOT NULL, "
                "capture_method TEXT NOT NULL, source_segment_id INTEGER, " + artifacts_tail + ", "
                "FOREIGN KEY (source_segment_id, session_id) REFERENCES segments(id, session_id))"),
        }
        for label, statements in variants.items():
            with self.subTest(case=label):
                path = self.migrated_path("wrong_%d.db" % abs(hash(label)))
                tamper(path, *statements)
                self._refused(path, authorized=False)

    def test_v1_with_v2_named_objects_is_refused_before_backup(self):
        collisions = {
            "artifacts tablosu": "CREATE TABLE artifacts (legacy TEXT)",
            "idx_segments_id_session": "CREATE INDEX idx_segments_id_session ON segments(id)",
            "idx_artifacts_session": "CREATE INDEX idx_artifacts_session ON sessions(id)",
            "idx_artifacts_segment": "CREATE INDEX idx_artifacts_segment ON summaries(id)",
        }
        for label, statement in collisions.items():
            with self.subTest(collision=label):
                path = self.v1_path("collision_%d.db" % abs(hash(label)))
                tamper(path, statement)
                exc = self._refused(path, authorized=True)
                self.assertIn("çakışma", str(exc))
                self.assertEqual(list_backups(path), ([], []), "çakışmada yedek alınmamalı")

    def test_versioned_databases_may_contain_unrelated_extra_objects(self):
        extras = ("CREATE TABLE kullanici_notlari (x TEXT)", "CREATE INDEX kullanici_idx ON kullanici_notlari(x)",
                  "CREATE VIEW kullanici_goru AS SELECT id FROM sessions")
        v1 = self.v1_path("extras_v1.db")
        tamper(v1, *extras)
        self.new_db(v1, allow_migration=True).init_schema()
        self.assert_v2_structure(v1)
        self.assertIn(("table", "kullanici_notlari"), snapshot(v1)["objects"])
        v2 = self.migrated_path("extras_v2.db")
        tamper(v2, *extras)
        before = snapshot(v2)
        self.new_db(v2).init_schema()
        self.assertEqual(snapshot(v2), before)

    def test_pragma_user_version_is_not_a_competing_version_authority(self):
        v2 = self.migrated_path("uv_v2.db")
        tamper(v2, "PRAGMA user_version = 77")
        before = snapshot(v2)
        self.new_db(v2).init_schema()                   # v2 sayılır; user_version yok sayılır
        self.assertEqual(snapshot(v2), before)
        v1 = self.v1_path("uv_v1.db")
        tamper(v1, "PRAGMA user_version = 5")
        self.new_db(v1, allow_migration=True).init_schema()
        conn = sqlite3.connect(str(v1))
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 5, "göç user_version'a dokunmamalı")
        finally:
            conn.close()
        self.assert_v2_structure(v1)


# =========================================================================== #
# Kilit zaman aşımı ve kilitsiz hızlı yol
# =========================================================================== #
class TestLockBehaviour(MigrationTestCase):
    def test_lock_timeout_is_reported_without_changes_or_backup(self):
        path = self.v1_path()
        holder = sqlite3.connect(str(path))
        try:
            holder.execute("PRAGMA journal_mode=WAL")
            holder.execute("BEGIN IMMEDIATE")
            db = self.new_db(path, allow_migration=True)
            before = snapshot(path)
            with mock.patch.object(storage, "MIGRATION_BUSY_TIMEOUT_SEC", 0.3):
                started = time.monotonic()
                with self.assertRaises(SchemaMigrationError) as ctx:
                    db.init_schema()
                self.assertLess(time.monotonic() - started, 20)
            self.assertEqual(ctx.exception.reason, "lock_timeout")
            self.assertNotIsInstance(ctx.exception, SchemaVersionError)
            self.assertFalse(db._local.schema_ready)
            self.assertIsNone(db._local.conn)
        finally:
            holder.rollback()
            holder.close()
        self.assertEqual(snapshot(path), before)
        self.assertEqual(list_backups(path), ([], []))

    def test_opening_valid_v2_never_takes_the_write_lock(self):
        path = self.migrated_path()
        holder = sqlite3.connect(str(path))
        try:
            holder.execute("PRAGMA journal_mode=WAL")
            holder.execute("BEGIN IMMEDIATE")
            with mock.patch.object(storage, "MIGRATION_BUSY_TIMEOUT_SEC", 0.01):
                self.new_db(path).init_schema()          # yazma kilidi beklenseydi hata verirdi
        finally:
            holder.rollback()
            holder.close()


# =========================================================================== #
# Hata sınıfları ve statik korumalar
# =========================================================================== #
class TestStalePreliminaryInspection(MigrationTestCase):
    """Kilitsiz ön inceleme BAYAT olabilir (beklerken başka süreç durumu değiştirmiş olabilir);
    karar her zaman yazma kilidi ALTINDAKİ yeniden sınıflandırmaya göre verilir."""

    @staticmethod
    def _stale_fresh(self_db):
        return storage._SchemaState(storage._STATE_FRESH, "bayat ön inceleme")

    def test_unauthorized_instance_refuses_under_the_lock_even_if_the_first_look_said_fresh(self):
        path = self.v1_path()
        ddl_calls = []
        real_ddl = Database._execute_ddl_statement

        def counting_ddl(db, conn, statement):
            ddl_calls.append(statement)
            return real_ddl(db, conn, statement)

        with mock.patch.object(Database, "_inspect_readonly", self._stale_fresh), \
                mock.patch.object(Database, "_execute_ddl_statement", counting_ddl):
            self.assert_unchanged_after(
                path, lambda: self.new_db(path).init_schema(),
                expected_exception=SchemaMigrationError, reason="authorization_required",
            )
        self.assertEqual(ddl_calls, [], "yetkisiz örnek kilit altında göçe BAŞLAMAMALI")
        self.assertEqual(list_backups(path), ([], []))

    def test_authorized_instance_migrates_a_v1_database_it_wrongly_saw_as_fresh(self):
        path = self.v1_path()
        original = snapshot(path)
        with mock.patch.object(Database, "_inspect_readonly", self._stale_fresh):
            self.new_db(path, allow_migration=True).init_schema()
        self.assert_v2_structure(path)
        finals, _ = list_backups(path)
        self.assertEqual(len(finals), 1, "taze kurulum sanılsa bile gerçek durum v1: yedek + göç yapılmalı")
        self.assert_valid_v1_backup(finals[0], original)
        self.assertEqual(history_part(snapshot(path))["table:sessions"], original["table:sessions"])

    def test_instance_that_saw_v1_returns_quietly_when_the_database_is_already_v2(self):
        path = self.migrated_path()
        backups = [p.name for p in list_backups(path)[0]]
        before = snapshot(path)
        stale_v1 = lambda self_db: storage._SchemaState(storage._STATE_V1, "bayat", 1)
        with mock.patch.object(Database, "_inspect_readonly", stale_v1):
            self.new_db(path, allow_migration=True).init_schema()
        self.assertEqual([p.name for p in list_backups(path)[0]], backups, "ikinci süreç yeni yedek almamalı")
        self.assertEqual(snapshot(path), before)


class TestFailureLogging(MigrationTestCase):
    def test_refusals_and_failures_are_logged_at_error_level_with_their_reason(self):
        v1 = self.v1_path("log_v1.db")
        with self.assertLogs("storage", level="ERROR") as captured:
            with self.assertRaises(SchemaMigrationError):
                self.new_db(v1).init_schema()
        self.assertTrue(any("authorization_required" in line for line in captured.output), captured.output)
        newer = self.v1_path("log_newer.db")
        tamper(newer, "UPDATE schema_metadata SET schema_version = 99")
        with self.assertLogs("storage", level="ERROR") as captured:
            with self.assertRaises(SchemaVersionError):
                self.new_db(newer, allow_migration=True).init_schema()
        self.assertTrue(any("newer_schema" in line for line in captured.output), captured.output)
        failing = self.v1_path("log_ddl.db")
        wrapper, _ = failing_ddl(2)
        with mock.patch.object(Database, "_execute_ddl_statement", wrapper):
            with self.assertLogs("storage", level="ERROR") as captured:
                with self.assertRaises(SchemaMigrationError):
                    self.new_db(failing, allow_migration=True).init_schema()
        self.assertTrue(any("migration_ddl_failed" in line for line in captured.output), captured.output)


class TestExceptionHierarchy(unittest.TestCase):
    def test_hierarchy_and_reasons(self):
        self.assertTrue(issubclass(SchemaMigrationError, RuntimeError))
        self.assertTrue(issubclass(SchemaVersionError, SchemaMigrationError))
        self.assertEqual(SchemaMigrationError("x").reason, "migration_failed")
        self.assertEqual(SchemaVersionError("x").reason, "inconsistent_schema")
        self.assertEqual(SchemaVersionError("x", reason="newer_schema").reason, "newer_schema")
        reasons = {
            storage.REASON_AUTHORIZATION_REQUIRED, storage.REASON_NEWER_SCHEMA, storage.REASON_INCONSISTENT_SCHEMA,
            storage.REASON_FK_VIOLATIONS, storage.REASON_LOCK_TIMEOUT, storage.REASON_BACKUP_FAILED,
            storage.REASON_DDL_FAILED,
            storage.REASON_ROLLBACK_FAILED, storage.REASON_COMMIT_FAILED, storage.REASON_CONNECTION_SETUP_FAILED,
        }
        self.assertEqual(len(reasons), 10, "ayırt edilmesi gereken kategoriler benzersiz olmalı")


class TestStaticGuards(unittest.TestCase):
    @staticmethod
    def _parse(path):
        source = Path(path).read_text(encoding="utf-8")
        return source, ast.parse(source, filename=str(path))

    def test_storage_never_uses_executescript_user_version_or_immutable(self):
        source, tree = self._parse(storage.__file__)
        self.assertEqual([n.lineno for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "executescript"], [])
        executed = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "execute":
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    executed.append(node.args[0].value.lower())
        self.assertFalse([s for s in executed if "user_version" in s], "ikinci sürüm otoritesi yazılmamalı")
        self.assertNotIn("immutable=1", source.lower())

    def test_storage_imports_only_the_standard_library(self):
        _, tree = self._parse(storage.__file__)
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.add((node.module or "").split(".")[0])
        self.assertTrue(modules <= {"__future__", "datetime", "hashlib", "logging", "os", "pathlib", "secrets",
                                    "sqlite3", "tempfile", "threading", "time", "typing"}, modules)

    def test_this_test_module_has_no_production_database_dependency(self):
        _, tree = self._parse(__file__)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        self.assertFalse({m for m in imported if m.startswith("vesper")} - {"vesper", "vesper.repository", "vesper.storage"})
        self.assertNotIn("vesper.engine", imported)
        self.assertNotIn("vesper.config", imported)


class TestReadonlyFailClosed(MigrationTestCase):
    """Salt-okunur açma başarısızsa yazılabilir yola geçilmediğini doğrular."""

    def test_connect_failure_never_retries_with_a_writable_connection(self):
        path = self.v1_path()
        for message in ("unable to open database file", "database is locked", "database is busy"):
            with self.subTest(message=message):
                error = sqlite3.OperationalError(message)
                with mock.patch.object(storage.sqlite3, "connect", side_effect=error) as connect:
                    with self.assertRaises(sqlite3.OperationalError) as caught:
                        storage._open_readonly_connection(path, 0.25)
                self.assertIs(caught.exception, error)
                connect.assert_called_once_with(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.25)

    def test_failed_schema_probe_closes_connection_without_retry(self):
        conn = mock.Mock()
        error = sqlite3.DatabaseError("enjekte edilen okuma hatası")
        conn.execute.side_effect = error
        with mock.patch.object(storage.sqlite3, "connect", return_value=conn) as connect:
            with self.assertRaises(sqlite3.DatabaseError) as caught:
                storage._open_readonly_connection(self.tmp / "probe.db", 1.0)
        self.assertIs(caught.exception, error)
        connect.assert_called_once()
        conn.close.assert_called_once_with()

    def test_nonexistent_readonly_target_is_not_created(self):
        path = self.tmp / "missing.db"
        with self.assertRaises(sqlite3.OperationalError):
            storage._open_readonly_connection(path, 1.0)
        self.assertFalse(path.exists())
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_successful_readonly_connection_rejects_writes(self):
        path = self.v1_path()
        conn = storage._open_readonly_connection(path, 1.0)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("CREATE TABLE forbidden (x TEXT)")
        finally:
            conn.close()

    def test_inspection_failure_preserves_categories_and_blocks_writable_preparation(self):
        path = self.v1_path()
        before = snapshot(path)
        cases = ((sqlite3.OperationalError("unable to open database file"), SchemaVersionError, "unreadable"),
                 (sqlite3.OperationalError("database is locked"), SchemaMigrationError, "lock_timeout"),
                 (sqlite3.DatabaseError("file is not a database"), SchemaVersionError, "inconsistent_schema"))
        for error, error_type, reason in cases:
            with self.subTest(reason=reason):
                db = self.new_db(path, allow_migration=True)
                with mock.patch.object(storage.sqlite3, "connect", side_effect=error) as connect, \
                        mock.patch.object(db, "_get_raw_connection") as raw, \
                        mock.patch.object(db, "_create_backup") as backup, \
                        mock.patch.object(db, "_execute_ddl_statement") as ddl:
                    with self.assertRaises(error_type) as caught:
                        db.get_connection()
                self.assertEqual(caught.exception.reason, reason)
                connect.assert_called_once()
                raw.assert_not_called()
                backup.assert_not_called()
                ddl.assert_not_called()
                self.assertFalse(db._local.schema_ready)
        self.assertEqual(snapshot(path), before)
        self.assertEqual(list_backups(path), ([], []))


class TestPublicConnectionGate(MigrationTestCase):
    """Herkese açık bağlantının şema ve örnek-yerel izin kapısını atlayamadığını sınar."""

    def test_direct_unauthorized_v1_and_legacy_access_never_opens_writable_connection(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                path = self.v1_path("direct_%s.db" % legacy)
                if legacy:
                    tamper(path, "DELETE FROM schema_metadata")
                db = self.new_db(path)
                with mock.patch.object(db, "_get_raw_connection") as raw:
                    for action in (db.get_connection, db.init_schema, db.get_connection):
                        self.assert_unchanged_after(path, action, reason="authorization_required")
                raw.assert_not_called()
                self.assertIsNone(getattr(db._local, "conn", None))
                self.assertEqual(list_backups(path), ([], []))

    def test_direct_access_refuses_newer_inconsistent_and_corrupt_databases(self):
        for kind in ("newer", "inconsistent", "corrupt"):
            with self.subTest(kind=kind):
                path = self.v1_path(kind + ".db")
                if kind == "newer":
                    tamper(path, "UPDATE schema_metadata SET schema_version=99")
                elif kind == "inconsistent":
                    tamper(path, "DROP INDEX idx_segments_session")
                else:
                    path.write_bytes(b"bozuk SQLite dosyasi" * 100)
                before = sha256_file(path)
                db = self.new_db(path, allow_migration=True)
                with mock.patch.object(db, "_get_raw_connection") as raw:
                    with self.assertRaises(SchemaVersionError):
                        db.get_connection()
                raw.assert_not_called()
                self.assertEqual(sha256_file(path), before)

    def test_direct_authorized_access_migrates_once_and_reuses_prepared_connection(self):
        path = self.v1_path()
        db = self.new_db(path, allow_migration=True)
        with mock.patch.object(db, "_inspect_readonly", wraps=db._inspect_readonly) as inspect:
            conn = db.get_connection()
            self.assertIs(db.get_connection(), conn)
            inspect.assert_called_once_with()
        self.assert_v2_structure(path)
        self.assertEqual(len(list_backups(path)[0]), 1)
        self.assertTrue(db._local.schema_ready)

    def test_direct_fresh_and_valid_v2_access_preserves_connection_settings(self):
        paths = (self.tmp / "direct_fresh.db", self.fresh_path(), self.migrated_path())
        for path in paths:
            with self.subTest(path=path.name):
                db = self.new_db(path)
                conn = db.get_connection()
                self.assertIs(conn.row_factory, sqlite3.Row)
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
                self.assert_v2_structure(path)

    def test_explicit_initialization_always_revalidates_and_revokes_cached_readiness(self):
        path = self.fresh_path()
        db = self.new_db(path)
        conn = db.get_connection()
        with mock.patch.object(db, "_inspect_readonly", wraps=db._inspect_readonly) as inspect:
            db.init_schema()
            db.init_schema()
            self.assertEqual(inspect.call_count, 2)
        tamper(path, "UPDATE schema_metadata SET schema_version=99")
        with self.assertRaises(SchemaVersionError):
            db.init_schema()
        self.assertFalse(db._local.schema_ready)
        with self.assertRaises(SchemaVersionError):
            db.get_connection()
        self.assertIsNone(db._local.conn)
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def test_close_requires_revalidation_and_a_new_connection(self):
        db = self.new_db(self.tmp / "close.db")
        old = db.get_connection()
        db.close()
        self.assertFalse(db._local.schema_ready)
        with self.assertRaises(sqlite3.ProgrammingError):
            old.execute("SELECT 1")
        with mock.patch.object(db, "_inspect_readonly", wraps=db._inspect_readonly) as inspect:
            new = db.get_connection()
            inspect.assert_called_once_with()
        self.assertIsNot(old, new)
        self.assertIs(db.get_connection(), new)

    def test_preparation_failure_cannot_expose_raw_connection_and_retry_is_explicitly_gated(self):
        path = self.v1_path()
        db = self.new_db(path, allow_migration=True)
        wrapper, calls = failing_ddl(2)
        with mock.patch.object(Database, "_execute_ddl_statement", wrapper):
            with self.assertRaises(SchemaMigrationError):
                db.get_connection()
        self.assertFalse(db._local.schema_ready)
        with mock.patch.object(db, "_inspect_readonly", side_effect=SchemaVersionError("enjekte")) as inspect:
            with self.assertRaises(SchemaVersionError):
                db.get_connection()
            inspect.assert_called_once_with()
        self.assertEqual(snapshot(path)["table:schema_metadata"], [(1,)])
        db.get_connection()
        self.assert_v2_structure(path)

    def test_readiness_and_connections_are_thread_local_on_a_shared_instance(self):
        db = self.new_db(self.tmp / "threads.db")
        main_conn = db.get_connection()
        results, errors = [], []

        def worker():
            try:
                self.assertFalse(getattr(db._local, "schema_ready", False))
                conn = db.get_connection()
                self.assertIs(conn, db.get_connection())
                self.assertIsNot(conn, main_conn)
                results.append(conn)
            except BaseException as exc:
                errors.append(exc)
            finally:
                db.close()

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(20)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertTrue(db._local.schema_ready)
        self.assertIs(db.get_connection(), main_conn)

    def test_raw_connection_setup_failure_closes_handle_and_leaves_readiness_false(self):
        db = self.new_db(self.fresh_path())
        real_inspect = db._inspect_readonly
        state = real_inspect()
        conn = mock.Mock()
        conn.execute.side_effect = sqlite3.OperationalError("enjekte edilen PRAGMA hatası")
        with mock.patch.object(db, "_inspect_readonly", return_value=state), \
                mock.patch.object(storage.sqlite3, "connect", return_value=conn):
            with self.assertRaises(SchemaMigrationError) as caught:
                db.get_connection()
        self.assertEqual(caught.exception.reason, "connection_setup_failed")
        self.assertIsInstance(caught.exception.__cause__, sqlite3.OperationalError)
        conn.close.assert_called_once_with()
        self.assertFalse(db._local.schema_ready)
        self.assertIsNone(getattr(db._local, "conn", None))


class TestExclusiveBackupOwnership(MigrationTestCase):
    """Yalnızca bu denemenin özel geçici alanının temizlendiğini doğrular."""

    def test_backup_file_is_exclusively_reserved_inside_unique_staging_directory(self):
        path = self.v1_path()
        real_copy = storage._online_backup_copy
        observed = []

        def checking_copy(src, dst):
            self.assertEqual(dst.parent.parent, path.parent / "backups")
            self.assertTrue(dst.parent.name.startswith(".vesper-backup-"))
            self.assertEqual(dst.read_bytes(), b"")
            with self.assertRaises(FileExistsError):
                with open(dst, "xb"):
                    pass
            observed.append(dst.parent)
            return real_copy(src, dst)

        with mock.patch.object(storage, "_online_backup_copy", checking_copy):
            self.new_db(path, allow_migration=True).init_schema()
        self.assertEqual(len(observed), 1)
        self.assertFalse(observed[0].exists())
        finals, others = list_backups(path)
        self.assertEqual((len(finals), others), (1, []))
        self.assertEqual(finals[0].parent, path.parent / "backups")

    def test_legacy_exact_partial_sidecars_and_crash_staging_are_preserved(self):
        path = self.v1_path()
        backup_dir = path.parent / "backups"
        backup_dir.mkdir()
        stamp = "20260101T000000000000Z"
        name = "%s.pre-v2.%s.deadbeef.db" % (path.stem, stamp)
        partial = backup_dir / (name + ".partial")
        foreign = [Path(str(partial) + s) for s in ("", "-wal", "-shm", "-journal")]
        stranded = backup_dir / ".vesper-backup-crash"
        stranded.mkdir()
        foreign.append(stranded / (name + ".partial"))
        for item in foreign:
            item.write_bytes(b"onceden var olan kullanici verisi")
        with mock.patch.object(storage.datetime, "datetime") as date, \
                mock.patch.object(storage.secrets, "token_hex", return_value="deadbeef"):
            date.now.return_value.strftime.return_value = stamp
            self.new_db(path, allow_migration=True).init_schema()
        for item in foreign:
            self.assertEqual(item.read_bytes(), b"onceden var olan kullanici verisi")
        self.assertTrue((backup_dir / name).exists())

    def test_failed_staging_allocation_does_not_clean_preexisting_directory(self):
        path = self.v1_path()
        stranded = path.parent / "backups" / ".vesper-backup-existing"
        stranded.mkdir(parents=True)
        partial = stranded / "existing.partial"
        partial.write_bytes(b"koru")
        with mock.patch.object(storage.tempfile, "mkdtemp", side_effect=FileExistsError("enjekte")), \
                mock.patch.object(storage, "_cleanup_partial") as cleanup:
            with self.assertRaises(SchemaMigrationError) as caught:
                self.new_db(path, allow_migration=True).init_schema()
        self.assertEqual(caught.exception.reason, "backup_failed")
        cleanup.assert_not_called()
        self.assertEqual(partial.read_bytes(), b"koru")
        self.assertEqual(snapshot(path)["table:schema_metadata"], [(1,)])

    def test_failed_exclusive_file_creation_never_adopts_existing_file_or_sidecars(self):
        for collision in ("partial", "sidecars", "both"):
            with self.subTest(collision=collision):
                path = self.v1_path("exclusive_%s.db" % collision)
                real_mkdtemp = storage.tempfile.mkdtemp
                foreign = []

                def occupied_staging(*args, **kwargs):
                    directory = real_mkdtemp(*args, **kwargs)
                    name = "%s.pre-v2.20260101T000000000000Z.deadbeef.db.partial" % path.stem
                    suffixes = {"partial": ("",), "sidecars": ("-wal", "-shm", "-journal"),
                                "both": ("", "-wal", "-shm", "-journal")}[collision]
                    for suffix in suffixes:
                        item = Path(directory) / (name + suffix)
                        item.write_bytes(b"yabanci dosya")
                        foreign.append(item)
                    return directory

                with mock.patch.object(storage.datetime, "datetime") as date, \
                        mock.patch.object(storage.secrets, "token_hex", return_value="deadbeef"), \
                        mock.patch.object(storage.tempfile, "mkdtemp", side_effect=occupied_staging), \
                        mock.patch.object(storage, "_cleanup_partial") as cleanup:
                    date.now.return_value.strftime.return_value = "20260101T000000000000Z"
                    with self.assertRaises(SchemaMigrationError):
                        self.new_db(path, allow_migration=True).init_schema()
                cleanup.assert_not_called()
                for item in foreign:
                    self.assertEqual(item.read_bytes(), b"yabanci dosya")

    def test_copy_failure_cleans_only_owned_partial_and_sidecars(self):
        path = self.v1_path()
        observed = []

        def failing_copy(src, dst):
            observed.append(dst.parent)
            for suffix in ("", "-wal", "-shm", "-journal"):
                Path(str(dst) + suffix).write_bytes(b"bu denemenin dosyasi")
            raise OSError("enjekte")

        with mock.patch.object(storage, "_online_backup_copy", failing_copy):
            with self.assertRaises(SchemaMigrationError):
                self.new_db(path, allow_migration=True).init_schema()
        self.assertFalse(observed[0].exists())
        self.assertEqual(list_backups(path), ([], []))
        self.assertEqual(snapshot(path)["table:schema_metadata"], [(1,)])

    def test_final_name_collision_preserves_destination_and_cleans_owned_staging(self):
        path = self.v1_path()
        real_copy = storage._online_backup_copy
        finals = []

        def colliding_copy(src, dst):
            real_copy(src, dst)
            final = dst.parent.parent / dst.name[:-len(".partial")]
            final.write_bytes(b"mevcut final yedek")
            finals.append(final)

        with mock.patch.object(storage, "_online_backup_copy", colliding_copy):
            with self.assertRaises(SchemaMigrationError):
                self.new_db(path, allow_migration=True).init_schema()
        self.assertEqual(finals[0].read_bytes(), b"mevcut final yedek")
        self.assertEqual(list_backups(path)[1], [])
        self.assertEqual(snapshot(path)["table:schema_metadata"], [(1,)])

    def test_rename_sharing_violation_is_retried_without_losing_backup(self):
        path = self.v1_path()
        real_rename = storage.os.rename
        attempts = []

        def transient_rename(src, dst):
            attempts.append(src)
            if len(attempts) == 1:
                raise PermissionError("enjekte edilen Windows paylaşım ihlali")
            return real_rename(src, dst)

        with mock.patch.object(storage.os, "rename", side_effect=transient_rename), \
                mock.patch.object(storage.time, "sleep"):
            self.new_db(path, allow_migration=True).init_schema()
        self.assertEqual(len(attempts), 2)
        self.assertEqual((len(list_backups(path)[0]), list_backups(path)[1]), (1, []))

    def test_fsync_and_exhausted_rename_failures_block_ddl_and_clean_staging(self):
        for operation in ("fsync", "rename"):
            with self.subTest(operation=operation):
                path = self.v1_path(operation + ".db")
                original = snapshot(path)
                with mock.patch.object(storage.os, operation, side_effect=PermissionError("enjekte")), \
                        mock.patch.object(storage.time, "sleep"), \
                        mock.patch.object(Database, "_execute_ddl_statement") as ddl:
                    with self.assertRaises(SchemaMigrationError) as caught:
                        self.new_db(path, allow_migration=True).init_schema()
                self.assertEqual(caught.exception.reason, "backup_failed")
                ddl.assert_not_called()
                self.assertEqual(snapshot(path), original)
                self.assertEqual(list_backups(path), ([], []))

    def test_final_verification_failure_keeps_completed_backup(self):
        path = self.v1_path()
        real_verify = storage._verify_backup_file

        def failing_final(path, counts, digests, metadata, readonly):
            if readonly:
                return ["enjekte edilen final doğrulama hatası"]
            return real_verify(path, counts, digests, metadata, readonly)

        with mock.patch.object(storage, "_verify_backup_file", side_effect=failing_final):
            with self.assertRaises(SchemaMigrationError):
                self.new_db(path, allow_migration=True).init_schema()
        finals, others = list_backups(path)
        self.assertEqual((len(finals), others), (1, []))
        self.assertEqual(snapshot(finals[0])["table:schema_metadata"], [(1,)])
        self.assertEqual(snapshot(path)["table:schema_metadata"], [(1,)])


class LifecycleFaultConnection(sqlite3.Connection):
    """Gerçek SQLite üzerinde yalnızca seçilen yaşam döngüsü adımına hata enjekte eder."""

    @property
    def in_transaction(self):
        if getattr(self, "state_error", None) is not None:
            raise self.state_error
        return super().in_transaction

    def execute(self, sql, parameters=()):
        self.events.append(sql)
        if sql == self.faults.get("fail_sql"):
            raise self.faults["setup_error"]
        if (getattr(self, "commit_completed", False) and sql == "PRAGMA busy_timeout=5000"
                and self.faults.get("restore_error") is not None):
            raise self.faults["restore_error"]
        cursor = super().execute(sql, parameters)
        if (getattr(self, "commit_completed", False) and sql == "PRAGMA busy_timeout"
                and self.faults.get("restore_wrong_value") is not None):
            return mock.Mock(fetchone=lambda: (self.faults["restore_wrong_value"],))
        if sql == self.faults.get("wrong_sql"):
            return mock.Mock(fetchone=lambda: (self.faults["wrong_value"],))
        return cursor

    def commit(self):
        self.events.append("commit")
        mode = self.faults.get("commit_mode")
        if mode == "before":
            raise self.faults["commit_error"]
        if mode == "noop":
            return
        if mode == "auto_rollback":
            super().rollback()
            raise self.faults["commit_error"]
        super().commit()
        self.commit_completed = True
        if mode == "after":
            raise self.faults["commit_error"]

    def rollback(self):
        self.events.append("rollback")
        if self.faults.get("rollback_error") is not None:
            raise self.faults["rollback_error"]
        if not self.faults.get("rollback_noop"):
            super().rollback()

    def backup(self, target, **kwargs):
        if self.faults.get("backup_error") is not None:
            raise self.faults["backup_error"]
        return super().backup(target, **kwargs)

    def close(self):
        self.events.append("close")
        callback = self.faults.get("on_close")
        if callback is not None:
            callback()
        if self.faults.get("close_error") is not None:
            raise self.faults["close_error"]
        super().close()


class LifecycleFaultMixin:
    """Yaşam döngüsü testlerinin bağlantı kurma ve iptal doğrulama yardımcıları."""

    def fault_factory(self, faults):
        real_connect = sqlite3.connect
        connections = []

        def connect(*args, **kwargs):
            if kwargs.get("uri") or kwargs.get("timeout") != storage._NORMAL_TIMEOUT_SEC:
                return real_connect(*args, **kwargs)
            kwargs["factory"] = LifecycleFaultConnection
            conn = real_connect(*args, **kwargs)
            conn.faults = faults
            conn.events = []
            connections.append(conn)
            # Enjekte edilen close hatası gerçek dosya tanıtıcısını açık bırakmasın.
            self.addCleanup(sqlite3.Connection.close, conn)
            return conn

        return connect, connections

    def assert_invalidated(self, db, conn=None):
        self.assertIsNone(getattr(db._local, "conn", None))
        self.assertFalse(db._local.schema_ready)
        if conn is not None:
            with self.assertRaises(sqlite3.ProgrammingError):
                sqlite3.Connection.execute(conn, "SELECT 1")


class TestConnectionLifecyclePackageTwo(LifecycleFaultMixin, MigrationTestCase):
    """İptal, kapalı ham bağlantı ve kurulum hatalarını geçici dosyalarda sınar."""

    def test_invalidation_detaches_cache_before_close_and_preserves_close_failure(self):
        path = self.fresh_path()
        db = self.new_db(path)
        close_error = sqlite3.OperationalError("kapatma hatası")
        observed = []

        def on_close():
            observed.append((db._local.conn, db._local.schema_ready))

        faults = {"close_error": close_error, "on_close": on_close}
        connect, connections = self.fault_factory(faults)
        with mock.patch.object(storage.sqlite3, "connect", side_effect=connect):
            conn = db.get_connection()
            primary = SchemaVersionError("asıl hata", reason="newer_schema")
            with mock.patch.object(db, "_inspect_readonly", side_effect=primary):
                with self.assertRaises(SchemaVersionError) as caught:
                    db.init_schema()
        self.assertIs(caught.exception, primary)
        self.assertIs(primary.close_error, close_error)
        self.assertEqual(observed, [(None, False)])
        self.assert_invalidated(db)
        self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)
        faults.clear()
        sqlite3.Connection.close(conn)

    def test_explicit_close_failure_still_detaches_thread_local_connection(self):
        db = self.new_db(self.fresh_path())
        error = sqlite3.OperationalError("kapatma hatası")
        faults = {"close_error": error}
        connect, connections = self.fault_factory(faults)
        with mock.patch.object(storage.sqlite3, "connect", side_effect=connect):
            db.get_connection()
        with self.assertRaises(sqlite3.OperationalError) as caught:
            db.close()
        self.assertIs(caught.exception, error)
        self.assert_invalidated(db)
        faults.clear()
        sqlite3.Connection.close(connections[0])

    def test_failed_revalidation_closes_handle_and_discards_active_application_writes(self):
        path = self.fresh_path()
        db = self.new_db(path)
        conn = db.get_connection()
        tamper(path, "UPDATE schema_metadata SET schema_version=99")
        conn.execute("INSERT INTO schema_metadata VALUES (42)")
        self.assertTrue(conn.in_transaction)
        with self.assertRaises(SchemaVersionError) as caught:
            db.init_schema()
        self.assertEqual(caught.exception.reason, "newer_schema")
        self.assert_invalidated(db, conn)
        self.assertEqual(snapshot(path)["table:schema_metadata"], [(99,)])
        self.assertIn("Göç DDL'i çalıştırılmadı", str(caught.exception))
        self.assertIn("WAL yan dosyaları", str(caught.exception))
        self.assertIn("uygulama yazılarını kaybettirebilir", str(caught.exception))
        self.assertNotIn("DEĞİŞTİRİLMEDİ", str(caught.exception))
        with self.assertRaises(SchemaVersionError):
            db.get_connection()
        tamper(path, "UPDATE schema_metadata SET schema_version=2")
        new = db.get_connection()
        self.assertIsNot(new, conn)

    def test_all_revalidation_errors_close_previously_returned_handles(self):
        for reason in ("authorization_required", "inconsistent_schema", "unreadable"):
            with self.subTest(reason=reason):
                db = self.new_db(self.fresh_path(reason + ".db"))
                conn = db.get_connection()
                error = SchemaMigrationError("enjekte", reason=reason)
                with mock.patch.object(db, "_inspect_readonly", side_effect=error):
                    with self.assertRaises(SchemaMigrationError) as caught:
                        db.init_schema()
                self.assertIs(caught.exception, error)
                self.assert_invalidated(db, conn)

    def test_external_close_reopens_only_after_schema_revalidation(self):
        db = self.new_db(self.fresh_path())
        old = db.get_connection()
        old.close()
        with mock.patch.object(db, "_inspect_readonly", wraps=db._inspect_readonly) as inspect:
            new = db.get_connection()
        inspect.assert_called_once_with()
        self.assertIsNot(new, old)
        self.assertEqual(new.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(new.execute("PRAGMA busy_timeout").fetchone()[0], 5000)

    def test_external_close_does_not_bypass_schema_rejection(self):
        path = self.fresh_path()
        db = self.new_db(path)
        db.get_connection().close()
        tamper(path, "UPDATE schema_metadata SET schema_version=99")
        with self.assertRaises(SchemaVersionError) as caught:
            db.get_connection()
        self.assertEqual(caught.exception.reason, "newer_schema")
        self.assert_invalidated(db)

    def test_explicit_init_recovers_a_directly_closed_cached_handle(self):
        db = self.new_db(self.fresh_path())
        old = db.get_connection()
        old.close()
        db.init_schema()
        self.assertIsNot(db.get_connection(), old)
        self.assertTrue(db._local.schema_ready)

    def test_live_reuse_runs_no_sql_or_revalidation_and_preserves_active_transaction(self):
        db = self.new_db(self.fresh_path())
        conn = db.get_connection()
        conn.execute("BEGIN IMMEDIATE")
        statements = []
        conn.set_trace_callback(statements.append)
        try:
            with mock.patch.object(db, "_inspect_readonly") as inspect:
                self.assertIs(db.get_connection(), conn)
                self.assertIs(db.get_connection(), conn)
            inspect.assert_not_called()
            self.assertEqual(statements, [])
            self.assertTrue(conn.in_transaction)
        finally:
            conn.set_trace_callback(None)
            conn.rollback()

    def test_setup_failures_at_each_pragma_close_uncached_connection(self):
        stages = (("PRAGMA journal_mode=WAL", "journal_mode"),
                  ("PRAGMA foreign_keys=ON", "foreign_keys"),
                  ("PRAGMA foreign_keys", "foreign_keys"),
                  ("PRAGMA busy_timeout=5000", "normal_timeout"),
                  ("PRAGMA busy_timeout", "normal_timeout"))
        for sql, phase in stages:
            with self.subTest(sql=sql):
                db = self.new_db(self.fresh_path("setup_%d.db" % len(self._databases)))
                primary = sqlite3.OperationalError("kurulum hatası")
                faults = {"fail_sql": sql, "setup_error": primary}
                connect, connections = self.fault_factory(faults)
                with mock.patch.object(storage.sqlite3, "connect", side_effect=connect):
                    with self.assertRaises(SchemaMigrationError) as caught:
                        db.get_connection()
                self.assertEqual(caught.exception.reason, "connection_setup_failed")
                self.assertEqual(caught.exception.phase, phase)
                self.assertIs(caught.exception.__cause__, primary)
                self.assertNotIn("DEĞİŞTİRİLMEDİ", str(caught.exception))
                self.assertEqual(connections[0].events.count("close"), 1)
                self.assert_invalidated(db, connections[0])

    def test_unapplied_connection_settings_fail_closed(self):
        for sql, value, phase in (("PRAGMA journal_mode=WAL", "delete", "journal_mode"),
                                  ("PRAGMA foreign_keys", 0, "foreign_keys"),
                                  ("PRAGMA busy_timeout", 1, "normal_timeout")):
            with self.subTest(sql=sql):
                db = self.new_db(self.fresh_path("wrong_%d.db" % len(self._databases)))
                connect, connections = self.fault_factory({"wrong_sql": sql, "wrong_value": value})
                with mock.patch.object(storage.sqlite3, "connect", side_effect=connect):
                    with self.assertRaises(SchemaMigrationError) as caught:
                        db.get_connection()
                self.assertEqual(caught.exception.reason, "connection_setup_failed")
                self.assertEqual(caught.exception.phase, phase)
                self.assert_invalidated(db, connections[0])

    def test_setup_close_error_does_not_mask_original_setup_failure(self):
        db = self.new_db(self.fresh_path())
        primary = sqlite3.OperationalError("PRAGMA hatası")
        secondary = sqlite3.OperationalError("kapatma hatası")
        faults = {"fail_sql": "PRAGMA foreign_keys=ON", "setup_error": primary, "close_error": secondary}
        connect, connections = self.fault_factory(faults)
        with mock.patch.object(storage.sqlite3, "connect", side_effect=connect):
            with self.assertRaises(SchemaMigrationError) as caught:
                db.get_connection()
        self.assertIs(caught.exception.__cause__, primary)
        self.assertIs(caught.exception.close_error, secondary)
        self.assert_invalidated(db)
        faults.clear()
        sqlite3.Connection.close(connections[0])

    def test_directory_and_connect_failures_are_classified_without_file_unchanged_claim(self):
        blocker = self.tmp / "not_a_directory"
        blocker.write_bytes(b"koru")
        db = self.new_db(blocker / "child.db")
        with mock.patch.object(db, "_inspect_readonly", return_value=storage._SchemaState(storage._STATE_FRESH)):
            with self.assertRaises(SchemaMigrationError) as caught:
                db.get_connection()
        self.assertEqual(caught.exception.phase, "directory")
        self.assertEqual(blocker.read_bytes(), b"koru")
        self.assert_invalidated(db)
        db = self.new_db(self.fresh_path())
        state = db._inspect_readonly()
        error = sqlite3.OperationalError("unable to open database file")
        with mock.patch.object(db, "_inspect_readonly", return_value=state), \
                mock.patch.object(storage.sqlite3, "connect", side_effect=error):
            with self.assertRaises(SchemaMigrationError) as caught:
                db.get_connection()
        self.assertEqual(caught.exception.reason, "connection_setup_failed")
        self.assertEqual(caught.exception.phase, "connect")
        self.assertIs(caught.exception.__cause__, error)
        self.assert_invalidated(db)


    def test_unexpected_liveness_errors_are_not_mistaken_for_external_close(self):
        for primary in (sqlite3.ProgrammingError("beklenmeyen kullanım hatası"),
                        sqlite3.OperationalError("işlem durumu hatası"), KeyboardInterrupt("kesme")):
            with self.subTest(kind=type(primary).__name__):
                db = self.new_db(self.fresh_path("liveness_%d.db" % len(self._databases)))
                connect, connections = self.fault_factory({})
                with mock.patch.object(storage.sqlite3, "connect", side_effect=connect):
                    conn = db.get_connection()
                conn.state_error = primary
                with mock.patch.object(db, "_inspect_readonly") as inspect:
                    with self.assertRaises(type(primary)) as caught:
                        db.get_connection()
                self.assertIs(caught.exception, primary)
                inspect.assert_not_called()
                self.assert_invalidated(db, conn)

    def test_setup_interrupts_preserve_identity_and_close_uncached_handles(self):
        for sql in ("PRAGMA journal_mode=WAL", "PRAGMA busy_timeout=5000"):
            with self.subTest(sql=sql):
                db = self.new_db(self.fresh_path("interrupt_%d.db" % len(self._databases)))
                primary = KeyboardInterrupt("kurulum kesildi")
                connect, connections = self.fault_factory({"fail_sql": sql, "setup_error": primary})
                with mock.patch.object(storage.sqlite3, "connect", side_effect=connect):
                    with self.assertRaises(KeyboardInterrupt) as caught:
                        db.get_connection()
                self.assertIs(caught.exception, primary)
                self.assert_invalidated(db, connections[0])


    def test_failed_revalidation_in_one_thread_preserves_other_threads_active_connection(self):
        path = self.fresh_path()
        db = self.new_db(path)
        main_conn = db.get_connection()
        ready, resume = threading.Event(), threading.Event()
        errors, results = [], []

        def worker():
            try:
                conn = db.get_connection()
                conn.execute("INSERT INTO schema_metadata VALUES (42)")
                self.assertTrue(conn.in_transaction)
                ready.set()
                self.assertTrue(resume.wait(15))
                self.assertIs(db.get_connection(), conn)
                self.assertTrue(db._local.schema_ready)
                self.assertTrue(conn.in_transaction)
                self.assertEqual([row[0] for row in conn.execute("SELECT schema_version FROM schema_metadata ORDER BY schema_version")],
                                 [2, 42])
                conn.rollback()
                results.append(True)
            except BaseException as exc:
                errors.append(exc)
            finally:
                db.close()

        thread = threading.Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(ready.wait(15))
            primary = SchemaVersionError("yeniden doğrulama hatası", reason="inconsistent_schema")
            with mock.patch.object(db, "_inspect_readonly", side_effect=primary):
                with self.assertRaises(SchemaVersionError) as caught:
                    db.init_schema()
            self.assertIs(caught.exception, primary)
            self.assert_invalidated(db, main_conn)
        finally:
            resume.set()
            thread.join(20)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [True])
        self.assertEqual(snapshot(path)["table:schema_metadata"], [(2,)])
        self.assert_invalidated(db)


class TestTransactionLifecyclePackageTwo(LifecycleFaultMixin, MigrationTestCase):
    """İşlem hatalarını gerçek SQLite ve seçilmiş commit/rollback hatalarıyla sınar."""

    def run_faulted_migration(self, faults, ddl_error=None):
        path = self.v1_path()
        original = snapshot(path)
        db = self.new_db(path, allow_migration=True)
        connect, connections = self.fault_factory(faults)
        real_ddl = Database._execute_ddl_statement
        calls = []

        def ddl(self_db, conn, statement):
            calls.append(statement)
            if ddl_error is not None and len(calls) == 2:
                if faults.get("state_error") is not None:
                    conn.state_error = faults["state_error"]
                raise ddl_error
            return real_ddl(self_db, conn, statement)

        with mock.patch.object(storage.sqlite3, "connect", side_effect=connect), \
                mock.patch.object(Database, "_execute_ddl_statement", ddl):
            with self.assertRaises(BaseException) as caught:
                db.init_schema()
        return path, original, db, connections[0], caught.exception

    def assert_backup_preserved(self, path, original):
        finals, others = list_backups(path)
        self.assertEqual((len(finals), others), (1, []))
        self.assert_valid_v1_backup(finals[0], original)

    def test_ddl_failure_followed_by_rollback_interrupt_preserves_both_failures(self):
        for interrupt in (KeyboardInterrupt("geri alma kesildi"), SystemExit(8)):
            with self.subTest(kind=type(interrupt).__name__):
                primary = RuntimeError("DDL hatası")
                path, original, db, conn, error = self.run_faulted_migration(
                    {"rollback_error": interrupt}, primary,
                )
                self.assertIs(error, interrupt)
                self.assertEqual(error.phase, "rollback")
                self.assertEqual(error.transaction_outcome, "unknown")
                self.assertEqual(error.original_reason, "migration_ddl_failed")
                self.assertIs(error.primary_error, error.__cause__)
                self.assertIs(error.__cause__.__cause__, primary)
                self.assertEqual(conn.events.count("rollback"), 1)
                self.assertEqual(conn.events.count("commit"), 0)
                self.assert_invalidated(db, conn)
                self.assertEqual(snapshot(path), original)
                self.assert_backup_preserved(path, original)

    def test_commit_failure_followed_by_rollback_interrupt_preserves_both_failures(self):
        for interrupt in (KeyboardInterrupt("geri alma kesildi"), SystemExit(9)):
            with self.subTest(kind=type(interrupt).__name__):
                primary = sqlite3.OperationalError("COMMIT hatası")
                path, original, db, conn, error = self.run_faulted_migration({
                    "commit_mode": "before", "commit_error": primary, "rollback_error": interrupt,
                })
                self.assertIs(error, interrupt)
                self.assertEqual(error.transaction_outcome, "unknown")
                self.assertEqual(error.original_reason, "migration_commit_failed")
                self.assertIs(error.primary_error, error.__cause__)
                self.assertIs(error.__cause__.__cause__, primary)
                self.assertEqual(conn.events.count("commit"), 1)
                self.assertEqual(conn.events.count("rollback"), 1)
                self.assert_invalidated(db, conn)
                self.assertEqual(snapshot(path), original)
                self.assert_backup_preserved(path, original)

    def test_original_interrupt_takes_priority_over_a_second_rollback_interrupt(self):
        primary = KeyboardInterrupt("asıl kesme")
        secondary = SystemExit(10)
        path, original, db, conn, error = self.run_faulted_migration({"rollback_error": secondary}, primary)
        self.assertIs(error, primary)
        self.assertIs(error.rollback_error, secondary)
        self.assertEqual(error.transaction_outcome, "unknown")
        self.assert_invalidated(db, conn)
        self.assert_backup_preserved(path, original)

    def test_already_v2_timeout_restore_failure_retains_confirmed_rollback(self):
        path = self.fresh_path()
        original = snapshot(path)
        db = self.new_db(path, allow_migration=True)
        primary = sqlite3.OperationalError("timeout geri yükleme hatası")
        # İlk normal kurulum 5000 ms ayarını uygular; yalnızca geri alma sonrası ayar bozulur.
        connect, connections = self.fault_factory({})
        real_rollback = LifecycleFaultConnection.rollback

        def rollback(conn):
            real_rollback(conn)
            conn.faults.update(fail_sql="PRAGMA busy_timeout=5000", setup_error=primary)

        stale = storage._SchemaState(storage._STATE_V1, "bayat inceleme", 1)
        with mock.patch.object(db, "_inspect_readonly", return_value=stale), \
                mock.patch.object(storage.sqlite3, "connect", side_effect=connect), \
                mock.patch.object(LifecycleFaultConnection, "rollback", rollback):
            with self.assertRaises(SchemaMigrationError) as caught:
                db.init_schema()
        error = caught.exception
        conn = connections[0]
        self.assertEqual(error.reason, "connection_setup_failed")
        self.assertEqual(error.phase, "restore_timeout")
        self.assertEqual(error.transaction_outcome, "rolled_back")
        self.assertIs(error.__cause__, primary)
        self.assertIn("geri alınması doğrulandı", str(error))
        self.assertNotIn("COMMIT ile başarıyla", str(error))
        self.assertEqual(conn.events.count("rollback"), 1)
        self.assertEqual(conn.events.count("commit"), 0)
        self.assert_invalidated(db, conn)
        self.assertEqual(snapshot(path), original)
        self.assertEqual(list_backups(path), ([], []))

    def test_rollback_exception_preserves_original_failure_and_closes_connection(self):
        primary = RuntimeError("DDL hatası")
        secondary = sqlite3.OperationalError("ROLLBACK hatası")
        path, original, db, conn, error = self.run_faulted_migration({"rollback_error": secondary}, primary)
        self.assertEqual(error.reason, "migration_rollback_failed")
        self.assertEqual(error.original_reason, "migration_ddl_failed")
        self.assertEqual(error.transaction_outcome, "unknown")
        self.assertIs(error.rollback_error, secondary)
        self.assertIs(error.__cause__.__cause__, primary)
        self.assertNotIn("GERİ ALINDI", str(error.__cause__))
        self.assertEqual(conn.events.count("rollback"), 1)
        self.assert_invalidated(db, conn)
        self.assertEqual(snapshot(path), original)
        self.assert_backup_preserved(path, original)

    def test_rollback_returning_with_active_transaction_is_not_reported_as_success(self):
        path, original, db, conn, error = self.run_faulted_migration({"rollback_noop": True}, RuntimeError("DDL"))
        self.assertEqual(error.reason, "migration_rollback_failed")
        self.assertEqual(error.transaction_outcome, "unknown")
        self.assertIsInstance(error.rollback_error, RuntimeError)
        self.assert_invalidated(db, conn)
        self.assertEqual(snapshot(path), original)
        self.assert_backup_preserved(path, original)

    def test_unreadable_transaction_state_is_uncertain_and_invalidates_connection(self):
        state_error = sqlite3.OperationalError("işlem durumu okunamıyor")
        path, original, db, conn, error = self.run_faulted_migration({"state_error": state_error}, RuntimeError("DDL"))
        self.assertEqual(error.reason, "migration_rollback_failed")
        self.assertIs(error.rollback_error, state_error)
        self.assertEqual(conn.events.count("rollback"), 0)
        self.assert_invalidated(db, conn)
        self.assertEqual(snapshot(path), original)
        self.assert_backup_preserved(path, original)

    def test_rollback_and_close_failure_preserve_both_diagnostics_and_original_chain(self):
        faults = {"rollback_error": sqlite3.OperationalError("ROLLBACK"),
                  "close_error": sqlite3.OperationalError("CLOSE")}
        primary = RuntimeError("DDL")
        path, original, db, conn, error = self.run_faulted_migration(faults, primary)
        self.assertIs(error.close_error, faults["close_error"])
        self.assertIs(error.rollback_error, faults["rollback_error"])
        self.assertIs(error.__cause__.__cause__, primary)
        self.assert_invalidated(db)
        self.assertEqual(error.transaction_outcome, "unknown")
        self.assert_backup_preserved(path, original)
        faults.clear()
        sqlite3.Connection.close(conn)
        self.assertEqual(snapshot(path), original)
        self.assert_backup_preserved(path, original)

    def test_keyboard_interrupt_and_system_exit_survive_rollback_failure(self):
        for primary in (KeyboardInterrupt("kesme"), SystemExit(7)):
            with self.subTest(kind=type(primary).__name__):
                rollback_error = sqlite3.OperationalError("ROLLBACK")
                path, original, db, conn, error = self.run_faulted_migration({"rollback_error": rollback_error}, primary)
                self.assertIs(error, primary)
                self.assertIs(error.rollback_error, rollback_error)
                self.assertEqual(error.transaction_outcome, "unknown")
                self.assert_invalidated(db, conn)
                self.assertEqual(snapshot(path), original)
                self.assert_backup_preserved(path, original)

    def test_commit_failure_rolls_back_once_and_is_distinct_from_ddl_failure(self):
        primary = sqlite3.OperationalError("COMMIT busy")
        path, original, db, conn, error = self.run_faulted_migration({"commit_mode": "before", "commit_error": primary})
        self.assertEqual(error.reason, "migration_commit_failed")
        self.assertEqual(error.transaction_outcome, "rolled_back")
        self.assertIs(error.__cause__, primary)
        self.assertEqual(conn.events.count("commit"), 1)
        self.assertEqual(conn.events.count("rollback"), 1)
        self.assert_invalidated(db, conn)
        self.assertEqual(snapshot(path), original)
        self.assert_backup_preserved(path, original)

    def test_commit_and_rollback_failure_preserve_commit_reason_in_chain(self):
        primary = sqlite3.OperationalError("COMMIT")
        faults = {"commit_mode": "before", "commit_error": primary,
                  "rollback_error": sqlite3.OperationalError("ROLLBACK")}
        path, original, db, conn, error = self.run_faulted_migration(faults)
        self.assertEqual(error.reason, "migration_rollback_failed")
        self.assertEqual(error.original_reason, "migration_commit_failed")
        self.assertEqual(error.__cause__.reason, "migration_commit_failed")
        self.assertIs(error.__cause__.__cause__, primary)
        self.assertEqual(conn.events.count("commit"), 1)
        self.assert_invalidated(db, conn)
        self.assert_backup_preserved(path, original)

    def test_commit_then_exception_reports_unknown_outcome_without_retry_or_rollback(self):
        primary = sqlite3.OperationalError("COMMIT sonrası hata")
        path, original, db, conn, error = self.run_faulted_migration({"commit_mode": "after", "commit_error": primary})
        self.assertEqual(error.reason, "migration_commit_failed")
        self.assertEqual(error.transaction_outcome, "unknown")
        self.assertEqual(conn.events.count("commit"), 1)
        self.assertEqual(conn.events.count("rollback"), 0)
        self.assert_invalidated(db, conn)
        self.assert_v2_structure(path)
        self.assert_backup_preserved(path, original)

    def test_commit_error_after_automatic_rollback_does_not_claim_commit_or_repeat_rollback(self):
        primary = sqlite3.OperationalError("otomatik geri alma")
        path, original, db, conn, error = self.run_faulted_migration({"commit_mode": "auto_rollback", "commit_error": primary})
        self.assertEqual(error.reason, "migration_commit_failed")
        self.assertEqual(error.transaction_outcome, "unknown")
        self.assertEqual(conn.events.count("rollback"), 0)
        self.assert_invalidated(db, conn)
        self.assertEqual(snapshot(path), original)
        self.assert_backup_preserved(path, original)

    def test_commit_returning_with_active_transaction_is_detected(self):
        path, original, db, conn, error = self.run_faulted_migration({"commit_mode": "noop"})
        self.assertEqual(error.reason, "migration_commit_failed")
        self.assertEqual(error.transaction_outcome, "rolled_back")
        self.assert_invalidated(db, conn)
        self.assertEqual(snapshot(path), original)
        self.assert_backup_preserved(path, original)

    def test_interrupt_during_commit_remains_an_interrupt_after_confirmed_rollback(self):
        primary = KeyboardInterrupt("COMMIT kesildi")
        path, original, db, conn, error = self.run_faulted_migration({"commit_mode": "before", "commit_error": primary})
        self.assertIs(error, primary)
        self.assertEqual(error.transaction_outcome, "rolled_back")
        self.assert_invalidated(db, conn)
        self.assertEqual(snapshot(path), original)
        self.assert_backup_preserved(path, original)

    def test_migration_timeout_setup_failure_is_classified_before_begin(self):
        path, original, db, conn, error = self.run_faulted_migration({
            "fail_sql": "PRAGMA busy_timeout=30000", "setup_error": sqlite3.OperationalError("timeout"),
        })
        self.assertEqual(error.reason, "connection_setup_failed")
        self.assertEqual(error.phase, "migration_timeout")
        self.assertNotIn("BEGIN IMMEDIATE", conn.events)
        self.assert_invalidated(db, conn)
        self.assertEqual(snapshot(path), original)
        self.assertEqual(list_backups(path), ([], []))

    def test_post_commit_timeout_restore_failure_reports_committed_schema_and_keeps_backup(self):
        primary = sqlite3.OperationalError("timeout geri yükleme hatası")
        path, original, db, conn, error = self.run_faulted_migration({"restore_error": primary})
        self.assertEqual(error.reason, "connection_setup_failed")
        self.assertEqual(error.phase, "restore_timeout")
        self.assertEqual(error.transaction_outcome, "committed")
        self.assertIs(error.__cause__, primary)
        self.assertIn("COMMIT ile başarıyla", str(error))
        self.assertEqual(conn.events.count("commit"), 1)
        self.assertEqual(conn.events.count("rollback"), 0)
        self.assert_invalidated(db, conn)
        self.assert_v2_structure(path)
        self.assert_backup_preserved(path, original)
        names = [p.name for p in list_backups(path)[0]]
        db.get_connection()
        self.assertEqual([p.name for p in list_backups(path)[0]], names)


    def test_post_commit_timeout_readback_mismatch_reports_committed_and_invalidates(self):
        path, original, db, conn, error = self.run_faulted_migration({"restore_wrong_value": 30000})
        self.assertEqual(error.reason, "connection_setup_failed")
        self.assertEqual(error.phase, "restore_timeout")
        self.assertEqual(error.transaction_outcome, "committed")
        self.assertEqual(conn.events.count("commit"), 1)
        self.assertEqual(conn.events.count("rollback"), 0)
        self.assert_invalidated(db, conn)
        self.assert_v2_structure(path)
        self.assert_backup_preserved(path, original)

    def test_post_commit_cleanup_interrupt_preserves_committed_outcome(self):
        primary = KeyboardInterrupt("COMMIT sonrası kesme")
        path, original, db, conn, error = self.run_faulted_migration({"restore_error": primary})
        self.assertIs(error, primary)
        self.assertEqual(error.phase, "restore_timeout")
        self.assertEqual(error.transaction_outcome, "committed")
        self.assertEqual(conn.events.count("rollback"), 0)
        self.assert_invalidated(db, conn)
        self.assert_v2_structure(path)
        self.assert_backup_preserved(path, original)

    def test_begin_errors_use_sqlite_codes_without_misclassifying_non_lock_failure(self):
        for code, reason in ((sqlite3.SQLITE_BUSY, "lock_timeout"),
                             (sqlite3.SQLITE_READONLY, "migration_failed")):
            with self.subTest(code=code):
                primary = sqlite3.OperationalError("ileti kilit sözcüğü içermiyor")
                primary.sqlite_errorcode = code
                path, original, db, conn, error = self.run_faulted_migration({
                    "fail_sql": "BEGIN IMMEDIATE", "setup_error": primary,
                })
                self.assertEqual(error.reason, reason)
                self.assertIs(error.__cause__, primary)
                self.assertEqual(conn.events.count("rollback"), 0)
                self.assert_invalidated(db, conn)
                self.assertEqual(snapshot(path), original)
                self.assertEqual(list_backups(path), ([], []))

    def test_interrupt_ending_an_already_migrated_transaction_is_preserved_without_retry(self):
        path = self.fresh_path()
        db = self.new_db(path, allow_migration=True)
        primary = KeyboardInterrupt("geri alma kesildi")
        connect, connections = self.fault_factory({"rollback_error": primary})
        stale = storage._SchemaState(storage._STATE_V1, "bayat inceleme", 1)
        with mock.patch.object(db, "_inspect_readonly", return_value=stale), \
                mock.patch.object(storage.sqlite3, "connect", side_effect=connect):
            with self.assertRaises(KeyboardInterrupt) as caught:
                db.init_schema()
        self.assertIs(caught.exception, primary)
        self.assertEqual(primary.transaction_outcome, "unknown")
        self.assertEqual(connections[0].events.count("rollback"), 1)
        self.assert_invalidated(db, connections[0])
        self.assert_v2_structure(path)
        self.assertEqual(list_backups(path), ([], []))


class TestTemporaryConnectionCleanupPackageTwoOne(MigrationTestCase):
    """Geçici bağlantıların ikincil kapatma hataları asıl hata veya kesmeyi maskelememeli."""

    def temporary_connection(self, path, faults):
        conn = sqlite3.connect(str(path), factory=LifecycleFaultConnection)
        conn.faults = faults
        conn.events = []
        self._temporary_connections = getattr(self, "_temporary_connections", []) + [conn]
        self.addCleanup(sqlite3.Connection.close, conn)
        return conn

    def tearDown(self):
        for conn in getattr(self, "_temporary_connections", []):
            sqlite3.Connection.close(conn)
        super().tearDown()

    def test_readonly_probe_failure_retains_primary_and_close_diagnostics(self):
        for primary in (sqlite3.DatabaseError("okuma hatası"), KeyboardInterrupt("okuma kesildi"), SystemExit(11)):
            with self.subTest(kind=type(primary).__name__):
                path = self.v1_path()
                secondary = RuntimeError("kapatma hatası")
                conn = self.temporary_connection(path, {
                    "fail_sql": "SELECT COUNT(*) FROM sqlite_master", "setup_error": primary,
                    "close_error": secondary,
                })
                with mock.patch.object(storage.sqlite3, "connect", return_value=conn) as connect:
                    with self.assertRaises(type(primary)) as caught:
                        storage._open_readonly_connection(path, 1.0)
                self.assertIs(caught.exception, primary)
                self.assertIs(primary.close_error, secondary)
                self.assertEqual(primary.close_errors, (secondary,))
                self.assertEqual(conn.events.count("close"), 1)
                connect.assert_called_once_with(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1.0)

    def test_inspection_failure_retains_category_cause_and_temporary_close_error(self):
        for primary, category in ((sqlite3.OperationalError("database is locked"), "lock_timeout"),
                                  (sqlite3.OperationalError("okuma hatası"), "unreadable"),
                                  (sqlite3.DatabaseError("bozuk dosya"), "inconsistent_schema")):
            with self.subTest(category=category):
                path = self.v1_path()
                db = self.new_db(path, allow_migration=True)
                secondary = RuntimeError("geçici kapatma hatası")
                conn = self.temporary_connection(path, {"close_error": secondary})
                with mock.patch.object(storage, "_open_readonly_connection", return_value=conn), \
                        mock.patch.object(storage, "_classify_schema", side_effect=primary):
                    with self.assertRaises(SchemaMigrationError) as caught:
                        db.init_schema()
                error = caught.exception
                self.assertEqual(error.reason, category)
                self.assertIs(error.__cause__, primary)
                self.assertIs(error.close_error, secondary)
                self.assertEqual(error.close_errors, (secondary,))
                self.assertIn("Göç DDL'i çalıştırılmadı", str(error))
                self.assertIn("WAL yan dosyaları", str(error))
                self.assertNotIn("değişiklik yapılmadı", str(error))
                self.assertIsNone(db._local.conn)
                self.assertFalse(db._local.schema_ready)

    def test_inspection_interrupt_survives_temporary_close_failure(self):
        path = self.v1_path()
        db = self.new_db(path)
        for primary in (KeyboardInterrupt("inceleme kesildi"), SystemExit(12)):
            with self.subTest(kind=type(primary).__name__):
                secondary = sqlite3.OperationalError("kapatma hatası")
                conn = self.temporary_connection(path, {"close_error": secondary})
                with mock.patch.object(storage, "_open_readonly_connection", return_value=conn), \
                        mock.patch.object(storage, "_classify_schema", side_effect=primary):
                    with self.assertRaises(type(primary)) as caught:
                        db.init_schema()
                self.assertIs(caught.exception, primary)
                self.assertIs(primary.close_error, secondary)
                self.assertIsNone(db._local.conn)
                self.assertFalse(db._local.schema_ready)

    def test_backup_verification_retains_read_error_and_close_problem(self):
        for readonly in (False, True):
            with self.subTest(readonly=readonly):
                path = self.v1_path()
                primary = sqlite3.OperationalError("asıl doğrulama hatası")
                secondary = RuntimeError("ikincil kapatma hatası")
                conn = self.temporary_connection(path, {
                    "fail_sql": "PRAGMA integrity_check", "setup_error": primary, "close_error": secondary,
                })
                patcher = (mock.patch.object(storage, "_open_readonly_connection", return_value=conn) if readonly else
                           mock.patch.object(storage.sqlite3, "connect", return_value=conn))
                with patcher:
                    problems = storage._verify_backup_file(path, (), {}, [], readonly)
                self.assertIn("asıl doğrulama hatası", problems[0])
                self.assertIn("ikincil kapatma hatası", problems[1])
                self.assertEqual(len(problems), 2)
                self.assertIs(primary.close_error, secondary)
                self.assertEqual(conn.events.count("close"), 1)

    def test_backup_verification_mismatch_is_not_hidden_by_close_failure(self):
        path = self.v1_path()
        secondary = RuntimeError("kapatma hatası")
        conn = self.temporary_connection(path, {"close_error": secondary})
        with mock.patch.object(storage.sqlite3, "connect", return_value=conn):
            problems = storage._verify_backup_file(path, (), {}, [], False)
        self.assertIn("schema_metadata kaynakla eşleşmiyor", problems)
        self.assertIn("tarihsel satır sayıları kaynakla eşleşmiyor", problems)
        self.assertIn("tarihsel satır içerikleri kaynakla eşleşmiyor", problems)
        self.assertTrue(any("yedek bağlantısı kapatılamadı: RuntimeError: kapatma hatası" == problem
                            for problem in problems))
        self.assertEqual(conn.events.count("close"), 1)

    def test_backup_verification_interrupt_or_runtime_error_survives_close_failure(self):
        for primary in (RuntimeError("asıl hata"), KeyboardInterrupt("doğrulama kesildi"), SystemExit(13)):
            with self.subTest(kind=type(primary).__name__):
                path = self.v1_path()
                secondary = sqlite3.OperationalError("kapatma hatası")
                conn = self.temporary_connection(path, {
                    "fail_sql": "PRAGMA integrity_check", "setup_error": primary, "close_error": secondary,
                })
                with mock.patch.object(storage.sqlite3, "connect", return_value=conn):
                    with self.assertRaises(type(primary)) as caught:
                        storage._verify_backup_file(path, (), {}, [], False)
                self.assertIs(caught.exception, primary)
                self.assertIs(primary.close_error, secondary)

    def test_online_backup_failure_preserves_primary_and_both_close_errors(self):
        for primary in (sqlite3.OperationalError("yedekleme hatası"), KeyboardInterrupt("yedekleme kesildi"), SystemExit(14)):
            with self.subTest(kind=type(primary).__name__):
                path = self.v1_path()
                destination_path = self.tmp / ("partial_%d.db" % self._dir_counter)
                first, second = RuntimeError("hedef kapatma hatası"), SystemExit(15)
                source = self.temporary_connection(path, {"backup_error": primary, "close_error": second})
                destination = self.temporary_connection(destination_path, {"close_error": first})
                with mock.patch.object(storage.sqlite3, "connect", side_effect=[source, destination]):
                    with self.assertRaises(type(primary)) as caught:
                        storage._online_backup_copy(path, destination_path)
                self.assertIs(caught.exception, primary)
                self.assertIs(primary.close_error, first)
                self.assertEqual(primary.close_errors, (first, second))
                self.assertEqual(source.events.count("close"), 1)
                self.assertEqual(destination.events.count("close"), 1)

    def test_online_backup_close_only_failure_is_reported_and_all_closes_are_attempted(self):
        path = self.v1_path()
        destination_path = self.tmp / "partial.db"
        first, second = sqlite3.OperationalError("hedef kapatma hatası"), RuntimeError("kaynak kapatma hatası")
        source = self.temporary_connection(path, {"close_error": second})
        destination = self.temporary_connection(destination_path, {"close_error": first})
        with mock.patch.object(storage.sqlite3, "connect", side_effect=[source, destination]):
            with self.assertRaises(sqlite3.OperationalError) as caught:
                storage._online_backup_copy(path, destination_path)
        self.assertIs(caught.exception, first)
        self.assertEqual(first.close_errors, (second,))
        self.assertEqual(source.events.count("close"), 1)
        self.assertEqual(destination.events.count("close"), 1)

    def test_temporary_cleanup_does_not_invalidate_cached_application_transaction(self):
        db = self.new_db(self.fresh_path())
        cached = db.get_connection()
        cached.execute("INSERT INTO schema_metadata VALUES (42)")
        path = self.v1_path()
        primary = RuntimeError("geçici doğrulama hatası")
        secondary = sqlite3.OperationalError("geçici kapatma hatası")
        conn = self.temporary_connection(path, {
            "fail_sql": "PRAGMA integrity_check", "setup_error": primary, "close_error": secondary,
        })
        with mock.patch.object(storage.sqlite3, "connect", return_value=conn):
            with self.assertRaises(RuntimeError) as caught:
                storage._verify_backup_file(path, (), {}, [], False)
        self.assertIs(caught.exception, primary)
        self.assertIs(db.get_connection(), cached)
        self.assertTrue(cached.in_transaction)
        self.assertTrue(db._local.schema_ready)
        self.assertEqual([row[0] for row in cached.execute("SELECT schema_version FROM schema_metadata ORDER BY schema_version")],
                         [2, 42])
        cached.rollback()


class MetadataFaultConnection(sqlite3.Connection):
    """Gerçek dosya bağlantısında yalnızca seçili içgözlem sonucunu değiştirir."""

    def execute(self, sql, parameters=()):
        cursor = super().execute(sql, parameters)
        if sql.startswith("PRAGMA main.table_"):
            description, rows = self.transform(sql, cursor.description, cursor.fetchall())
            return mock.Mock(description=description, fetchall=lambda: rows)
        return cursor


class TestStructuralFingerprintPackageThreeA(MigrationTestCase):
    """Metadata parmak izini bağımsız DDL ve geçici veritabanlarıyla sınar."""

    v2_statements = (
        "CREATE UNIQUE INDEX idx_segments_id_session ON segments(id, session_id)",
        "CREATE TABLE artifacts (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL "
        "REFERENCES sessions(id) ON DELETE CASCADE, kind TEXT NOT NULL, content TEXT NOT NULL, "
        "capture_method TEXT NOT NULL, source_segment_id INTEGER, created_at TEXT NOT NULL, "
        "FOREIGN KEY (source_segment_id, session_id) REFERENCES segments(id, session_id))",
        "CREATE INDEX idx_artifacts_session ON artifacts(session_id, created_at, id)",
        "CREATE INDEX idx_artifacts_segment ON artifacts(source_segment_id, session_id)",
    )

    def variant_path(self, version=1, changes=None, extras=(), legacy=False, history=True):
        path = self._db_dir("variant.db") / "variant.db"
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            for statement in V1_FIXTURE_DDL + (self.v2_statements if version == 2 else ()):
                for table, change in (changes or {}).items():
                    if statement.startswith("CREATE TABLE %s (" % table):
                        statement = change(statement)
                conn.execute(statement)
            if not legacy:
                conn.execute("INSERT INTO schema_metadata VALUES (?)", (version,))
            if history:
                seed_history(conn)
            for statement in extras:
                conn.execute(statement)
            conn.commit()
        finally:
            conn.close()
        return path

    def assert_initial_rejection(self, path, detail):
        before = snapshot(path)
        db = self.new_db(path, allow_migration=True)
        with mock.patch.object(db, "_get_raw_connection") as raw, \
                mock.patch.object(db, "_create_backup") as backup, \
                mock.patch.object(db, "_execute_ddl_statement") as ddl:
            with self.assertRaises(SchemaVersionError) as caught:
                db.get_connection()
        self.assertEqual(caught.exception.reason, "inconsistent_schema")
        self.assertIn(detail, str(caught.exception))
        raw.assert_not_called()
        backup.assert_not_called()
        ddl.assert_not_called()
        self.assertIsNone(db._local.conn)
        self.assertFalse(db._local.schema_ready)
        self.assertEqual(snapshot(path), before)
        self.assertEqual(list_backups(path), ([], []))
        return caught.exception

    def metadata_factory(self, path, transform):
        real_connect = sqlite3.connect
        connections = []

        def connect(*args, **kwargs):
            if str(args[0]) not in (str(path), path.resolve().as_uri() + "?mode=ro"):
                return real_connect(*args, **kwargs)
            conn = real_connect(*args, **kwargs, factory=MetadataFaultConnection)
            conn.transform = transform
            connections.append(conn)
            self.addCleanup(sqlite3.Connection.close, conn)
            return conn

        return connect, connections

    def test_added_virtual_and_stored_generated_columns_are_rejected(self):
        for version, legacy in ((1, False), (2, False), (1, True)):
            for mode in ("VIRTUAL", "STORED"):
                with self.subTest(version=version, legacy=legacy, mode=mode):
                    path = self.variant_path(version, legacy=legacy, changes={
                        "sessions": lambda sql, mode=mode: sql[:-1] +
                        ", extra_length INTEGER GENERATED ALWAYS AS (length(title)) %s)" % mode,
                    })
                    self.assert_initial_rejection(path, "sütun tanımları")
                    conn = sqlite3.connect(str(path))
                    try:
                        rows = conn.execute("PRAGMA main.table_xinfo(sessions)").fetchall()
                        self.assertEqual(rows[-1][6], 2 if mode == "VIRTUAL" else 3)
                        self.assertNotIn("extra_length", [row[1] for row in conn.execute("PRAGMA table_info(sessions)")])
                    finally:
                        conn.close()

    def test_required_column_replaced_by_generated_column_is_rejected(self):
        path = self.variant_path(history=False, changes={
            "sessions": lambda sql: sql.replace("source_name TEXT", "source_name TEXT AS (source_type) VIRTUAL"),
        })
        self.assert_initial_rejection(path, "sütun tanımları")

    def test_hidden_flag_is_compared_even_when_all_other_column_metadata_matches(self):
        path = self.variant_path()

        def transform(sql, description, rows):
            if sql == 'PRAGMA main.table_xinfo("sessions")':
                rows = [tuple(row[:6]) + (1,) if row[1] == "source_name" else tuple(row) for row in rows]
            return description, rows

        connect, connections = self.metadata_factory(path, transform)
        with mock.patch.object(storage.sqlite3, "connect", side_effect=connect):
            db = self.new_db(path, allow_migration=True)
            with self.assertRaises(SchemaVersionError) as caught:
                db.init_schema()
        self.assertEqual(caught.exception.reason, "inconsistent_schema")
        self.assertIn("gizli/üretilmiş", str(caught.exception))
        self.assertEqual(list_backups(path), ([], []))

    def test_strict_table_is_rejected_for_versioned_and_legacy_schemas(self):
        for version, legacy in ((1, False), (2, False), (1, True)):
            with self.subTest(version=version, legacy=legacy):
                path = self.variant_path(version, legacy=legacy, changes={"schema_metadata": lambda sql: sql + " STRICT"})
                self.assert_initial_rejection(path, "STRICT/WITHOUT ROWID")

    def test_without_rowid_table_is_rejected_with_explicit_option_diagnostic(self):
        for version in (1, 2):
            with self.subTest(version=version):
                path = self.variant_path(version, changes={"sessions": lambda sql: sql + " WITHOUT ROWID"})
                self.assert_initial_rejection(path, "STRICT/WITHOUT ROWID")

    def test_virtual_and_shadow_kinds_are_rejected_even_with_matching_columns(self):
        path = self.variant_path()
        for kind in ("virtual", "shadow"):
            with self.subTest(kind=kind):
                def transform(sql, description, rows):
                    if sql == 'PRAGMA main.table_list("sessions")':
                        rows = [(row[0], row[1], kind) + tuple(row[3:]) for row in rows]
                    return description, rows

                connect, connections = self.metadata_factory(path, transform)
                with mock.patch.object(storage.sqlite3, "connect", side_effect=connect):
                    with self.assertRaises(SchemaVersionError) as caught:
                        self.new_db(path, allow_migration=True).init_schema()
                self.assertEqual(caught.exception.reason, "inconsistent_schema")
                self.assertIn("tablo türü", str(caught.exception))

    def test_persistent_triggers_on_every_required_table_are_rejected(self):
        for version in (1, 2):
            tables = ("sessions", "segments", "summaries", "schema_metadata") + (("artifacts",) if version == 2 else ())
            for table in tables:
                for timing in ("BEFORE", "AFTER"):
                    with self.subTest(version=version, table=table, timing=timing):
                        path = self.variant_path(version, extras=(
                            'CREATE TRIGGER extra_trigger %s UPDATE ON "%s" BEGIN SELECT RAISE(ABORT, \'engelle\'); END'
                            % (timing, table.upper()),
                        ))
                        self.assert_initial_rejection(path, "extra_trigger")

    def test_extension_only_triggers_and_objects_remain_allowed_for_versioned_schemas(self):
        extras = (
            "CREATE TABLE extension_notes (value TEXT UNIQUE)",
            "CREATE TABLE extension_log (value TEXT)",
            "CREATE VIEW extension_view AS SELECT value FROM extension_notes",
            "CREATE TRIGGER extension_trigger AFTER INSERT ON extension_notes "
            "BEGIN INSERT INTO extension_log VALUES (new.value); END",
        )
        for version in (1, 2):
            with self.subTest(version=version):
                path = self.variant_path(version, extras=extras)
                original = snapshot(path)
                db = self.new_db(path, allow_migration=version == 1)
                conn = db.get_connection()
                conn.execute("INSERT INTO extension_notes VALUES ('koru')")
                conn.commit()
                self.assertEqual(conn.execute("SELECT value FROM extension_log").fetchone()[0], "koru")
                self.assertEqual(history_part(snapshot(path))["table:sessions"], original["table:sessions"])
                self.assertEqual(len(list_backups(path)[0]), 1 if version == 1 else 0)

    def test_legacy_schema_rejects_extension_triggers_and_extra_nonunique_indexes(self):
        for extras in (("CREATE TABLE extension_notes (value TEXT)",
                        "CREATE TRIGGER extension_trigger AFTER INSERT ON extension_notes BEGIN SELECT 1; END"),
                       ("CREATE INDEX extra_nonunique ON sessions(title)",)):
            with self.subTest(extras=extras):
                self.assert_initial_rejection(self.variant_path(legacy=True, extras=extras), "beklenmeyen ek")

    def test_unexpected_unique_constraints_are_rejected(self):
        for version, legacy in ((1, False), (2, False), (1, True)):
            with self.subTest(version=version, legacy=legacy):
                path = self.variant_path(version, legacy=legacy, changes={
                    "schema_metadata": lambda sql: sql[:-1] + ", UNIQUE(schema_version))",
                })
                self.assert_initial_rejection(path, "UNIQUE/PRIMARY KEY")

    def test_additional_unique_indexes_including_partial_and_expression_are_rejected(self):
        for version in (1, 2):
            for definition in ("sessions(id)", "sessions(lower(id))", "sessions(id) WHERE source_name IS NOT NULL"):
                with self.subTest(version=version, definition=definition):
                    path = self.variant_path(version, extras=("CREATE UNIQUE INDEX extra_unique ON %s" % definition,))
                    self.assert_initial_rejection(path, "extra_unique")

    def test_additional_nonunique_indexes_remain_allowed_for_versioned_schemas(self):
        for version in (1, 2):
            with self.subTest(version=version):
                path = self.variant_path(version, extras=("CREATE INDEX extra_nonunique ON sessions(title)",))
                original = snapshot(path)
                self.new_db(path, allow_migration=version == 1).init_schema()
                self.assertIn(("index", "extra_nonunique"), snapshot(path)["objects"])
                self.assertEqual(snapshot(path)["table:sessions"], original["table:sessions"])


    def assert_capability_rejection(self, path, transform):
        original = snapshot(path)
        db = self.new_db(path, allow_migration=True)
        connect, connections = self.metadata_factory(path, transform)
        with mock.patch.object(storage.sqlite3, "connect", side_effect=connect), \
                mock.patch.object(db, "_get_raw_connection") as raw:
            with self.assertRaises(SchemaVersionError) as caught:
                db.get_connection()
        self.assertEqual(caught.exception.reason, "unreadable")
        self.assertIn("içgözlem", str(caught.exception))
        raw.assert_not_called()
        self.assertIsNone(db._local.conn)
        self.assertFalse(db._local.schema_ready)
        self.assertTrue(connections)
        for conn in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                sqlite3.Connection.execute(conn, "SELECT 1")
        self.assertEqual(snapshot(path), original)
        self.assertEqual(list_backups(path), ([], []))
        return caught.exception

    def test_unavailable_empty_and_truncated_introspection_results_fail_closed(self):
        path = self.variant_path()
        for pragma in ('PRAGMA main.table_list("sessions")', 'PRAGMA main.table_xinfo("sessions")'):
            for fault in ("unavailable", "no_rows", "missing_column", "short_row", "duplicate_rows"):
                with self.subTest(pragma=pragma, fault=fault):
                    def transform(sql, description, rows):
                        if sql == pragma:
                            if fault == "unavailable":
                                return None, []
                            if fault == "no_rows":
                                return description, []
                            if fault == "missing_column":
                                return description[:-1], [tuple(row[:-1]) for row in rows]
                            if fault == "short_row":
                                return description, [tuple(row[:-1]) for row in rows]
                            return description, rows + rows
                        return description, rows

                    self.assert_capability_rejection(path, transform)

    def test_malformed_table_options_fail_closed(self):
        path = self.variant_path()
        for position, value in ((0, "temp"), (1, "other"), (2, "unknown"),
                                (3, 0), (3, "11"), (4, 2), (4, None), (5, -1), (5, "0")):
            with self.subTest(position=position, value=value):
                def transform(sql, description, rows):
                    if sql == 'PRAGMA main.table_list("sessions")':
                        row = list(rows[0])
                        row[position] = value
                        return description, [tuple(row)]
                    return description, rows

                self.assert_capability_rejection(path, transform)

    def test_malformed_column_metadata_fails_closed(self):
        path = self.variant_path()
        for position, value in ((0, -1), (1, ""), (1, None), (2, 5), (3, 2),
                                (4, []), (5, -1), (6, 4), (6, "0")):
            with self.subTest(position=position, value=value):
                def transform(sql, description, rows):
                    if sql == 'PRAGMA main.table_xinfo("sessions")':
                        rows = [tuple(row) for row in rows]
                        row = list(rows[0])
                        row[position] = value
                        rows[0] = tuple(row)
                    return description, rows

                self.assert_capability_rejection(path, transform)

    def test_introspection_sqlite_errors_preserve_cause(self):
        path = self.variant_path()
        for pragma in ('PRAGMA main.table_list("sessions")', 'PRAGMA main.table_xinfo("sessions")'):
            with self.subTest(pragma=pragma):
                primary = sqlite3.OperationalError("içgözlem desteklenmiyor")

                def transform(sql, description, rows):
                    if sql == pragma:
                        raise primary
                    return description, rows

                error = self.assert_capability_rejection(path, transform)
                self.assertIs(error.__cause__, primary)

    def test_future_additional_introspection_columns_do_not_break_valid_schema(self):
        path = self.variant_path(2)

        def transform(sql, description, rows):
            return description + (("future_field", None, None, None, None, None, None),), [tuple(row) + (99,) for row in rows]

        connect, connections = self.metadata_factory(path, transform)
        with mock.patch.object(storage.sqlite3, "connect", side_effect=connect):
            db = self.new_db(path)
            conn = db.get_connection()
        self.assertIs(db.get_connection(), conn)
        self.assertTrue(db._local.schema_ready)

    def test_reference_capability_failure_does_not_cache_incomplete_shapes(self):
        real_connect = sqlite3.connect
        connections = []

        def transform(sql, description, rows):
            return None, []

        def connect(*args, **kwargs):
            self.assertEqual(args[0], ":memory:")
            conn = real_connect(*args, **kwargs, factory=MetadataFaultConnection)
            conn.transform = transform
            connections.append(conn)
            return conn

        with mock.patch.dict(storage._reference_cache, clear=True):
            with mock.patch.object(storage.sqlite3, "connect", side_effect=connect):
                with self.assertRaises(SchemaVersionError) as caught:
                    storage._reference_shapes(False)
            self.assertEqual(caught.exception.reason, "unreadable")
            self.assertEqual(storage._reference_cache, {})
            for conn in connections:
                with self.assertRaises(sqlite3.ProgrammingError):
                    conn.execute("SELECT 1")
            shapes = storage._reference_shapes(False)
            self.assertEqual(set(shapes), {"sessions", "segments", "summaries", "schema_metadata"})
            self.assertTrue(all(shape.table_kind == "table" and shape.strict == 0 and shape.without_rowid == 0
                                and not shape.triggers and all(col[5] == 0 for col in shape.columns)
                                for shape in shapes.values()))

    def test_under_lock_reclassification_rejects_each_material_mismatch_before_backup(self):
        cases = (
            ({"sessions": lambda sql: sql[:-1] + ", extra_length INTEGER AS (length(title)) VIRTUAL)"}, (), "sütun tanımları"),
            ({"schema_metadata": lambda sql: sql + " STRICT"}, (), "STRICT/WITHOUT ROWID"),
            ({"sessions": lambda sql: sql + " WITHOUT ROWID"}, (), "STRICT/WITHOUT ROWID"),
            ({}, ("CREATE TRIGGER extra_trigger AFTER UPDATE ON schema_metadata BEGIN SELECT 1; END",), "extra_trigger"),
            ({"schema_metadata": lambda sql: sql[:-1] + ", UNIQUE(schema_version))"}, (), "UNIQUE/PRIMARY KEY"),
            ({}, ("CREATE UNIQUE INDEX extra_unique ON sessions(id)",), "extra_unique"),
        )
        for changes, extras, detail in cases:
            with self.subTest(detail=detail, extras=extras):
                path = self.variant_path(changes=changes, extras=extras)
                original = snapshot(path)
                db = self.new_db(path, allow_migration=True)
                handles = []
                classify = db._classify_under_lock

                def record(conn):
                    self.assertTrue(conn.in_transaction)
                    handles.append(conn)
                    return classify(conn)

                with mock.patch.object(db, "_inspect_readonly", return_value=storage._SchemaState(storage._STATE_FRESH)), \
                        mock.patch.object(db, "_classify_under_lock", side_effect=record), \
                        mock.patch.object(db, "_create_backup") as backup, \
                        mock.patch.object(db, "_execute_ddl_statement") as ddl:
                    with self.assertRaises(SchemaVersionError) as caught:
                        db.init_schema()
                self.assertEqual(caught.exception.reason, "inconsistent_schema")
                self.assertEqual(caught.exception.transaction_outcome, "rolled_back")
                self.assertIn(detail, str(caught.exception))
                backup.assert_not_called()
                ddl.assert_not_called()
                self.assertIsNone(db._local.conn)
                self.assertFalse(db._local.schema_ready)
                self.assertEqual(len(handles), 1)
                with self.assertRaises(sqlite3.ProgrammingError):
                    handles[0].execute("SELECT 1")
                self.assertEqual(snapshot(path), original)
                self.assertEqual(list_backups(path), ([], []))

    def test_under_lock_capability_failure_rolls_back_and_invalidates(self):
        path = self.variant_path()
        original = snapshot(path)

        def transform(sql, description, rows):
            return None, []

        connect, connections = self.metadata_factory(path, transform)
        db = self.new_db(path, allow_migration=True)
        with mock.patch.object(db, "_inspect_readonly", return_value=storage._SchemaState(storage._STATE_V1)), \
                mock.patch.object(storage.sqlite3, "connect", side_effect=connect):
            with self.assertRaises(SchemaVersionError) as caught:
                db.init_schema()
        self.assertEqual(caught.exception.reason, "unreadable")
        self.assertEqual(caught.exception.transaction_outcome, "rolled_back")
        self.assertIsNone(db._local.conn)
        self.assertFalse(db._local.schema_ready)
        for conn in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")
        self.assertEqual(snapshot(path), original)
        self.assertEqual(list_backups(path), ([], []))

    def test_post_ddl_verification_rejects_new_structural_changes_and_preserves_backup(self):
        for statement, detail in (
            ("ALTER TABLE artifacts ADD COLUMN extra_length INTEGER AS (length(content)) VIRTUAL", "sütun tanımları"),
            ("CREATE TRIGGER extra_trigger AFTER INSERT ON artifacts BEGIN SELECT 1; END", "extra_trigger"),
            ("CREATE UNIQUE INDEX extra_unique ON artifacts(id)", "extra_unique"),
        ):
            with self.subTest(statement=statement):
                path = self.variant_path()
                original = snapshot(path)
                db = self.new_db(path, allow_migration=True)
                write = db._write_schema_version
                handles = []

                def mutate(conn, insert):
                    write(conn, insert)
                    conn.execute(statement)
                    handles.append(conn)

                with mock.patch.object(db, "_write_schema_version", side_effect=mutate):
                    with self.assertRaises(SchemaMigrationError) as caught:
                        db.init_schema()
                self.assertEqual(caught.exception.reason, "migration_ddl_failed")
                self.assertEqual(caught.exception.transaction_outcome, "rolled_back")
                self.assertIn(detail, str(caught.exception))
                self.assertIsNone(db._local.conn)
                self.assertFalse(db._local.schema_ready)
                with self.assertRaises(sqlite3.ProgrammingError):
                    handles[0].execute("SELECT 1")
                self.assertEqual(snapshot(path), original)
                finals, others = list_backups(path)
                self.assertEqual((len(finals), others), (1, []))
                self.assert_valid_v1_backup(finals[0], original)

    def test_independent_signature_observes_generated_flags_options_and_trigger_attachments(self):
        ordinary = self.variant_path()
        generated = self.variant_path(changes={"sessions": lambda sql: sql[:-1] + ", extra_length INTEGER AS (length(title)) VIRTUAL)"})
        strict = self.variant_path(changes={"schema_metadata": lambda sql: sql + " STRICT"})
        without = self.variant_path(changes={"sessions": lambda sql: sql + " WITHOUT ROWID"})
        triggered = self.variant_path(extras=("CREATE TRIGGER extra_trigger AFTER UPDATE ON sessions BEGIN SELECT 1; END",))
        baseline = structure_signature(ordinary)
        self.assertEqual(structure_signature(generated)["tables"]["sessions"]["columns"][-1][-1], 2)
        self.assertEqual(structure_signature(strict)["tables"]["schema_metadata"]["options"], ("table", 0, 1))
        self.assertEqual(structure_signature(without)["tables"]["sessions"]["options"], ("table", 1, 0))
        self.assertEqual(structure_signature(triggered)["tables"]["sessions"]["triggers"][0][0], "extra_trigger")
        for path in (generated, strict, without, triggered):
            self.assertNotEqual(structure_signature(path), baseline)


class TestSemanticFingerprintPackageThreeB(MigrationTestCase):
    """Hedeflenen SQL davranışlarını bağımsız DDL ve gerçek geçici SQLite ile sınar."""

    v2_statements = TestStructuralFingerprintPackageThreeA.v2_statements
    variant_path = TestStructuralFingerprintPackageThreeA.variant_path
    assert_initial_rejection = TestStructuralFingerprintPackageThreeA.assert_initial_rejection

    def assert_accepted(self, changes, version=1, legacy=False):
        path = self.variant_path(version=version, changes=changes, legacy=legacy)
        before = snapshot(path)
        db = self.new_db(path, allow_migration=True)
        db.init_schema()
        self.assert_v2_structure(path)
        for table in ("sessions", "segments", "summaries"):
            self.assertEqual(snapshot(path)["table:" + table], before["table:" + table])
        return path, db

    def conflict_cases(self, policy):
        return (
            {"sessions": lambda sql: sql.replace("id TEXT PRIMARY KEY", "id TEXT PRIMARY KEY ON CONFLICT " + policy)},
            {"sessions": lambda sql: sql.replace("title TEXT NOT NULL", "title TEXT NOT NULL ON CONFLICT " + policy)},
            {"segments": lambda sql: sql.replace("UNIQUE (session_id, sequence)",
                                                 "CONSTRAINT named_unique UNIQUE (session_id, sequence) ON CONFLICT " + policy)},
            {"sessions": lambda sql: sql.replace("id TEXT PRIMARY KEY", "id TEXT")[:-1]
             + ", CONSTRAINT named_primary PRIMARY KEY (id) ON CONFLICT " + policy + ")"},
        )

    def assert_conflict_rejected(self, policy):
        for version in (1, 2):
            for case, changes in enumerate(self.conflict_cases(policy)):
                with self.subTest(version=version, policy=policy, case=case):
                    self.assert_initial_rejection(self.variant_path(version=version, changes=changes), "ON CONFLICT")

    def test_column_check_is_rejected(self):
        for version in (1, 2):
            path = self.variant_path(version=version, changes={
                "sessions": lambda sql: sql.replace("title TEXT NOT NULL", "title TEXT NOT NULL CHECK(length(title) >= 0)"),
            })
            self.assert_initial_rejection(path, "CHECK")

    def test_table_check_is_rejected_without_interpreting_expression(self):
        path = self.variant_path(changes={
            "sessions": lambda sql: sql[:-1] + ", CHECK((length(title) >= 0) AND ('CHECK, )' != '')))",
        })
        self.assert_initial_rejection(path, "CHECK")

    def test_named_checks_in_both_positions_are_rejected(self):
        for changes in (
            {"sessions": lambda sql: sql.replace("title TEXT NOT NULL",
                                                  'title TEXT CONSTRAINT "named CHECK" CHECK(length(title) >= 0) NOT NULL')},
            {"sessions": lambda sql: sql[:-1] + ', CONSTRAINT `named CHECK` CHECK(1) ON CONFLICT ABORT)'},
        ):
            self.assert_initial_rejection(self.variant_path(changes=changes), "CHECK")

    def test_check_on_schema_metadata_is_rejected_even_when_current_rows_satisfy_it(self):
        path = self.variant_path(changes={
            "schema_metadata": lambda sql: sql.replace("INTEGER NOT NULL", "INTEGER NOT NULL CHECK(schema_version=1)"),
        })
        self.assert_initial_rejection(path, "CHECK")

    def test_conflict_replace_is_rejected(self):
        self.assert_conflict_rejected("REPLACE")

    def test_conflict_ignore_is_rejected(self):
        self.assert_conflict_rejected("IGNORE")

    def test_conflict_fail_is_rejected(self):
        self.assert_conflict_rejected("FAIL")

    def test_conflict_rollback_is_rejected(self):
        self.assert_conflict_rejected("ROLLBACK")

    def test_inline_unique_policies_are_rejected(self):
        for policy in ("REPLACE", "IGNORE", "FAIL", "ROLLBACK"):
            with self.subTest(policy=policy):
                path = self.variant_path(version=2, changes={
                    "artifacts": lambda sql: sql.replace("source_segment_id INTEGER",
                                                          "source_segment_id INTEGER UNIQUE ON CONFLICT " + policy),
                })
                self.assert_initial_rejection(path, "ON CONFLICT")

    def test_explicit_abort_is_accepted_for_inline_and_table_constraints(self):
        for changes in self.conflict_cases("ABORT"):
            self.assert_accepted(changes)
        self.assert_accepted({
            "segments": lambda sql: sql.replace("PRIMARY KEY AUTOINCREMENT", "PRIMARY KEY ON CONFLICT ABORT AUTOINCREMENT")
            .replace("NOT NULL", "NOT NULL ON CONFLICT ABORT")
            .replace("UNIQUE (session_id, sequence)", "UNIQUE (session_id, sequence) ON CONFLICT ABORT"),
        })

    def test_later_abort_policy_cannot_hide_an_earlier_replace(self):
        path = self.variant_path(changes={
            "sessions": lambda sql: sql.replace("id TEXT PRIMARY KEY", "id TEXT PRIMARY KEY ON CONFLICT REPLACE")
            .replace("title TEXT NOT NULL", "title TEXT NOT NULL ON CONFLICT ABORT"),
        })
        self.assert_initial_rejection(path, "ON CONFLICT")

    def test_conflict_policies_remain_bound_to_their_constraint_columns(self):
        sql = ("CREATE TABLE sample (a TEXT NOT NULL ON CONFLICT ABORT, "
               "b INTEGER CONSTRAINT uq UNIQUE ON CONFLICT IGNORE, "
               "CONSTRAINT pk PRIMARY KEY (a) ON CONFLICT REPLACE)")
        semantics = storage._RequiredTableReader().read(sql, "sample")
        self.assertEqual(semantics.conflicts, (
            ("NOT NULL", ("a",), "ABORT"), ("PRIMARY KEY", ("a",), "REPLACE"), ("UNIQUE", ("b",), "IGNORE"),
        ))

    def test_deferred_inline_foreign_keys_are_rejected(self):
        for table in ("segments", "summaries"):
            path = self.variant_path(changes={
                table: lambda sql: sql.replace("ON DELETE CASCADE", "ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED"),
            })
            self.assert_initial_rejection(path, "FK zamanlaması")

    def test_deferred_composite_foreign_key_is_rejected(self):
        path = self.variant_path(version=2, changes={
            "artifacts": lambda sql: sql.replace("REFERENCES segments(id, session_id)",
                                                  "REFERENCES segments(id, session_id) DEFERRABLE INITIALLY DEFERRED"),
        })
        self.assert_initial_rejection(path, "FK zamanlaması")

    def test_all_immediate_foreign_key_forms_are_accepted(self):
        variants = ("", "NOT DEFERRABLE", "NOT DEFERRABLE INITIALLY IMMEDIATE",
                    "NOT DEFERRABLE INITIALLY DEFERRED", "DEFERRABLE", "DEFERRABLE INITIALLY IMMEDIATE")
        for timing in variants:
            with self.subTest(timing=timing):
                path, db = self.assert_accepted({
                    "artifacts": lambda sql, timing=timing: sql.replace("ON DELETE CASCADE", "ON DELETE CASCADE " + timing)
                    .replace("REFERENCES segments(id, session_id)", "REFERENCES segments(id, session_id) " + timing),
                }, version=2)
                conn = db.get_connection()
                conn.execute("BEGIN")
                try:
                    with self.assertRaises(sqlite3.IntegrityError):
                        conn.execute("INSERT INTO artifacts(session_id,kind,content,capture_method,source_segment_id,created_at) "
                                     "VALUES ('sess-b','note','x','manual',1,'now')")
                    self.assertTrue(conn.in_transaction)
                finally:
                    conn.rollback()

    def test_timing_after_other_inline_constraints_keeps_its_fk_owner(self):
        for timing, accepted in (("not deferrable initially deferred", True), ("deferrable initially deferred", False)):
            changes = {"segments": lambda sql, timing=timing: sql.replace(
                "session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE",
                "session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE NOT NULL " + timing,
            )}
            if accepted:
                self.assert_accepted(changes)
            else:
                self.assert_initial_rejection(self.variant_path(changes=changes), "FK zamanlaması")

    def test_fk_timing_is_bound_to_each_actual_mapping(self):
        for deferred_target in ("sessions", "segments"):
            path = self.variant_path(version=2, changes={
                "artifacts": lambda sql, target=deferred_target: sql.replace(
                    "ON DELETE CASCADE", "ON DELETE CASCADE " +
                    ("DEFERRABLE INITIALLY DEFERRED" if target == "sessions" else "NOT DEFERRABLE INITIALLY DEFERRED"),
                ).replace("REFERENCES segments(id, session_id)", "REFERENCES segments(id, session_id) " +
                          ("DEFERRABLE INITIALLY DEFERRED" if target == "segments" else "DEFERRABLE INITIALLY IMMEDIATE")),
            })
            self.assert_initial_rejection(path, "FK zamanlaması")

    def test_named_composite_fk_with_reordered_actions_is_accepted(self):
        self.assert_accepted({
            "artifacts": lambda sql: sql.replace("FOREIGN KEY (source_segment_id, session_id)",
                                                  'CONSTRAINT "fk named" FOREIGN KEY (source_segment_id, session_id)')
            .replace("REFERENCES segments(id, session_id)",
                     "REFERENCES segments(id, session_id) ON UPDATE NO ACTION ON DELETE NO ACTION NOT DEFERRABLE INITIALLY DEFERRED"),
        }, version=2)

    def test_nonbinary_unindexed_column_collations_are_rejected(self):
        for collation in ("NOCASE", "RTRIM"):
            for version in (1, 2):
                with self.subTest(collation=collation, version=version):
                    path = self.variant_path(version=version, changes={
                        "sessions": lambda sql, collation=collation: sql.replace("title TEXT NOT NULL", "title TEXT COLLATE " + collation + " NOT NULL"),
                    })
                    self.assert_initial_rejection(path, "COLLATE")

    def test_custom_collation_is_rejected_without_registering_it_on_inspection(self):
        path = self.variant_path(history=False)
        conn = sqlite3.connect(str(path))
        try:
            conn.create_collation("custom_order", lambda a, b: (a > b) - (a < b))
            conn.execute("DROP TABLE summaries")
            conn.execute(V1_FIXTURE_DDL[2].replace("content TEXT NOT NULL", "content TEXT COLLATE custom_order NOT NULL"))
            conn.execute("CREATE INDEX idx_summaries_session ON summaries(session_id, kind, created_at)")
            conn.commit()
        finally:
            conn.close()
        self.assert_initial_rejection(path, "COLLATE")

    def test_explicit_binary_on_every_column_is_accepted(self):
        changes = {table: lambda sql: re.sub(r"\b(TEXT|INTEGER)\b", r"\1 COLLATE BINARY", sql)
                   for table in ("sessions", "segments", "summaries", "schema_metadata", "artifacts")}
        self.assert_accepted(changes, version=2)

    def test_missing_autoincrement_with_comment_decoys_is_rejected(self):
        for decoy in ("/* AUTOINCREMENT */", "-- AUTOINCREMENT\n", "/* 'AUTOINCREMENT' \"AUTOINCREMENT\" */"):
            for table in ("segments", "summaries", "artifacts"):
                with self.subTest(decoy=decoy, table=table):
                    path = self.variant_path(version=2 if table == "artifacts" else 1, changes={
                        table: lambda sql, decoy=decoy: sql.replace("AUTOINCREMENT", decoy),
                    })
                    self.assert_initial_rejection(path, "AUTOINCREMENT")

    def test_autoincrement_constraint_name_is_not_a_declaration(self):
        path = self.variant_path(changes={
            "segments": lambda sql: sql.replace("PRIMARY KEY AUTOINCREMENT", 'CONSTRAINT "AUTOINCREMENT" PRIMARY KEY'),
        })
        self.assert_initial_rejection(path, "AUTOINCREMENT")
        conn = sqlite3.connect(str(path))
        try:
            self.assertFalse(native_autoincrement(conn, "segments"))
        finally:
            conn.close()

    def test_table_primary_key_autoincrement_is_bound_to_id(self):
        self.assert_accepted({
            "summaries": lambda sql: sql.replace("id INTEGER PRIMARY KEY AUTOINCREMENT", "id INTEGER")[:-1]
            + ", CONSTRAINT pk PRIMARY KEY(id AUTOINCREMENT) ON CONFLICT ABORT)",
        })

    def test_misplaced_autoincrement_and_unassociated_timing_fail_closed(self):
        statements = (
            "CREATE TABLE sample(id INTEGER AUTOINCREMENT PRIMARY KEY)",
            "CREATE TABLE sample(id INTEGER, PRIMARY KEY(id), AUTOINCREMENT)",
            "CREATE TABLE sample(id INTEGER NOT DEFERRABLE INITIALLY DEFERRED)",
            "CREATE TABLE sample(id INTEGER PRIMARY KEY, content TEXT AUTOINCREMENT)",
        )
        for statement in statements:
            with self.subTest(statement=statement):
                with self.assertRaises(storage._TableSqlError):
                    storage._RequiredTableReader().read(statement, "sample")

    def test_comments_and_keyword_constraint_names_do_not_change_semantics(self):
        self.assert_accepted({
            "sessions": lambda sql: sql.replace("title TEXT NOT NULL",
                'title /* CHECK(0), ON CONFLICT REPLACE */ TEXT CONSTRAINT "CHECK" NOT NULL ON /* IGNORE */ CONFLICT ABORT')
            .replace("id TEXT PRIMARY KEY", "id TEXT CONSTRAINT `AUTOINCREMENT` PRIMARY KEY"),
            "segments": lambda sql: sql.replace("PRIMARY KEY AUTOINCREMENT", "PRIMARY -- CHECK(0)\n KEY /* REPLACE */ AUTOINCREMENT"),
        })

    def test_all_supported_identifier_quotes_and_keyword_case_are_accepted(self):
        identifiers = ("sessions", "segments", "summaries", "schema_metadata", "artifacts", "id", "title", "session_id",
                       "sequence", "started_at", "ended_at", "status", "source_type", "source_name", "source_language",
                       "summary_language", "created_at", "updated_at", "text", "language", "kind", "content", "model",
                       "capture_method", "source_segment_id", "schema_version")
        pattern = r"\b(" + "|".join(sorted(identifiers, key=len, reverse=True)) + r")\b"
        for opening, closing in (('"', '"'), ('`', '`'), ('[', ']'), ("'", "'")):
            with self.subTest(quote=opening):
                def transform(sql):
                    sql = re.sub(pattern, lambda match: opening + match.group(0) + closing, sql)
                    return sql.replace("CREATE TABLE", "create\n table").replace("NOT NULL", "not\t null")
                self.assert_accepted({name: transform for name in ("sessions", "segments", "summaries", "schema_metadata")})

    def test_escaped_quotes_in_constraint_names_are_accepted(self):
        for name in ('"CHECK"" AUTOINCREMENT"', '`CHECK`` AUTOINCREMENT`', "'CHECK'' AUTOINCREMENT'", '[CHECK " AUTOINCREMENT]'):
            with self.subTest(name=name):
                self.assert_accepted({"sessions": lambda sql, name=name: sql.replace("title TEXT NOT NULL", "title TEXT CONSTRAINT " + name + " NOT NULL")})

    def test_single_quoted_keyword_literals_are_not_constraints(self):
        sql = ("CREATE TABLE sample(id INTEGER PRIMARY KEY AUTOINCREMENT, "
               "content TEXT DEFAULT ('CHECK(''AUTOINCREMENT'') /* ON CONFLICT IGNORE */'))")
        semantics = storage._RequiredTableReader().read(sql, "sample")
        self.assertEqual(semantics.checks, 0)
        self.assertEqual(semantics.autoincrement, ("id",))
        path = self.variant_path(changes={"sessions": lambda sql: sql.replace("title TEXT NOT NULL", "title TEXT NOT NULL DEFAULT 'CHECK'' AUTOINCREMENT'")})
        self.assert_initial_rejection(path, "sütun tanımları")

    def test_equivalent_table_constraint_order_and_formatting_are_accepted(self):
        self.assert_accepted({
            "segments": lambda sql: sql.replace("UNIQUE (session_id, sequence)",
                'CONSTRAINT "uq" UNIQUE (session_id ASC, sequence ASC) ON CONFLICT ABORT'),
            "summaries": lambda sql: sql.replace("session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE",
                "session_id TEXT REFERENCES sessions(id) ON UPDATE NO ACTION ON DELETE CASCADE COLLATE BINARY NOT NULL ON CONFLICT ABORT NOT DEFERRABLE"),
        })

    def test_repeated_constraints_and_collations_fail_closed(self):
        for replacement in ("title TEXT NOT NULL NOT NULL", "title TEXT COLLATE BINARY COLLATE BINARY NOT NULL"):
            path = self.variant_path(changes={"sessions": lambda sql, replacement=replacement: sql.replace("title TEXT NOT NULL", replacement)})
            self.assert_initial_rejection(path, "desteklenmeyen/belirsiz CREATE TABLE")

    def test_incomplete_or_trailing_required_sql_is_never_partially_accepted(self):
        statements = (
            "CREATE TABLE sample(id INTEGER PRIMARY KEY AUTOINCREMENT) SELECT 1",
            "CREATE TABLE sample(id INTEGER PRIMARY KEY AUTOINCREMENT); CREATE TABLE extra(a TEXT)",
            "CREATE TABLE sample(id INTEGER PRIMARY KEY AUTOINCREMENT /* never closed)",
            "CREATE TABLE sample(id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT DEFAULT ('unterminated))",
            "CREATE TABLE sample(id INTEGER PRIMARY KEY AUTOINCREMENT,)",
            "CREATE TABLE sample(id INTEGER PRIMARY KEY AUTOINCREMENT",
        )
        for statement in statements:
            with self.subTest(statement=statement):
                with self.assertRaises(storage._TableSqlError):
                    storage._RequiredTableReader().read(statement, "sample")

    def test_unknown_required_sql_syntax_is_not_treated_as_plain_text(self):
        for tail in ("ON CONFLICT ABORT", "AUTOINCREMENT", "GENERATED ALWAYS AS (1)", "mystery_clause"):
            statement = "CREATE TABLE sample(id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT " + tail + ")"
            with self.subTest(tail=tail):
                with self.assertRaises(storage._TableSqlError):
                    storage._RequiredTableReader().read(statement, "sample")

    def test_strict_unversioned_legacy_accepts_safe_semantic_equivalents(self):
        self.assert_accepted({
            "sessions": lambda sql: sql.replace("title TEXT NOT NULL", 'title TEXT COLLATE "BINARY" CONSTRAINT "CHECK" NOT NULL ON CONFLICT ABORT'),
            "segments": lambda sql: sql.replace("ON DELETE CASCADE", "ON DELETE CASCADE NOT DEFERRABLE INITIALLY DEFERRED"),
        }, legacy=True)

    def test_unversioned_legacy_rejects_behavior_changes(self):
        for changes, detail in (
            ({"sessions": lambda sql: sql[:-1] + ", CHECK(1))"}, "CHECK"),
            (self.conflict_cases("REPLACE")[2], "ON CONFLICT"),
            ({"summaries": lambda sql: sql.replace("ON DELETE CASCADE", "ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED")}, "FK zamanlaması"),
        ):
            self.assert_initial_rejection(self.variant_path(legacy=True, changes=changes), detail)

    def test_versioned_extension_sql_is_not_parsed_as_required_schema(self):
        extras = (
            "CREATE TABLE extension (id INTEGER PRIMARY KEY ON CONFLICT REPLACE, "
            "text_value VARCHAR(20) COLLATE NOCASE CHECK(length(text_value)>0), "
            "derived INTEGER GENERATED ALWAYS AS (length(text_value)) VIRTUAL)",
            "CREATE TRIGGER extension_trigger AFTER INSERT ON extension BEGIN SELECT 'CHECK AUTOINCREMENT'; END",
        )
        for version in (1, 2):
            path = self.variant_path(version=version, extras=extras)
            db = self.new_db(path, allow_migration=True)
            db.init_schema()
            self.assertEqual(db.get_connection().execute("SELECT name FROM sqlite_schema WHERE name='extension_trigger'").fetchone()[0], "extension_trigger")

    def test_initial_authorization_gate_distinguishes_valid_and_unsafe_v1(self):
        valid = self.variant_path()
        with self.assertRaises(SchemaMigrationError) as caught:
            self.new_db(valid).get_connection()
        self.assertEqual(caught.exception.reason, "authorization_required")
        invalid = self.variant_path(changes=self.conflict_cases("REPLACE")[2])
        db = self.new_db(invalid)
        with mock.patch.object(db, "_get_raw_connection") as raw:
            with self.assertRaises(SchemaVersionError) as caught:
                db.get_connection()
        self.assertEqual(caught.exception.reason, "inconsistent_schema")
        raw.assert_not_called()
        self.assertEqual(list_backups(valid), ([], []))
        self.assertEqual(list_backups(invalid), ([], []))

    def test_under_lock_semantic_reclassification_rejects_before_backup_or_ddl(self):
        cases = (
            ({"sessions": lambda sql: sql[:-1] + ", CHECK(1))"}, "CHECK"),
            (self.conflict_cases("IGNORE")[2], "ON CONFLICT"),
            ({"segments": lambda sql: sql.replace("ON DELETE CASCADE", "ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED")}, "FK zamanlaması"),
            ({"sessions": lambda sql: sql.replace("title TEXT NOT NULL", "title TEXT COLLATE NOCASE NOT NULL")}, "COLLATE"),
            ({"segments": lambda sql: sql.replace("AUTOINCREMENT", "/* AUTOINCREMENT */")}, "AUTOINCREMENT"),
        )
        for changes, detail in cases:
            path = self.variant_path(changes=changes)
            before = snapshot(path)
            db = self.new_db(path, allow_migration=True)
            with mock.patch.object(db, "_inspect_readonly", return_value=storage._SchemaState(storage._STATE_FRESH)), \
                    mock.patch.object(db, "_create_backup") as backup, mock.patch.object(db, "_execute_ddl_statement") as ddl:
                with self.assertRaises(SchemaVersionError) as caught:
                    db.init_schema()
            self.assertIn(detail, str(caught.exception))
            self.assertEqual(caught.exception.transaction_outcome, "rolled_back")
            backup.assert_not_called()
            ddl.assert_not_called()
            self.assertEqual(snapshot(path), before)
            self.assertIsNone(db._local.conn)
            self.assertFalse(db._local.schema_ready)

    def test_post_ddl_semantic_failure_rolls_back_and_preserves_finalized_backup(self):
        changes = (
            (lambda sql: sql.replace("content TEXT NOT NULL", "content TEXT NOT NULL CHECK(length(content)>=0)"), "CHECK"),
            (lambda sql: sql.replace("content TEXT NOT NULL", "content TEXT NOT NULL ON CONFLICT REPLACE"), "ON CONFLICT"),
            (lambda sql: sql.replace("REFERENCES segments(id, session_id)", "REFERENCES segments(id, session_id) DEFERRABLE INITIALLY DEFERRED"), "FK zamanlaması"),
            (lambda sql: sql.replace("content TEXT NOT NULL", "content TEXT COLLATE RTRIM NOT NULL"), "COLLATE"),
            (lambda sql: sql.replace("AUTOINCREMENT", "/* AUTOINCREMENT */"), "AUTOINCREMENT"),
        )
        for change, detail in changes:
            with self.subTest(detail=detail):
                path = self.variant_path()
                before = snapshot(path)
                db = self.new_db(path, allow_migration=True)
                execute = db._execute_ddl_statement

                def mutate(conn, statement):
                    if "CREATE TABLE artifacts" in statement:
                        statement = change(" ".join(statement.split()))
                    execute(conn, statement)

                with mock.patch.object(db, "_execute_ddl_statement", side_effect=mutate):
                    with self.assertRaises(SchemaMigrationError) as caught:
                        db.init_schema()
                self.assertIn(detail, str(caught.exception))
                self.assertEqual(caught.exception.reason, "migration_ddl_failed")
                self.assertEqual(caught.exception.transaction_outcome, "rolled_back")
                self.assertEqual(snapshot(path), before)
                self.assertIsNone(db._local.conn)
                finals, others = list_backups(path)
                self.assertEqual((len(finals), others), (1, []))
                self.assert_valid_v1_backup(finals[0], before)
                backup_hash = sha256_file(finals[0])
                db.init_schema()
                self.assertEqual(sha256_file(finals[0]), backup_hash)
                self.assert_v2_structure(path)

    def test_actual_sql_semantics_are_reparsed_on_each_read(self):
        path = self.variant_path(version=2)
        conn = sqlite3.connect(str(path))
        try:
            first = storage._read_table_shape(conn, "artifacts")
            conn.execute("DROP TABLE artifacts")
            conn.execute(self.v2_statements[1].replace("content TEXT NOT NULL", "content TEXT COLLATE NOCASE NOT NULL"))
            second = storage._read_table_shape(conn, "artifacts")
            self.assertEqual(first.semantics.collations[3], ("content", "BINARY"))
            self.assertEqual(second.semantics.collations[3], ("content", "NOCASE"))
        finally:
            conn.close()

    def test_independent_autoincrement_probe_ignores_sql_keyword_decoys(self):
        ordinary = self.variant_path()
        spoofed = self.variant_path(changes={"segments": lambda sql: sql.replace("AUTOINCREMENT", "/* AUTOINCREMENT */")})
        self.assertTrue(structure_signature(ordinary)["tables"]["segments"]["autoincrement"])
        self.assertFalse(structure_signature(spoofed)["tables"]["segments"]["autoincrement"])


class TestUnicodeSchemaNames(MigrationTestCase):
    """ASCII ad eşlemesini gerçek Unicode kolasyonlu, geçici SQLite ile sınar."""

    variant_path = TestStructuralFingerprintPackageThreeA.variant_path
    v2_statements = TestStructuralFingerprintPackageThreeA.v2_statements
    assert_initial_rejection = TestStructuralFingerprintPackageThreeA.assert_initial_rejection
    assert_accepted = TestSemanticFingerprintPackageThreeB.assert_accepted

    @staticmethod
    def register_unicode_collation(conn):
        conn.create_collation("bınary", lambda a, b: (b > a) - (b < a))

    def unicode_collation_path(self, declaration, version=1, legacy=False):
        path = self.variant_path(version=version, legacy=legacy, history=False)
        conn = sqlite3.connect(str(path))
        try:
            self.register_unicode_collation(conn)
            # Gerçek SQLite bu adı BINARY'den ayrı tutar ve farklı karşılaştırma uygular.
            self.assertEqual(conn.execute("SELECT 'a' < 'b' COLLATE BINARY").fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT \'a\' < \'b\' COLLATE "bınary"').fetchone()[0], 0)
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("DROP TABLE sessions")
            conn.execute(V1_FIXTURE_DDL[0].replace("title TEXT NOT NULL", "title TEXT COLLATE " + declaration + " NOT NULL"))
            seed_history(conn)
            conn.commit()
        finally:
            conn.close()
        return path

    def test_quoted_unicode_collation_is_rejected_during_preliminary_classification(self):
        for version, legacy in ((1, False), (2, False), (1, True)):
            with self.subTest(version=version, legacy=legacy):
                path = self.unicode_collation_path('"bınary"', version=version, legacy=legacy)
                self.assert_initial_rejection(path, "COLLATE")

    def test_unquoted_unicode_collation_is_rejected_during_preliminary_classification(self):
        for version in (1, 2):
            with self.subTest(version=version):
                self.assert_initial_rejection(self.unicode_collation_path("bınary", version=version), "COLLATE")

    def test_ascii_mixed_case_binary_remains_accepted(self):
        for declaration in ("bInArY", '"BiNaRy"'):
            for version, legacy in ((1, False), (2, False), (1, True)):
                with self.subTest(declaration=declaration, version=version, legacy=legacy):
                    changes = {table: lambda sql, declaration=declaration: re.sub(
                        r"\b(TEXT|INTEGER)\b", lambda match: match.group(0) + " COLLATE " + declaration, sql,
                    ) for table in ("sessions", "segments", "summaries", "schema_metadata", "artifacts")}
                    self.assert_accepted(changes, version=version, legacy=legacy)

    def test_reader_preserves_unicode_identifier_codepoints(self):
        for name, expected in (("BıNARY", "bınary"), ("KIND", "Kind"), ("İD", "İd"), ("ſession", "ſession"), ("Straße", "straße")):
            for quoted in (name, '"' + name + '"', "`" + name + "`", "[" + name + "]", "'" + name + "'"):
                with self.subTest(name=name, quoted=quoted):
                    cursor = storage._TableSqlCursor(storage._table_sql_tokens(quoted))
                    self.assertEqual(cursor.identifier(), expected)
                    self.assertTrue(cursor.finished())

    def test_unicode_table_column_and_fk_names_do_not_alias_ascii_names(self):
        with self.assertRaises(storage._TableSqlError):
            storage._RequiredTableReader().read('CREATE TABLE "K"(id INTEGER)', "k")
        semantics = storage._RequiredTableReader().read(
            'CREATE TABLE "K"("Kind" TEXT CONSTRAINT "Key" NOT NULL REFERENCES "K"("Kind"))', "K",
        )
        self.assertEqual(semantics.collations, (("Kind", "BINARY"),))
        self.assertEqual(semantics.conflicts, (("NOT NULL", ("Kind",), "ABORT"),))
        self.assertEqual(semantics.foreign_keys, ((("K", "NO ACTION", "NO ACTION", "NONE", (("Kind", "Kind"),)), False),))
        path = self.variant_path(history=False, changes={"sessions": lambda sql: sql.replace("title TEXT", '"tıtle" TEXT')})
        self.assert_initial_rejection(path, "sütun tanımları")

    def test_unicode_keyword_and_type_spoofs_fail_closed(self):
        for declaration in ("PRıMARY KEY", "CONſTRAINT named PRIMARY KEY"):
            with self.subTest(declaration=declaration):
                with self.assertRaises(storage._TableSqlError):
                    storage._RequiredTableReader().read("CREATE TABLE sample(id INTEGER " + declaration + ")", "sample")
        with self.assertRaises(storage._TableSqlError):
            storage._RequiredTableReader().read("CREATE TABLE sample(id ıNTEGER)", "sample")

    def test_ascii_case_variants_still_match_keywords_names_and_fk_targets(self):
        semantics = storage._RequiredTableReader().read(
            'cReAtE tAbLe SaMpLe (ID InTeGeR CoNsTrAiNt MiXeD PrImArY kEy AuToInCrEmEnT, '
            'PaReNt_Id TeXt ReFeReNcEs PaReNt(ID) CoLlAtE bInArY)', "sAmPlE",
        )
        self.assertEqual(semantics.autoincrement, ("id",))
        self.assertEqual(semantics.collations, (("id", "BINARY"), ("parent_id", "BINARY")))
        self.assertEqual(semantics.foreign_keys, ((("parent", "NO ACTION", "NO ACTION", "NONE", (("parent_id", "id"),)), False),))

    def test_unicode_collation_is_rejected_under_lock_without_backup_or_ddl(self):
        for declaration in ('"bınary"', "bınary"):
            with self.subTest(declaration=declaration):
                path = self.unicode_collation_path(declaration)
                before = snapshot(path)
                db = self.new_db(path, allow_migration=True)
                with mock.patch.object(db, "_inspect_readonly", return_value=storage._SchemaState(storage._STATE_FRESH)), \
                        mock.patch.object(db, "_create_backup") as backup, mock.patch.object(db, "_execute_ddl_statement") as ddl:
                    with self.assertRaises(SchemaVersionError) as caught:
                        db.init_schema()
                self.assertEqual(caught.exception.reason, "inconsistent_schema")
                self.assertIn("COLLATE", str(caught.exception))
                self.assertEqual(caught.exception.transaction_outcome, "rolled_back")
                backup.assert_not_called()
                ddl.assert_not_called()
                self.assertEqual(snapshot(path), before)
                self.assertIsNone(db._local.conn)
                self.assertFalse(db._local.schema_ready)
                self.assertEqual(list_backups(path), ([], []))

    def test_post_ddl_unicode_collation_rejection_rolls_back_and_preserves_backup(self):
        for declaration in ('"bınary"', "bınary"):
            with self.subTest(declaration=declaration):
                path = self.v1_path()
                before = snapshot(path)
                db = self.new_db(path, allow_migration=True)
                execute = db._execute_ddl_statement
                finalized = []

                def mutate(conn, statement):
                    if "CREATE TABLE artifacts" in statement:
                        self.register_unicode_collation(conn)
                        statement = " ".join(statement.split()).replace(
                            "content TEXT NOT NULL", "content TEXT COLLATE " + declaration + " NOT NULL",
                        )
                        finals, others = list_backups(path)
                        self.assertEqual((len(finals), others), (1, []))
                        finalized.append((finals[0], sha256_file(finals[0])))
                    execute(conn, statement)

                with mock.patch.object(db, "_execute_ddl_statement", side_effect=mutate):
                    with self.assertRaises(SchemaMigrationError) as caught:
                        db.init_schema()
                self.assertEqual(caught.exception.reason, "migration_ddl_failed")
                self.assertIn("COLLATE", str(caught.exception))
                self.assertEqual(caught.exception.transaction_outcome, "rolled_back")
                self.assertEqual(snapshot(path), before)
                self.assertIsNone(db._local.conn)
                self.assertFalse(db._local.schema_ready)
                self.assertEqual(len(finalized), 1)
                backup_path, backup_hash = finalized[0]
                self.assertEqual(list_backups(path), ([backup_path], []))
                self.assertEqual(sha256_file(backup_path), backup_hash)
                self.assert_valid_v1_backup(backup_path, before)


class TestSqliteLexicalWhitespace(MigrationTestCase):
    """SQLite'ın ad saydığı Unicode boşluklarının kolasyon davranışını gizleyemediğini sınar."""

    variant_path = TestStructuralFingerprintPackageThreeA.variant_path
    v2_statements = TestStructuralFingerprintPackageThreeA.v2_statements
    assert_initial_rejection = TestStructuralFingerprintPackageThreeA.assert_initial_rejection

    @staticmethod
    def register_collation(conn, name):
        conn.create_collation(name, lambda a, b: (b > a) - (b < a))

    def spoof_path(self, prefix, version=1, legacy=False):
        path = self.variant_path(version=version, legacy=legacy, history=False)
        with closing(sqlite3.connect(str(path))) as conn:
            name = prefix + "BINARY"
            self.register_collation(conn, name)
            self.assertEqual(conn.execute('SELECT \'a\' < \'b\' COLLATE "' + name + '"').fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT 'a' < 'b' COLLATE BINARY").fetchone()[0], 1)
            conn.execute("DROP TABLE sessions")
            conn.execute(V1_FIXTURE_DDL[0].replace("title TEXT NOT NULL", "title TEXT COLLATE " + name + " NOT NULL"))
            seed_history(conn)
            conn.commit()
        return path

    def test_only_sqlite_lexical_whitespace_is_discarded(self):
        with closing(sqlite3.connect(":memory:")) as conn:
            for character in " \t\n\r\f":
                self.assertEqual(conn.execute("SELECT" + character + "1").fetchone()[0], 1)
                self.assertEqual(storage._table_sql_tokens(character + "BINARY")[0].value, "BINARY")
            for character in ("\v", "\x1c", "\u00a0", "\u2003", "\u202f"):
                with self.subTest(character=repr(character)):
                    if ord(character) < 128:
                        with self.assertRaises(storage._TableSqlError):
                            storage._table_sql_tokens(character + "BINARY")
                    else:
                        self.assertEqual(storage._table_sql_tokens(character + "BINARY")[0].value, character + "BINARY")

    def test_preliminary_rejects_nonbinary_unicode_prefix_for_v1_v2_and_legacy(self):
        for prefix in ("\u00a0", "\u2003"):
            for version, legacy in ((1, False), (2, False), (1, True)):
                with self.subTest(prefix=repr(prefix), version=version, legacy=legacy):
                    path = self.spoof_path(prefix, version, legacy)
                    self.assert_initial_rejection(path, "COLLATE")

    def test_default_authorization_remains_denied_for_valid_v1(self):
        path = self.v1_path()
        before = snapshot(path)
        with self.assertRaises(SchemaMigrationError) as caught:
            self.new_db(path).get_connection()
        self.assertEqual(caught.exception.reason, "authorization_required")
        self.assertEqual(snapshot(path), before)
        self.assertEqual(list_backups(path), ([], []))

    def test_under_lock_rejection_rolls_back_without_backup_or_ddl(self):
        for prefix in ("\u00a0", "\u2003"):
            with self.subTest(prefix=repr(prefix)):
                path = self.spoof_path(prefix)
                before = snapshot(path)
                db = self.new_db(path, allow_migration=True)
                with mock.patch.object(db, "_inspect_readonly", return_value=storage._SchemaState(storage._STATE_FRESH)), \
                        mock.patch.object(db, "_create_backup") as backup, mock.patch.object(db, "_execute_ddl_statement") as ddl:
                    with self.assertRaises(SchemaVersionError) as caught:
                        db.init_schema()
                self.assertEqual(caught.exception.transaction_outcome, "rolled_back")
                self.assertIn("COLLATE", str(caught.exception))
                backup.assert_not_called()
                ddl.assert_not_called()
                self.assertEqual(snapshot(path), before)
                self.assertIsNone(db._local.conn)
                self.assertFalse(db._local.schema_ready)

    def test_post_ddl_rejection_preserves_history_and_finalized_backup(self):
        for prefix in ("\u00a0", "\u2003"):
            with self.subTest(prefix=repr(prefix)):
                path = self.v1_path()
                before = snapshot(path)
                db = self.new_db(path, allow_migration=True)
                execute = db._execute_ddl_statement
                finalized = []

                def mutate(conn, statement):
                    if "CREATE TABLE artifacts" in statement:
                        self.register_collation(conn, prefix + "BINARY")
                        statement = " ".join(statement.split()).replace(
                            "content TEXT NOT NULL", "content TEXT COLLATE " + prefix + "BINARY NOT NULL")
                        finals, others = list_backups(path)
                        self.assertEqual((len(finals), others), (1, []))
                        finalized.append((finals[0], sha256_file(finals[0])))
                    execute(conn, statement)

                with mock.patch.object(db, "_execute_ddl_statement", side_effect=mutate):
                    with self.assertRaises(SchemaMigrationError) as caught:
                        db.init_schema()
                self.assertEqual(caught.exception.reason, "migration_ddl_failed")
                self.assertEqual(caught.exception.transaction_outcome, "rolled_back")
                self.assertIn("COLLATE", str(caught.exception))
                self.assertEqual(snapshot(path), before)
                self.assertIsNone(db._local.conn)
                backup, digest = finalized[0]
                self.assertEqual(list_backups(path), ([backup], []))
                self.assertEqual(sha256_file(backup), digest)
                self.assert_valid_v1_backup(backup, before)


class TestDiagnosticFailureSafety(LifecycleFaultMixin, MigrationTestCase):
    """Raporlama hatalarının asıl göç hatasını ve thread-yerel iptali bozamamasını sınar."""

    def test_success_logging_failure_preserves_ready_connection_and_revalidation(self):
        path = self._db_dir("success_logging") / "disposable.db"
        db = self.new_db(path)
        logging_error = OSError("başarı günlüğü yazılamadı")

        def report(message, *args, **kwargs):
            if message.startswith("Veritabanı hazır:"):
                raise logging_error

        with mock.patch.object(storage.log, "info", side_effect=report):
            db.init_schema()
        self.assertTrue(db._local.schema_ready)
        conn = db.get_connection()
        self.assertIs(conn, db._local.conn)
        self.assertIs(db._local.initialization_logging_error, logging_error)
        self.assertEqual(conn.execute("SELECT schema_version FROM schema_metadata").fetchall()[0][0], SCHEMA_VERSION)
        conn.execute("BEGIN")
        with mock.patch.object(storage.log, "info", side_effect=report), \
                mock.patch.object(db, "_inspect_readonly", wraps=db._inspect_readonly) as inspect:
            db.init_schema()
        inspect.assert_called_once()
        self.assertTrue(db._local.schema_ready)
        self.assertIs(db.get_connection(), conn)
        self.assertTrue(conn.in_transaction)
        conn.rollback()
        db.init_schema()
        self.assertIsNone(db._local.initialization_logging_error)
        self.assertIs(db.get_connection(), conn)

    def test_close_and_logging_failures_preserve_primary_identity_and_traceback(self):
        db = self.new_db(self.fresh_path())
        primary = SchemaVersionError("asıl hata")
        close_error = OSError("kapatma hatası")
        logging_error = RuntimeError("günlük hatası")
        conn = mock.Mock()
        conn.close.side_effect = close_error
        db._local.conn, db._local.schema_ready = conn, True
        try:
            raise primary
        except SchemaVersionError:
            original_traceback = primary.__traceback__
            with mock.patch.object(storage.log, "error", side_effect=logging_error):
                db._invalidate_connection(primary)
            self.assertIs(primary.__traceback__, original_traceback)
        self.assertIsNone(db._local.conn)
        self.assertFalse(db._local.schema_ready)
        self.assertIs(primary.close_error, close_error)
        self.assertEqual(primary.logging_errors, (logging_error,))
        conn.close.assert_called_once()

    def test_revalidation_reporting_keeps_original_error_and_close_diagnostics(self):
        db = self.new_db(self.fresh_path())
        original = SchemaVersionError("asıl ret")
        conn = mock.Mock()
        conn.close.side_effect = OSError("kapatma")
        db._local.conn, db._local.schema_ready = conn, True
        with mock.patch.object(db, "_inspect_readonly", side_effect=original), \
                mock.patch.object(storage.log, "error", side_effect=RuntimeError("günlük")):
            with self.assertRaises(SchemaVersionError) as caught:
                db.init_schema()
        self.assertIs(caught.exception, original)
        self.assertEqual(len(original.logging_errors), 2)
        self.assertIsNone(db._local.conn)
        self.assertFalse(db._local.schema_ready)

    def test_rollback_interrupt_reporting_does_not_replace_original_interrupt(self):
        db = self.new_db(self.fresh_path())
        conn = mock.Mock(in_transaction=True)
        conn.rollback.side_effect = OSError("geri alma")
        db._local.conn = conn
        interrupt = KeyboardInterrupt("asıl kesme")
        with mock.patch.object(storage.log, "error", side_effect=RuntimeError("günlük")):
            self.assertEqual(db._rollback_transaction(conn, interrupt), "unknown")
        self.assertEqual(interrupt.transaction_outcome, "unknown")
        self.assertEqual(len(interrupt.logging_errors), 1)
        self.assertIsNone(db._local.conn)

    def test_backup_cleanup_and_logging_failures_preserve_backup_error(self):
        path = self.v1_path()
        db = self.new_db(path, allow_migration=True)
        before = snapshot(path)
        with mock.patch.object(storage, "_online_backup_copy", side_effect=OSError("yedek kopya")), \
                mock.patch.object(Path, "unlink", side_effect=PermissionError("geçici dosya")), \
                mock.patch.object(storage.log, "warning", side_effect=RuntimeError("günlük")):
            with self.assertRaises(SchemaMigrationError) as caught:
                db.init_schema()
        self.assertEqual(caught.exception.reason, "backup_failed")
        self.assertEqual(str(caught.exception.__cause__), "yedek kopya")
        self.assertTrue(caught.exception.cleanup_errors)
        self.assertTrue(all(error.logging_errors for error in caught.exception.cleanup_errors))
        self.assertEqual(caught.exception.cleanup_errors, db._local.backup_cleanup_errors)
        self.assertEqual(snapshot(path), before)


if __name__ == "__main__":
    unittest.main()
