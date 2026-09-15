"""
SQLite bağlantı yönetimi ve şema.

Tasarım notu (thread safety): Vesper zaten ayrı thread'ler kullanıyor
(yakalama, transkripsiyon, özetleme, + NiceGUI'nin kendi event loop'u).
Tek bir sqlite3.Connection'ı bu thread'ler arasında paylaşmak yerine, her
thread ilk erişiminde KENDİ bağlantısını açar (threading.local). Aynı dosyaya
birden fazla bağlantı WAL modunda güvenlidir; SQLite bunu tam olarak bu
senaryo için tasarlamıştır.

Bu modül SQL YAZMAZ (CREATE/PRAGMA hariç) — satır ekleme/silme/güncelleme
repository.py'de. UI bu dosyayı hiç import etmemeli.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Union

log = logging.getLogger("storage")

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id                TEXT PRIMARY KEY,
    title             TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    ended_at          TEXT,
    status            TEXT NOT NULL,
    source_type       TEXT NOT NULL,
    source_name       TEXT,
    source_language   TEXT,
    summary_language  TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS segments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    sequence    INTEGER NOT NULL,
    started_at  TEXT NOT NULL,
    ended_at    TEXT NOT NULL,
    text        TEXT NOT NULL,
    language    TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE (session_id, sequence)
);

CREATE TABLE IF NOT EXISTS summaries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    content     TEXT NOT NULL,
    model       TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_metadata (
    schema_version INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_segments_session   ON segments(session_id, sequence);
CREATE INDEX IF NOT EXISTS idx_summaries_session  ON summaries(session_id, kind, created_at);
CREATE INDEX IF NOT EXISTS idx_sessions_started   ON sessions(started_at DESC);
"""


class Database:
    """Tek bir .db dosyasına thread-local bağlantı sağlar.

    Üretimde bir tane paylaşılır; her thread ilk erişiminde kendi SQLite
    bağlantısını açar. close() yalnızca çağıran thread'in bağlantısını
    kapatır; böylece sqlite3'ün thread affinity kuralı korunur.
    """

    def __init__(self, db_path: Union[str, Path]):
        self.db_path = Path(db_path)
        self._local = threading.local()

    def get_connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path), timeout=5.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return conn

    def init_schema(self) -> None:
        conn = self.get_connection()
        with conn:
            conn.executescript(SCHEMA_SQL)
            row = conn.execute(
                "SELECT schema_version FROM schema_metadata"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_metadata (schema_version) VALUES (?)",
                    (SCHEMA_VERSION,),
                )
        log.info(
            "Veritabanı hazır: %s (schema v%d)",
            self.db_path,
            SCHEMA_VERSION,
        )

    def close(self) -> None:
        """Yalnızca çağıran thread'e ait SQLite bağlantısını kapatır."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None