"""
SQLite bağlantı yönetimi, şema ve kontrollü şema göçü.

Tasarım notu (thread safety): Vesper zaten ayrı thread'ler kullanıyor
(yakalama, transkripsiyon, özetleme, + NiceGUI'nin kendi event loop'u).
Tek bir sqlite3.Connection'ı bu thread'ler arasında paylaşmak yerine, her
thread ilk erişiminde KENDİ bağlantısını açar (threading.local). Aynı dosyaya
birden fazla bağlantı WAL modunda güvenlidir; SQLite bunu tam olarak bu
senaryo için tasarlamıştır.

Şema sürümü ve göç (v0.5.0, Checkpoint A):
  * SCHEMA_VERSION = 2. Sürümün TEK otoritesi `schema_metadata` tablosudur;
    ikinci bir sürüm kaynağı (PRAGMA tabanlı) KULLANILMAZ.
  * init_schema() veritabanının durumunu AÇIKÇA sınıflandırır ve gerçek SQLite
    yapısını (tablo, sütun, tür, NOT NULL, birincil anahtar, yabancı anahtar,
    dizin, dizin benzersizliği/sütun sırası) doğrular; yalnızca sürüm
    numarasına güvenmez:
      - BOŞ (FRESH)        : doğrudan v2 olarak kurulur. İzin ve yedek gerekmez.
      - GEÇERLİ v2         : hiçbir şey yapılmaz. İzin ve yedek gerekmez.
      - GEÇERLİ v1         : yalnızca Database(..., allow_migration=True) ile
                             göç eder; izin yoksa SchemaMigrationError ile
                             REDDEDİLİR (bu deneme yedek veya göç DDL'i başlatmaz).
      - SÜRÜMSÜZ v1        : boş schema_metadata + KATI v1 parmak izi; v1 gibi
                             (izin gerekir). Hasarlı bir veritabanı v1 sayılmaz.
      - YENİ (> v2)        : HER ZAMAN SchemaVersionError ile reddedilir.
      - TUTARSIZ/HASARLI   : SchemaVersionError ile reddedilir; ASLA onarılmaz.
  * Göç izni VARSAYILAN OLARAK KAPALIDIR. Yoldan, ortam değişkeninden, test/debug
    durumundan veya global bir ayardan TÜRETİLMEZ; yalnızca çağıranın açıkça
    verdiği `allow_migration` parametresiyle (ve yalnızca o Database örneği için)
    açılır. İzin; şema doğrulamasını ve yedeği ATLAMAZ.

Göç/kilit modeli (yetkili v1 -> v2):
  1. Salt-okunur ön sınıflandırma (kendi kısa ömürlü bağlantısı; yalnızca mode=ro;
     açılamazsa reddedilir). WAL yan dosyalarında sıfır etki garantisi verilmez.
  2. Normal thread-local bağlantı açılır; yalnızca göç için 30 sn busy timeout.
  3. BEGIN IMMEDIATE (SQLite'ın yazma kilidi, süreçler arası serileştirme).
  4. Kilit ALTINDA yeniden sınıflandırma (başka süreç göç ettiyse sessizce dönülür).
  5. Mevcut yabancı anahtar ihlali varsa REDDEDİLİR (onarılmaz).
  6. Tarihsel satır varsa, AYRI bir kaynak bağlantısıyla SQLite çevrimiçi yedek
     API'si ile yedek alınır ve doğrulanır (bağlantı BEGIN IMMEDIATE'i tutan
     bağlantı DEĞİLDİR; o bağlantıdan backup() çağrısı asla yapılmaz).
  7. DDL tek tek (executescript YOK), sürüm satırı güncellenir, v2 doğrulanır,
     foreign_key_check temiz olmalı, COMMIT. Hata -> geri almayı dene/doğrula;
     belirsiz sonuçta bağlantıyı iptal et. Tamamlanmış yedek korunur.
  Python threading kilidi SQLite yedekleme işlemi boyunca TUTULMAZ.

Zaman aşımı notu: sqlite3 backup() ilerleme geri-çağrısındaki süre sınırı EN İYİ
ÇABA bir mekanizmadır; yerel (native) çağrı geri-çağrıya hiç uğramadan bloke
olursa katı bir duvar-saati zaman aşımı GARANTİ ETMEZ. Garanti değil, sınırlı
SQLite busy timeout'ları ve bu geri-çağrı birlikte kullanılır.

Bu modül satır ekleme/silme/güncelleme SQL'i YAZMAZ (şema, PRAGMA ve göç
SQL'i hariç) — veri işlemleri repository.py'de. UI bu dosyayı hiç import
etmemeli.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import os
import secrets
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple, Union

log = logging.getLogger("storage")

SCHEMA_VERSION = 2

# --------------------------------------------------------------------------- #
# Zaman aşımı / yedekleme sabitleri
# --------------------------------------------------------------------------- #
# Normal uygulama bağlantısı (v0.2.0'dan beri DEĞİŞMEDİ).
_NORMAL_TIMEOUT_SEC = 5.0
_NORMAL_BUSY_TIMEOUT_MS = 5000

# Yalnızca şema göçü sırasında yazma kilidini beklemek için ayrılmış busy timeout.
MIGRATION_BUSY_TIMEOUT_SEC = 30.0
# Salt-okunur ön inceleme / yedek doğrulama bağlantılarının busy timeout'u.
INSPECTION_BUSY_TIMEOUT_SEC = 1.0
# Yedek kaynak bağlantısının busy timeout'u (kısa tutulur: geri-çağrı sık çalışsın).
BACKUP_SOURCE_BUSY_TIMEOUT_SEC = 1.0
# Yedek ilerleme geri-çağrısı için EN İYİ ÇABA süre sınırı (sert zaman aşımı DEĞİL).
BACKUP_PROGRESS_DEADLINE_SEC = 60.0
# Geçici yedek dosyasını son adına taşırken (ör. Windows'ta tarayıcı yazılımı
# dosyayı kısa süre tutuyorsa) SINIRLI sayıda yeniden deneme.
BACKUP_RENAME_ATTEMPTS = 5
BACKUP_RENAME_DELAY_SEC = 0.2
# Yedekler, veritabanı dosyasının bulunduğu dizinin altındaki bu klasöre yazılır
# (yol SABİT KODLANMAZ: Database.db_path.parent / BACKUP_DIR_NAME).
BACKUP_DIR_NAME = "backups"

# --------------------------------------------------------------------------- #
# Hata sınıfları
# --------------------------------------------------------------------------- #
REASON_AUTHORIZATION_REQUIRED = "authorization_required"
REASON_NEWER_SCHEMA = "newer_schema"
REASON_INCONSISTENT_SCHEMA = "inconsistent_schema"
REASON_UNREADABLE = "unreadable"
REASON_FK_VIOLATIONS = "foreign_key_violations"
REASON_LOCK_TIMEOUT = "lock_timeout"
REASON_BACKUP_FAILED = "backup_failed"
REASON_DDL_FAILED = "migration_ddl_failed"
REASON_MIGRATION_FAILED = "migration_failed"
REASON_ROLLBACK_FAILED = "migration_rollback_failed"
REASON_COMMIT_FAILED = "migration_commit_failed"
REASON_CONNECTION_SETUP_FAILED = "connection_setup_failed"

_PREPARATION_EFFECTS_NOTE = (
    " WAL yan dosyaları veya bağlantı kurulumu dosya sistemini etkilemiş olabilir. "
    "Bağlantının iptali, commit edilmemiş uygulama yazılarını kaybettirebilir."
)


class SchemaMigrationError(RuntimeError):
    """Şema hazırlama/göç işlemi reddedildi veya başarısız oldu.

    `reason` makine-okunur kategoridir: authorization_required, lock_timeout,
    backup_failed, migration_ddl_failed, foreign_key_violations,
    migration_failed, migration_rollback_failed, migration_commit_failed,
    connection_setup_failed. İkincil hatalar asıl hata zincirini değiştirmez.
    """

    default_reason = REASON_MIGRATION_FAILED

    def __init__(
        self,
        message: str,
        reason: Optional[str] = None,
        db_path: Optional[Union[str, Path]] = None,
    ):
        super().__init__(message)
        self.reason = reason or self.default_reason
        self.db_path = None if db_path is None else Path(db_path)
        self.phase: Optional[str] = None
        self.transaction_outcome: Optional[str] = None
        self.original_reason: Optional[str] = None
        self.primary_error: Optional[BaseException] = None
        self.rollback_error: Optional[BaseException] = None
        self.close_error: Optional[BaseException] = None


class SchemaVersionError(SchemaMigrationError):
    """Veritabanının şeması/sürümü bu Vesper sürümü tarafından AÇILAMAZ.

    reason: newer_schema (desteklenenden yeni), inconsistent_schema (tutarsız,
    hasarlı, sürüm/şema uyuşmazlığı, çakışan nesneler) veya unreadable
    (şema durumu belirlenemedi). Reddetme otomatik şema onarımı yapmaz.
    """

    default_reason = REASON_INCONSISTENT_SCHEMA


# --------------------------------------------------------------------------- #
# Şema tanımları
# --------------------------------------------------------------------------- #
# v1 TEMEL ŞEMA (DONDURULMUŞ): v0.2.0'dan beri değişmeyen tablolar/dizinler.
# Bilerek `IF NOT EXISTS` KULLANILMAZ: beklenmeyen önceden var olan bir nesne
# sessizce kabul edilmek yerine yüksek sesle hata verir. Yalnızca BOŞ bir
# veritabanında çalıştırılır; mevcut v1 veritabanlarına dokunmaz.
_V1_BASELINE_STATEMENTS: Tuple[str, ...] = (
    """
    CREATE TABLE sessions (
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
    )
    """,
    """
    CREATE TABLE segments (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        sequence    INTEGER NOT NULL,
        started_at  TEXT NOT NULL,
        ended_at    TEXT NOT NULL,
        text        TEXT NOT NULL,
        language    TEXT,
        created_at  TEXT NOT NULL,
        UNIQUE (session_id, sequence)
    )
    """,
    """
    CREATE TABLE summaries (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        kind        TEXT NOT NULL,
        content     TEXT NOT NULL,
        model       TEXT,
        created_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE schema_metadata (
        schema_version INTEGER NOT NULL
    )
    """,
    "CREATE INDEX idx_segments_session   ON segments(session_id, sequence)",
    "CREATE INDEX idx_summaries_session  ON summaries(session_id, kind, created_at)",
    "CREATE INDEX idx_sessions_started   ON sessions(started_at DESC)",
)

# v1 -> v2 DDL (sıra ÖNEMLİDİR): bileşik yabancı anahtarın ebeveyn anahtarı olan
# benzersiz dizin, artifacts tablosundan ÖNCE kurulur. Dizin yoksa artifacts'a
# her yazma ve hatta oturum silme "foreign key mismatch" ile başarısız olur.
# Bileşik FK bilerek varsayılan NO ACTION kullanır (SET NULL session_id'yi de
# NULL'lardı; CASCADE ise bağlı artifact'ı sessizce silerdi).
_V2_STATEMENTS: Tuple[str, ...] = (
    """
    CREATE UNIQUE INDEX idx_segments_id_session
        ON segments(id, session_id)
    """,
    """
    CREATE TABLE artifacts (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id        TEXT NOT NULL
                          REFERENCES sessions(id) ON DELETE CASCADE,
        kind              TEXT NOT NULL,
        content           TEXT NOT NULL,
        capture_method    TEXT NOT NULL,
        source_segment_id INTEGER,
        created_at        TEXT NOT NULL,
        FOREIGN KEY (source_segment_id, session_id)
            REFERENCES segments(id, session_id)
    )
    """,
    """
    CREATE INDEX idx_artifacts_session
        ON artifacts(session_id, created_at, id)
    """,
    """
    CREATE INDEX idx_artifacts_segment
        ON artifacts(source_segment_id, session_id)
    """,
)

# v1 veritabanında ZATEN bulunursa göçü reddettiren çakışma adayları.
_V2_OBJECT_NAMES: Tuple[str, ...] = (
    "artifacts",
    "idx_segments_id_session",
    "idx_artifacts_session",
    "idx_artifacts_segment",
)

# Tarihsel veri tabloları (yedek kararı ve yedek doğrulaması için).
_HISTORY_TABLES: Tuple[Tuple[str, str], ...] = (
    ("sessions", "id"),
    ("segments", "id"),
    ("summaries", "id"),
)

# --------------------------------------------------------------------------- #
# Durum sınıflandırması
# --------------------------------------------------------------------------- #
_STATE_FRESH = "fresh"
_STATE_V1 = "v1"
_STATE_UNVERSIONED_V1 = "unversioned_v1"
_STATE_V2 = "v2"
_STATE_NEWER = "newer"
_STATE_INVALID = "invalid"


class _SchemaState(NamedTuple):
    kind: str
    detail: str = ""
    version: Optional[int] = None


class _TableSemantics(NamedTuple):
    """Yalnızca dondurulmuş Vesper tablolarının hedeflenen SQL davranışları."""

    checks: int
    conflicts: Tuple[Tuple[str, Tuple[str, ...], str], ...]
    foreign_keys: Tuple[Tuple[Tuple[Any, ...], bool], ...]
    collations: Tuple[Tuple[str, str], ...]
    autoincrement: Tuple[str, ...]


class _TableSqlError(ValueError):
    """Gerekli tablo bildirimi dar okuyucuyla güvenle tüketilemedi."""


class _SqlToken(NamedTuple):
    kind: str
    value: str


def _ascii_case(value: str, upper: bool = False) -> str:
    """Yalnızca ASCII harflerini dönüştürür; Unicode ad ayrımlarını aynen korur."""
    lowercase = "abcdefghijklmnopqrstuvwxyz"
    uppercase = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    source, target = (lowercase, uppercase) if upper else (uppercase, lowercase)
    return value.translate(str.maketrans(source, target))


def _table_sql_tokens(sql: str) -> List[_SqlToken]:
    """Yorumları atar; tırnaklı adları ve sabitleri anahtar sözcüklerden ayırır.

    İfade çözümleyicisi değildir. Parantez içindeki CHECK/DEFAULT ifadeleri
    yalnızca dengeli birim olarak tüketilir; SQL çalıştırılmaz.
    """
    if not isinstance(sql, str) or not sql or "\x00" in sql:
        raise _TableSqlError("boş veya geçersiz CREATE TABLE metni")
    tokens: List[_SqlToken] = []
    position = 0
    while position < len(sql):
        char = sql[position]
        # SQLite boşluğu: SPACE, TAB, LF, CR, FF; Unicode boşlukları adın parçasıdır.
        if char in " \t\n\r\f":
            position += 1
        elif sql.startswith("--", position):
            end = sql.find("\n", position + 2)
            position = len(sql) if end < 0 else end + 1
        elif sql.startswith("/*", position):
            end = sql.find("*/", position + 2)
            if end < 0:
                raise _TableSqlError("kapanmayan SQL yorumu")
            position = end + 2
        elif char in "'\"`[":
            closing = "]" if char == "[" else char
            kind = "literal" if char == "'" else "identifier"
            value = []
            position += 1
            while position < len(sql):
                current = sql[position]
                position += 1
                if current == closing:
                    if char != "[" and position < len(sql) and sql[position] == closing:
                        value.append(closing)
                        position += 1
                    else:
                        break
                else:
                    value.append(current)
            else:
                raise _TableSqlError("kapanmayan tırnaklı SQL birimi")
            tokens.append(_SqlToken(kind, "".join(value)))
        elif char.isalnum() or char in "_$" or ord(char) >= 128:
            end = position + 1
            while end < len(sql) and (sql[end].isalnum() or sql[end] in "_$" or ord(sql[end]) >= 128):
                end += 1
            tokens.append(_SqlToken("word", sql[position:end]))
            position = end
        elif char in "(),.;+-*/%<>=!|&~":
            tokens.append(_SqlToken("symbol", char))
            position += 1
        else:
            raise _TableSqlError("desteklenmeyen SQL karakteri: %r" % char)
    return tokens


class _TableSqlCursor:
    """Dar tablo dilbilgisi için ilerleyen, geri izleme yapmayan imleç."""

    def __init__(self, tokens: List[_SqlToken]):
        self.tokens = tokens
        self.position = 0

    def at(self, value: str, offset: int = 0) -> bool:
        if self.position + offset >= len(self.tokens):
            return False
        token = self.tokens[self.position + offset]
        return token.kind in ("word", "symbol") and _ascii_case(token.value, upper=True) == value

    def take(self, value: str) -> bool:
        if not self.at(value):
            return False
        self.position += 1
        return True

    def need(self, value: str) -> None:
        if not self.take(value):
            raise _TableSqlError("beklenen SQL birimi: %s" % value)

    def identifier(self) -> str:
        if self.position == len(self.tokens) or self.tokens[self.position].kind == "symbol":
            raise _TableSqlError("SQL adı bekleniyor")
        # SQLite, ad beklenen konumda tek tırnaklı eski ad yazımını da kabul eder.
        value = self.tokens[self.position].value
        self.position += 1
        if not value:
            raise _TableSqlError("boş SQL adı")
        return _ascii_case(value)

    def group(self) -> List[_SqlToken]:
        self.need("(")
        start, depth = self.position, 1
        while self.position < len(self.tokens):
            token = self.tokens[self.position]
            self.position += 1
            if token == _SqlToken("symbol", "("):
                depth += 1
            elif token == _SqlToken("symbol", ")"):
                depth -= 1
                if depth == 0:
                    if self.position == start + 1:
                        raise _TableSqlError("boş parantez grubu")
                    return self.tokens[start:self.position - 1]
        raise _TableSqlError("dengesiz SQL parantezleri")

    def finished(self) -> bool:
        return self.position == len(self.tokens)


def _split_table_items(tokens: List[_SqlToken]) -> List[List[_SqlToken]]:
    """Yalnızca en dış düzeydeki virgüllerden tablo/sütun maddelerini ayırır."""
    items: List[List[_SqlToken]] = []
    start, depth = 0, 0
    for position, token in enumerate(tokens):
        if token == _SqlToken("symbol", "("):
            depth += 1
        elif token == _SqlToken("symbol", ")"):
            depth -= 1
        elif token == _SqlToken("symbol", ",") and depth == 0:
            items.append(tokens[start:position])
            start = position + 1
        if depth < 0:
            raise _TableSqlError("dengesiz madde parantezleri")
    items.append(tokens[start:])
    if depth or any(not item for item in items):
        raise _TableSqlError("boş veya dengesiz tablo maddesi")
    return items


class _RequiredTableReader:
    """CREATE TABLE'ın yalnızca Vesper için gereken kısıt dilini okur.

    SELECT, sanal tablo, üretilmiş sütun veya ifade semantiği çözmez. Tekrarlı
    ve belirsiz kısıtlar reddedilir; ilgisiz ek nesnelerin SQL'i buraya gelmez.
    """

    def __init__(self):
        self.columns: Dict[str, str] = {}
        self.collations: List[Tuple[str, str]] = []
        self.conflicts: Dict[Tuple[str, Tuple[str, ...]], str] = {}
        self.foreign_keys: List[Dict[str, Any]] = []
        self.autoincrement: List[str] = []
        self.checks = 0

    @staticmethod
    def conflict(cursor: _TableSqlCursor) -> str:
        if not cursor.take("ON"):
            return "ABORT"
        cursor.need("CONFLICT")
        for policy in ("ABORT", "REPLACE", "IGNORE", "FAIL", "ROLLBACK"):
            if cursor.take(policy):
                return policy
        raise _TableSqlError("bilinmeyen ON CONFLICT politikası")

    def add_conflict(self, kind: str, columns: Tuple[str, ...], cursor: _TableSqlCursor) -> None:
        key = (kind, columns)
        if key in self.conflicts:
            raise _TableSqlError("tekrarlı/belirsiz %s kısıtı" % kind)
        self.conflicts[key] = self.conflict(cursor)

    def key_columns(self, cursor: _TableSqlCursor, primary: bool = False) -> Tuple[str, ...]:
        inner = _TableSqlCursor(cursor.group())
        columns = []
        while True:
            columns.append(inner.identifier())
            if inner.take("COLLATE"):
                if inner.identifier() != "binary":
                    raise _TableSqlError("anahtar COLLATE davranışı BINARY olmalı")
            if not inner.take("ASC"):
                inner.take("DESC")
            if primary and inner.take("AUTOINCREMENT"):
                if len(columns) != 1 or not inner.finished():
                    raise _TableSqlError("yanlış konumda AUTOINCREMENT")
                self.autoincrement.extend(columns)
            if inner.finished():
                return tuple(columns)
            inner.need(",")

    @staticmethod
    def identifier_list(cursor: _TableSqlCursor) -> Tuple[str, ...]:
        inner = _TableSqlCursor(cursor.group())
        columns = [inner.identifier()]
        while not inner.finished():
            inner.need(",")
            columns.append(inner.identifier())
        return tuple(columns)

    def references(self, cursor: _TableSqlCursor, source: Tuple[str, ...]) -> Dict[str, Any]:
        cursor.need("REFERENCES")
        target = cursor.identifier()
        columns = self.identifier_list(cursor) if cursor.at("(") else ("",) * len(source)
        if len(columns) != len(source):
            raise _TableSqlError("FK sütun eşleştirmesinin genişliği geçersiz")
        actions = {"DELETE": "NO ACTION", "UPDATE": "NO ACTION"}
        seen = set()
        while cursor.at("ON") or cursor.at("MATCH"):
            if cursor.take("MATCH"):
                # SQLite MATCH adını saklamaz/uygulamaz; hedef eşleme PRAGMA ile doğrulanır.
                cursor.identifier()
                continue
            cursor.need("ON")
            if cursor.take("DELETE"):
                event = "DELETE"
            elif cursor.take("UPDATE"):
                event = "UPDATE"
            else:
                raise _TableSqlError("desteklenmeyen FK ON olayı")
            if event in seen:
                raise _TableSqlError("tekrarlı FK eylemi")
            seen.add(event)
            if cursor.take("SET"):
                if cursor.take("NULL"):
                    action = "SET NULL"
                else:
                    cursor.need("DEFAULT")
                    action = "SET DEFAULT"
            elif cursor.take("NO"):
                cursor.need("ACTION")
                action = "NO ACTION"
            elif cursor.take("CASCADE"):
                action = "CASCADE"
            elif cursor.take("RESTRICT"):
                action = "RESTRICT"
            else:
                raise _TableSqlError("bilinmeyen FK eylemi")
            actions[event] = action
        foreign = {"mapping": (target, actions["UPDATE"], actions["DELETE"], "NONE", tuple(zip(source, columns))),
                   "deferred": False, "timing_seen": False}
        self.foreign_keys.append(foreign)
        return foreign

    @staticmethod
    def timing(cursor: _TableSqlCursor, foreign: Optional[Dict[str, Any]]) -> None:
        if foreign is None or foreign["timing_seen"]:
            raise _TableSqlError("FK ile eşleştirilemeyen/tekrarlı DEFERRABLE")
        not_deferrable = cursor.take("NOT")
        cursor.need("DEFERRABLE")
        deferred = False
        if cursor.take("INITIALLY"):
            if cursor.take("DEFERRED"):
                deferred = True
            else:
                cursor.need("IMMEDIATE")
        foreign["deferred"] = deferred and not not_deferrable
        foreign["timing_seen"] = True

    def constraint(self, cursor: _TableSqlCursor, column: Optional[str],
                   foreign: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if cursor.take("CONSTRAINT"):
            cursor.identifier()
        if cursor.take("PRIMARY"):
            cursor.need("KEY")
            if column is None:
                columns = self.key_columns(cursor, primary=True)
            else:
                columns = (column,)
                if not cursor.take("ASC"):
                    cursor.take("DESC")
            self.add_conflict("PRIMARY KEY", columns, cursor)
            if column is not None and cursor.take("AUTOINCREMENT"):
                self.autoincrement.append(column)
        elif cursor.take("UNIQUE"):
            columns = (column,) if column is not None else self.key_columns(cursor)
            self.add_conflict("UNIQUE", columns, cursor)
        elif cursor.take("CHECK"):
            cursor.group()
            self.checks += 1
            if column is None:
                self.conflict(cursor)
        elif column is None:
            cursor.need("FOREIGN")
            cursor.need("KEY")
            foreign = self.references(cursor, self.identifier_list(cursor))
            if cursor.at("NOT") or cursor.at("DEFERRABLE"):
                self.timing(cursor, foreign)
        elif cursor.at("REFERENCES"):
            foreign = self.references(cursor, (column,))
        elif cursor.take("COLLATE"):
            if any(name == column for name, _ in self.collations):
                raise _TableSqlError("tekrarlı/belirsiz sütun COLLATE bildirimi")
            self.collations.append((column, _ascii_case(cursor.identifier(), upper=True)))
        elif cursor.at("DEFERRABLE") or (cursor.at("NOT") and cursor.at("DEFERRABLE", 1)):
            self.timing(cursor, foreign)
        elif cursor.take("NOT"):
            cursor.need("NULL")
            self.add_conflict("NOT NULL", (column,), cursor)
        elif cursor.take("NULL"):
            if self.conflict(cursor) != "ABORT":
                raise _TableSqlError("desteklenmeyen NULL ON CONFLICT bildirimi")
        elif cursor.take("DEFAULT"):
            if cursor.at("("):
                cursor.group()
            else:
                if not cursor.take("+"):
                    cursor.take("-")
                if cursor.finished() or cursor.tokens[cursor.position].kind == "symbol":
                    raise _TableSqlError("DEFAULT değeri bekleniyor")
                cursor.position += 1
        else:
            raise _TableSqlError("desteklenmeyen veya yanlış konumda sütun kısıtı")
        return foreign

    def read(self, sql: str, table: str) -> _TableSemantics:
        cursor = _TableSqlCursor(_table_sql_tokens(sql))
        cursor.need("CREATE")
        cursor.need("TABLE")
        if cursor.take("IF"):
            cursor.need("NOT")
            cursor.need("EXISTS")
        name = cursor.identifier()
        if cursor.take("."):
            if name != "main":
                raise _TableSqlError("gerekli tablo main şemasında olmalı")
            name = cursor.identifier()
        if name != _ascii_case(table):
            raise _TableSqlError("CREATE TABLE adı beklenen tabloyla eşleşmiyor")
        items = _split_table_items(cursor.group())
        # Seçenekler PRAGMA parmak izinde ayrıca reddedilir; artan SQL yutulmaz.
        if not cursor.at(";") and not cursor.finished():
            while True:
                if cursor.take("WITHOUT"):
                    cursor.need("ROWID")
                elif not cursor.take("STRICT"):
                    raise _TableSqlError("desteklenmeyen CREATE TABLE son eki")
                if not cursor.take(","):
                    break
        cursor.take(";")
        if not cursor.finished():
            raise _TableSqlError("CREATE TABLE sonrasında tüketilmeyen SQL")
        table_constraints = False
        for item in items:
            part = _TableSqlCursor(item)
            is_table_constraint = any(part.at(word) for word in ("CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN"))
            if is_table_constraint:
                table_constraints = True
                while not part.finished():
                    self.constraint(part, None, None)
                continue
            if table_constraints:
                raise _TableSqlError("tablo kısıtından sonra sütun bildirimi")
            column, column_type = part.identifier(), _ascii_case(part.identifier(), upper=True)
            if column in self.columns or column_type not in ("TEXT", "INTEGER"):
                raise _TableSqlError("tekrarlı sütun veya desteklenmeyen sütun türü")
            self.columns[column] = column_type
            foreign = None
            while not part.finished():
                foreign = self.constraint(part, column, foreign)
        for column in self.autoincrement:
            if self.columns.get(column) != "INTEGER" or ("PRIMARY KEY", (column,)) not in self.conflicts:
                raise _TableSqlError("AUTOINCREMENT gerçek INTEGER PRIMARY KEY sütununa bağlı değil")
        if any(column not in self.columns for _, columns in self.conflicts for column in columns):
            raise _TableSqlError("kısıt bilinmeyen sütuna bağlı")
        collations = dict(self.collations)
        return _TableSemantics(
            self.checks,
            tuple(sorted((kind, columns, policy) for (kind, columns), policy in self.conflicts.items())),
            tuple(sorted((foreign["mapping"], foreign["deferred"]) for foreign in self.foreign_keys)),
            tuple((column, collations.get(column, "BINARY")) for column in self.columns),
            tuple(sorted(self.autoincrement)),
        )


class _TableShape(NamedTuple):
    """Bir tablonun SQLite'tan okunan yapısal parmak izi (adlar değil, yapı)."""

    columns: Tuple[Tuple[Any, ...], ...]
    foreign_keys: Tuple[Tuple[Any, ...], ...]
    named_indexes: Tuple[Tuple[str, Tuple[Any, ...]], ...]
    auto_indexes: Tuple[Tuple[Any, ...], ...]
    autoincrement: bool
    table_kind: str
    without_rowid: int
    strict: int
    triggers: Tuple[str, ...]
    semantics: Optional[_TableSemantics]
    semantic_error: str


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _user_objects(conn: sqlite3.Connection) -> Dict[str, str]:
    """Kullanıcı şema nesneleri (ad -> tür). SQLite'ın kendi `sqlite_*` nesneleri
    (sqlite_sequence, otomatik dizin adları vb.) KARARLI kimlik sayılmaz ve dışarıda bırakılır."""
    rows = conn.execute(
        "SELECT name, type FROM sqlite_master WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def _read_foreign_keys(conn: sqlite3.Connection, table: str) -> Tuple[Tuple[Any, ...], ...]:
    """Yabancı anahtarları KISITLAMA bazında gruplar: bileşik FK ile iki ayrı
    tekil FK, sütun eşleşmeleri (from -> to sırası) dahil birbirinden ayırt edilir."""
    groups: Dict[int, Dict[str, Any]] = {}
    for row in conn.execute("PRAGMA foreign_key_list(%s)" % _quote(table)):
        fk_id, seq, ref_table, from_col, to_col, on_update, on_delete, match = (
            row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7],
        )
        group = groups.setdefault(
            fk_id,
            {
                "table": ref_table,
                "on_update": str(on_update).upper(),
                "on_delete": str(on_delete).upper(),
                "match": str(match).upper(),
                "columns": [],
            },
        )
        group["columns"].append((seq, from_col or "", to_col or ""))
    result = []
    for group in groups.values():
        pairs = tuple((from_col, to_col) for _, from_col, to_col in sorted(group["columns"]))
        result.append((group["table"], group["on_update"], group["on_delete"], group["match"], pairs))
    return tuple(sorted(result))


def _metadata_error(pragma: str, detail: str) -> SchemaVersionError:
    """Eksik/bozuk içgözlem sonucunu şemanın uyduğu varsayımına dönüştürmez."""
    return SchemaVersionError(
        "Şema içgözlemi kullanılamıyor veya geçersiz (%s): %s" % (pragma, detail),
        reason=REASON_UNREADABLE,
    )


def _read_pragma_rows(
    conn: sqlite3.Connection, pragma: str, columns: Tuple[str, ...],
) -> List[Tuple[Any, ...]]:
    """PRAGMA yeteneğini başlık ve satır genişliğiyle doğrular; yeni ek sütunlara izin verir."""
    try:
        cursor = conn.execute(pragma)
        description = cursor.description
        if description is None or tuple(item[0] for item in description[:len(columns)]) != columns:
            raise _metadata_error(pragma, "beklenen sonuç sütunları yok")
        rows = [tuple(row) for row in cursor.fetchall()]
        if any(len(row) != len(description) for row in rows):
            raise _metadata_error(pragma, "sonuç satırının genişliği başlıkla eşleşmiyor")
        return rows
    except sqlite3.Error as exc:
        raise _metadata_error(pragma, str(exc)) from exc
    except (TypeError, ValueError, IndexError) as exc:
        raise _metadata_error(pragma, "sonuç okunamadı: %s" % exc) from exc


def _read_table_shape(conn: sqlite3.Connection, table: str) -> Optional[_TableShape]:
    row = conn.execute(
        "SELECT sql FROM main.sqlite_schema WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    if row is None:
        return None
    create_sql = row[0] or ""
    pragma = "PRAGMA main.table_list(%s)" % _quote(table)
    options = _read_pragma_rows(conn, pragma, ("schema", "name", "type", "ncol", "wr", "strict"))
    if len(options) != 1:
        raise _metadata_error(pragma, "gerekli tablo için tam bir satır bekleniyor")
    schema, name, kind, count, without_rowid, strict = options[0][:6]
    if (schema != "main" or name != table or kind not in ("table", "view", "virtual", "shadow")
            or type(count) is not int or count < 1
            or type(without_rowid) is not int or without_rowid not in (0, 1)
            or type(strict) is not int or strict not in (0, 1)):
        raise _metadata_error(pragma, "tablo türü/seçenekleri geçersiz")
    pragma = "PRAGMA main.table_xinfo(%s)" % _quote(table)
    column_rows = _read_pragma_rows(conn, pragma, ("cid", "name", "type", "notnull", "dflt_value", "pk", "hidden"))
    if len(column_rows) != count:
        raise _metadata_error(pragma, "sütun sayısı table_list ile eşleşmiyor")
    names = []
    for position, col in enumerate(column_rows):
        if (type(col[0]) is not int or col[0] != position
                or not isinstance(col[1], str) or not col[1] or col[1] in names
                or not isinstance(col[2], str)
                or type(col[3]) is not int or col[3] not in (0, 1)
                or (col[4] is not None and not isinstance(col[4], str))
                or type(col[5]) is not int or col[5] < 0
                or type(col[6]) is not int or col[6] not in (0, 1, 2, 3)):
            raise _metadata_error(pragma, "sütun tanımı veya gizli/üretilmiş sütun bayrağı geçersiz")
        names.append(col[1])
    columns = tuple(
        (col[1], col[2].upper(), col[3], col[4], col[5], col[6])
        for col in column_rows
    )
    named: Dict[str, Tuple[Any, ...]] = {}
    auto: List[Tuple[Any, ...]] = []
    for index in conn.execute("PRAGMA index_list(%s)" % _quote(table)).fetchall():
        name, unique, origin, partial = index[1], int(index[2]), index[3], int(index[4])
        key_columns = tuple(
            (info[2] or "", int(info[3]), _ascii_case(str(info[4] or ""), upper=True))
            for info in conn.execute("PRAGMA index_xinfo(%s)" % _quote(name))
            if int(info[5]) == 1
        )
        shape = (unique, partial, key_columns)
        if origin == "c":
            named[name] = shape  # CREATE INDEX ile kurulan: AD kararlıdır
        else:
            auto.append((origin,) + shape)  # UNIQUE/PRIMARY KEY otomatik dizini: AD kararlı DEĞİL
    foreign_keys = _read_foreign_keys(conn, table)
    semantics, semantic_error = None, ""
    try:
        semantics = _RequiredTableReader().read(create_sql, table)
        if tuple(sorted(mapping for mapping, _ in semantics.foreign_keys)) != foreign_keys:
            raise _TableSqlError("SQL FK bildirimi PRAGMA eşlemeleriyle eşleşmiyor")
    except _TableSqlError as exc:
        semantics, semantic_error = None, str(exc)
    return _TableShape(
        columns=columns,
        foreign_keys=foreign_keys,
        named_indexes=tuple(sorted(named.items())),
        auto_indexes=tuple(sorted(auto)),
        autoincrement=bool(semantics and semantics.autoincrement),
        table_kind=kind,
        without_rowid=without_rowid,
        strict=strict,
        triggers=tuple(row[0] for row in conn.execute(
            "SELECT name FROM main.sqlite_schema WHERE type = 'trigger' "
            "AND tbl_name = ? COLLATE NOCASE ORDER BY name", (table,),
        )),
        semantics=semantics,
        semantic_error=semantic_error,
    )


_REFERENCE_TABLES_V1: Tuple[str, ...] = ("sessions", "segments", "summaries", "schema_metadata")
_REFERENCE_TABLES_V2: Tuple[str, ...] = _REFERENCE_TABLES_V1 + ("artifacts",)
_reference_cache: Dict[bool, Dict[str, _TableShape]] = {}
_reference_lock = threading.Lock()


def _reference_shapes(include_v2: bool) -> Dict[str, _TableShape]:
    """Beklenen yapı: dondurulmuş DDL, geçici bir bellek-içi SQLite'ta kurulup
    okunur (dosya DEĞİL). Böylece karşılaştırma, gerçek SQLite çıktısıyla gerçek
    SQLite çıktısı arasındadır."""
    with _reference_lock:
        cached = _reference_cache.get(include_v2)
        if cached is not None:
            return cached
        memory = sqlite3.connect(":memory:")
        try:
            statements = _V1_BASELINE_STATEMENTS + (_V2_STATEMENTS if include_v2 else ())
            for statement in statements:
                memory.execute(statement)
            tables = _REFERENCE_TABLES_V2 if include_v2 else _REFERENCE_TABLES_V1
            shapes = {table: _read_table_shape(memory, table) for table in tables}
            if any(shape is None or shape.semantics is None for shape in shapes.values()):
                raise _metadata_error("referans CREATE TABLE", "dondurulmuş DDL güvenle çözümlenemedi")
        finally:
            memory.close()
        _reference_cache[include_v2] = shapes  # type: ignore[assignment]
        return shapes  # type: ignore[return-value]


def _structure_problems(conn: sqlite3.Connection, include_v2: bool, strict: bool) -> List[str]:
    """Gerçek yapıyı beklenenle karşılaştırır; SALT OKUNUR (veritabanını değiştirmez).

    strict=False (sürümlü veritabanı): gerekli nesneler beklenen tanımla eşleşmeli,
    ilgisiz EK nesnelere izin verilir. strict=True (sürümsüz/v1 çıkarımı): nesne
    kümesi TAM OLARAK beklenen olmalı."""
    problems: List[str] = []
    expected = _reference_shapes(include_v2)
    for table, exp in expected.items():
        act = _read_table_shape(conn, table)
        if act is None:
            problems.append("gerekli tablo eksik: %s" % table)
            continue
        if act.columns != exp.columns:
            problems.append("%s: sütun tanımları (ad/tür/NOT NULL/birincil anahtar/gizli/üretilmiş) beklenenle eşleşmiyor" % table)
        if (act.table_kind, act.without_rowid, act.strict) != (exp.table_kind, exp.without_rowid, exp.strict):
            problems.append("%s: tablo türü/STRICT/WITHOUT ROWID seçenekleri beklenenle eşleşmiyor" % table)
        if act.triggers != exp.triggers:
            problems.append("%s: gerekli tabloya bağlı beklenmeyen tetikleyici(ler): %s" % (table, ", ".join(act.triggers)))
        if act.foreign_keys != exp.foreign_keys:
            problems.append("%s: yabancı anahtar tanımları (hedef/sütun eşleşmesi/eylem) beklenenle eşleşmiyor" % table)
        if act.autoincrement != exp.autoincrement:
            problems.append("%s: AUTOINCREMENT özelliği beklenenle eşleşmiyor" % table)
        if act.semantics is None:
            problems.append("%s: desteklenmeyen/belirsiz CREATE TABLE: %s" % (table, act.semantic_error))
        elif exp.semantics is not None:
            if act.semantics.checks != exp.semantics.checks:
                problems.append("%s: beklenmeyen CHECK kısıtı" % table)
            if act.semantics.conflicts != exp.semantics.conflicts:
                problems.append("%s: PRIMARY KEY/UNIQUE/NOT NULL ON CONFLICT davranışı ABORT ile eşleşmiyor" % table)
            if act.semantics.foreign_keys != exp.semantics.foreign_keys:
                problems.append("%s: FK zamanlaması/eşlemesi beklenen immediate davranışla eşleşmiyor" % table)
            if act.semantics.collations != exp.semantics.collations:
                problems.append("%s: sütun COLLATE davranışı BINARY ile eşleşmiyor" % table)
            if act.semantics.autoincrement != exp.semantics.autoincrement:
                problems.append("%s: AUTOINCREMENT doğru id sütununa bağlı değil" % table)
        actual_named = dict(act.named_indexes)
        for name, shape in exp.named_indexes:
            if name not in actual_named:
                problems.append("gerekli dizin eksik: %s (tablo %s)" % (name, table))
            elif actual_named[name] != shape:
                problems.append("dizin tanımı beklenenle eşleşmiyor (benzersizlik/sütun sırası): %s" % name)
        if act.auto_indexes != exp.auto_indexes:
            problems.append("%s: UNIQUE/PRIMARY KEY otomatik dizinleri beklenenle eşleşmiyor" % table)
        if strict:
            if set(actual_named) != {name for name, _ in exp.named_indexes}:
                problems.append("%s: beklenmeyen ek dizin(ler) var" % table)
        else:
            unexpected_unique = sorted(name for name, shape in act.named_indexes
                                       if shape[0] and name not in dict(exp.named_indexes))
            if unexpected_unique:
                problems.append("%s: beklenmeyen UNIQUE dizin(ler): %s" % (table, ", ".join(unexpected_unique)))
    if strict:
        expected_names = set(expected)
        for exp in expected.values():
            expected_names.update(name for name, _ in exp.named_indexes)
        extras = sorted(set(_user_objects(conn)) - expected_names)
        if extras:
            problems.append("beklenmeyen ek şema nesneleri: %s" % ", ".join(extras))
    return problems


def _metadata_rows(conn: sqlite3.Connection) -> List[Tuple[Any, str]]:
    return [
        (row[0], row[1])
        for row in conn.execute(
            "SELECT schema_version, typeof(schema_version) FROM schema_metadata ORDER BY rowid"
        )
    ]


def _classify_schema(conn: sqlite3.Connection) -> _SchemaState:
    """Veritabanı durumunu sınıflandırır. YALNIZCA okuma yapar."""
    objects = _user_objects(conn)
    if not objects:
        return _SchemaState(_STATE_FRESH, "uygulama/kullanıcı şema nesnesi yok")
    if objects.get("schema_metadata") != "table":
        return _SchemaState(
            _STATE_INVALID, "schema_metadata tablosu yok; veritabanı bilinen bir Vesper şeması değil"
        )
    try:
        rows = _metadata_rows(conn)
    except sqlite3.Error as exc:
        return _SchemaState(_STATE_INVALID, "schema_metadata okunamadı: %s" % exc)
    if len(rows) >= 2:
        return _SchemaState(
            _STATE_INVALID,
            "schema_metadata içinde %d satır var (tam olarak 1 olmalı); yinelenen sürüm satırları" % len(rows),
        )
    if not rows:
        problems = _structure_problems(conn, include_v2=False, strict=True)
        if problems:
            return _SchemaState(
                _STATE_INVALID,
                "sürüm satırı yok ve şema katı v1 parmak iziyle eşleşmiyor: %s" % "; ".join(problems),
            )
        return _SchemaState(_STATE_UNVERSIONED_V1, "schema_metadata boş ama şema tam v1 parmak iziyle eşleşiyor")
    version, value_type = rows[0]
    if value_type != "integer" or version < 1:
        return _SchemaState(
            _STATE_INVALID,
            "geçersiz sürüm değeri %r (tür %s); pozitif bir tamsayı olmalı" % (version, value_type),
        )
    if version > SCHEMA_VERSION:
        return _SchemaState(
            _STATE_NEWER, "veritabanı şema sürümü v%d" % version, version=version
        )
    if version == 2:
        problems = _structure_problems(conn, include_v2=True, strict=False)
        if problems:
            return _SchemaState(
                _STATE_INVALID,
                "sürüm v2 diyor ama şema v2 yapısıyla eşleşmiyor: %s" % "; ".join(problems),
                version=2,
            )
        return _SchemaState(_STATE_V2, "geçerli v2", version=2)
    collisions = [name for name in _V2_OBJECT_NAMES if name in objects]
    if collisions:
        return _SchemaState(
            _STATE_INVALID,
            "v1 veritabanında v2'ye ait nesneler zaten var (çakışma): %s" % ", ".join(collisions),
            version=1,
        )
    problems = _structure_problems(conn, include_v2=False, strict=False)
    if problems:
        return _SchemaState(
            _STATE_INVALID,
            "sürüm v1 diyor ama şema v1 yapısıyla eşleşmiyor: %s" % "; ".join(problems),
            version=1,
        )
    return _SchemaState(_STATE_V1, "geçerli v1", version=1)


# --------------------------------------------------------------------------- #
# Salt-okunur bağlantı, tarihsel veri parmak izi, yedekleme yardımcıları
# --------------------------------------------------------------------------- #
def _close_temporary_connections(
    connections: Tuple[Optional[sqlite3.Connection], ...],
    primary: Optional[BaseException] = None,
) -> None:
    """Geçici bağlantıların hepsini kapatmayı dener; asıl hatayı ve ikincil tanıları korur."""
    failure = primary
    for conn in connections:
        if conn is None:
            continue
        try:
            conn.close()
        except BaseException as close_error:
            if failure is None:
                failure = close_error
            else:
                failure.close_errors = getattr(failure, "close_errors", ()) + (close_error,)
                if getattr(failure, "close_error", None) is None:
                    failure.close_error = close_error
    if primary is None and failure is not None:
        raise failure


def _open_readonly_connection(path: Union[str, Path], timeout: float) -> sqlite3.Connection:
    """Yalnızca mode=ro ile açar; başarısızlıkta yazılabilir bağlantıya DÖNMEZ.

    SQLite WAL yan dosyaları üzerinde sıfır dosya sistemi etkisi garanti edilmez.
    """
    resolved = Path(path).resolve()
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True, timeout=timeout)
        conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        return conn
    except BaseException as exc:
        _close_temporary_connections((conn,), exc)
        raise


def _history_counts(conn: sqlite3.Connection) -> Tuple[int, ...]:
    return tuple(
        int(conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0])
        for table, _ in _HISTORY_TABLES
    )


def _history_digests(conn: sqlite3.Connection) -> Dict[str, str]:
    """Her tarihsel tablonun TÜM satır içeriğinin SHA-256 özeti (birincil anahtar sırasıyla)."""
    digests: Dict[str, str] = {}
    for table, key in _HISTORY_TABLES:
        hasher = hashlib.sha256()
        for row in conn.execute("SELECT * FROM %s ORDER BY %s" % (table, key)):
            hasher.update(repr(tuple(row)).encode("utf-8"))
            hasher.update(b"\n")
        digests[table] = hasher.hexdigest()
    return digests


class _BackupDeadlineExceeded(Exception):
    """Yedek ilerleme geri-çağrısındaki EN İYİ ÇABA süre sınırı aşıldı."""


def _online_backup_copy(src_path: Path, dst_path: Path) -> None:
    """SQLite çevrimiçi yedek API'si ile AYRI bir kaynak bağlantısından yedek alır.

    Kaynak bağlantısı, BEGIN IMMEDIATE'i tutan göç bağlantısı DEĞİLDİR (aynı
    bağlantıdan backup() çağrısı asla sonuçlanmaz). WAL'deki işlenmiş (commit
    edilmiş) satırlar yedeğe dahildir. Hedef, tek dosyalık (journal_mode=DELETE)
    bağımsız okunabilir bir veritabanına çevrilir.

    NOT: İlerleme geri-çağrısındaki süre sınırı EN İYİ ÇABA'dır; yerel çağrı
    geri-çağrıya hiç uğramadan bloke olursa sert zaman aşımı GARANTİ ETMEZ.
    """
    started = time.monotonic()

    def progress(status: int, remaining: int, total: int) -> None:
        if time.monotonic() - started > BACKUP_PROGRESS_DEADLINE_SEC:
            raise _BackupDeadlineExceeded(
                "yedek ilerleme süre sınırı (%.0f sn, en iyi çaba) aşıldı (son durum kodu %s)"
                % (BACKUP_PROGRESS_DEADLINE_SEC, status)
            )

    source: Optional[sqlite3.Connection] = None
    destination: Optional[sqlite3.Connection] = None
    primary: Optional[BaseException] = None
    try:
        source = sqlite3.connect(str(src_path), timeout=BACKUP_SOURCE_BUSY_TIMEOUT_SEC)
        source.execute("PRAGMA query_only=ON")
        destination = sqlite3.connect(str(dst_path))
        source.backup(destination, pages=-1, progress=progress, sleep=0.05)
        destination.execute("PRAGMA journal_mode=DELETE")
    except BaseException as exc:
        primary = exc
        raise
    finally:
        _close_temporary_connections((destination, source), primary)


def _verify_backup_file(
    path: Path,
    expected_counts: Tuple[int, ...],
    expected_digests: Dict[str, str],
    expected_metadata: List[Tuple[Any, str]],
    readonly: bool,
) -> List[str]:
    """Yedeği bağımsız bir bağlantıyla doğrular; sorunların listesini döndürür (boş = geçerli)."""
    problems: List[str] = []
    conn: Optional[sqlite3.Connection] = None
    primary: Optional[BaseException] = None
    try:
        if readonly:
            conn = _open_readonly_connection(path, INSPECTION_BUSY_TIMEOUT_SEC)
        else:
            conn = sqlite3.connect(str(path), timeout=INSPECTION_BUSY_TIMEOUT_SEC)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            problems.append("integrity_check başarısız: %s" % integrity)
        if _metadata_rows(conn) != expected_metadata:
            problems.append("schema_metadata kaynakla eşleşmiyor")
        if _history_counts(conn) != expected_counts:
            problems.append("tarihsel satır sayıları kaynakla eşleşmiyor")
        if _history_digests(conn) != expected_digests:
            problems.append("tarihsel satır içerikleri kaynakla eşleşmiyor")
    except sqlite3.Error as exc:
        primary = exc
        problems.append("yedek okunamadı: %s: %s" % (type(exc).__name__, exc))
    except BaseException as exc:
        primary = exc
        raise
    finally:
        if primary is None and problems:
            primary = RuntimeError("; ".join(problems))
        _close_temporary_connections((conn,), primary)
    if primary is not None:
        for close_error in getattr(primary, "close_errors", ()):
            problems.append("yedek bağlantısı kapatılamadı: %s: %s" % (type(close_error).__name__, close_error))
    for suffix in ("-wal", "-shm", "-journal"):
        if Path(str(path) + suffix).exists():
            problems.append("yedeğin yanında beklenmeyen yan dosya var: %s" % (path.name + suffix))
    return problems


def _rename_without_overwrite(src: Path, dst: Path) -> None:
    """Geçici yedeği son adına taşır; var olan bir dosyanın ÜZERİNE ASLA yazmaz."""
    if dst.exists():
        raise FileExistsError("yedek dosyası zaten var: %s" % dst)
    last_error: Optional[BaseException] = None
    for attempt in range(BACKUP_RENAME_ATTEMPTS):
        try:
            os.rename(str(src), str(dst))  # Windows'ta hedef varsa reddeder
            return
        except PermissionError as exc:  # dosyayı geçici tutan tarayıcı yazılımı vb.
            last_error = exc
            time.sleep(BACKUP_RENAME_DELAY_SEC)
    assert last_error is not None
    raise last_error


def _report_error(primary: BaseException, reporter, message: str, *args, **kwargs) -> None:
    """İkincil günlük arızasını asıl hataya ekler; hata kimliğini ve izini değiştirmez."""
    try:
        reporter(message, *args, **kwargs)
    except BaseException as logging_error:
        primary.logging_errors = getattr(primary, "logging_errors", ()) + (logging_error,)


def _cleanup_partial(partial: Path) -> Tuple[BaseException, ...]:
    """YALNIZCA bu (başarısız) denemenin oluşturduğu geçici dosyaları siler."""
    errors: List[BaseException] = []
    for suffix in ("", "-journal", "-wal", "-shm"):
        candidate = Path(str(partial) + suffix)
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            errors.append(exc)
            _report_error(exc, log.warning, "Geçici yedek dosyası silinemedi: %s", candidate, exc_info=True)
    return tuple(errors)


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
class Database:
    """Tek bir .db dosyasına thread-local bağlantı sağlar ve şemayı yönetir.

    Üretimde bir tane paylaşılır; her thread ilk erişiminde kendi SQLite
    bağlantısını açar. close() yalnızca çağıran thread'in bağlantısını
    kapatır; böylece sqlite3'ün thread affinity kuralı korunur.

    allow_migration (varsayılan False): mevcut bir v1 veritabanını v2'ye göç
    ettirme İZNİ. Yalnızca bu Database örneği için ve yalnızca AÇIKÇA True
    verilirse geçerlidir; yoldan/ortamdan/global ayardan türetilmez. BOŞ veritabanı
    kurulumu ve geçerli v2'yi açmak izin gerektirmez.

    Kurucu hiçbir G/Ç yapmaz (dosya açmaz, dizin oluşturmaz).
    Başarısız şema hazırlığı önceden verilen bağlantıyı da kapatmayı dener;
    bu, o bağlantıdaki henüz commit edilmemiş uygulama yazılarını kaybettirebilir.
    Kapatma başarısızsa dışarıda tutulan ham referansların iptali garanti edilemez.
    """

    def __init__(self, db_path: Union[str, Path], allow_migration: bool = False):
        if not isinstance(allow_migration, bool):
            raise TypeError(
                "allow_migration yalnızca bool olabilir (True/False); alınan: %r" % (allow_migration,)
            )
        self.db_path = Path(db_path)
        self._allow_migration = allow_migration
        self._local = threading.local()

    @property
    def allow_migration(self) -> bool:
        return self._allow_migration

    def get_connection(self) -> sqlite3.Connection:
        """Şema bu thread için başarıyla hazırlandıktan sonra yazılabilir bağlantıyı verir."""
        conn = self._get_live_cached_connection()
        if conn is None or not getattr(self._local, "schema_ready", False):
            self.init_schema()
        return self._local.conn

    def _invalidate_connection(
        self, failure: Optional[BaseException] = None,
        connection: Optional[sqlite3.Connection] = None,
    ) -> None:
        """Önce thread-yerel sahipliği kaldırır, sonra kapatır; asıl hatayı maskelemez."""
        conn = connection if connection is not None else getattr(self._local, "conn", None)
        self._local.conn = None
        self._local.schema_ready = False
        if conn is not None:
            try:
                conn.close()
            except BaseException as close_error:
                if failure is None:
                    raise
                failure.close_error = close_error
                _report_error(failure, log.error, "Bağlantı önbellekten çıkarıldı fakat kapatılamadı", exc_info=True)

    def _get_live_cached_connection(self) -> Optional[sqlite3.Connection]:
        """SQL çalıştırmadan kapalı ham bağlantıyı fark eder; canlı işlemi değiştirmez."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.in_transaction
            except sqlite3.ProgrammingError as exc:
                self._invalidate_connection(exc)
                if "closed" not in str(exc).lower():
                    raise
                return None
            except BaseException as exc:
                self._invalidate_connection(exc)
                raise
        return conn

    def _setup_error(
        self, error: BaseException, phase: str, outcome: Optional[str] = None,
    ) -> SchemaMigrationError:
        """Kurulum hatasını dosyanın değişmediği iddiası olmadan sınıflandırır."""
        message = "Bağlantı kurulumu/temizliği başarısız (%s): %s (%s: %s)." % (
            phase, self.db_path, type(error).__name__, error,
        )
        if outcome == "committed":
            message = ("Şema işlemi COMMIT ile başarıyla kalıcılaştırıldı; bağlantı temizliği "
                       "başarısız. Geri alma veya otomatik yeniden deneme yapılmadı. " + message)
        else:
            if outcome == "rolled_back":
                message += " Şema işleminin geri alınması doğrulandı."
            message += " Dosya sistemi etkisi olmadığı varsayılamaz."
        failure = SchemaMigrationError(message, reason=REASON_CONNECTION_SETUP_FAILED, db_path=self.db_path)
        failure.phase = phase
        failure.transaction_outcome = outcome
        failure.primary_error = error
        return failure

    def _set_busy_timeout(
        self, conn: sqlite3.Connection, timeout_ms: int, phase: str,
        outcome: Optional[str] = None,
    ) -> None:
        """Busy timeout'u ayarlar ve SQLite'ın gerçekten uyguladığını doğrular."""
        try:
            conn.execute("PRAGMA busy_timeout=%d" % timeout_ms)
            row = conn.execute("PRAGMA busy_timeout").fetchone()
            if row is None or row[0] != timeout_ms:
                raise RuntimeError("SQLite busy_timeout değerini uygulamadı")
        except Exception as exc:
            raise self._setup_error(exc, phase, outcome) from exc
        except BaseException as exc:
            exc.phase = phase
            exc.transaction_outcome = outcome
            raise

    def _get_raw_connection(self) -> sqlite3.Connection:
        """Yalnızca şema hazırlama iç adımları için thread-local bağlantı açar."""
        conn = self._get_live_cached_connection()
        if conn is None:
            phase = "directory"
            try:
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
                phase = "connect"
                conn = sqlite3.connect(str(self.db_path), timeout=_NORMAL_TIMEOUT_SEC)
                phase = "row_factory"
                conn.row_factory = sqlite3.Row
                phase = "journal_mode"
                row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
                if row is None or str(row[0]).lower() != "wal":
                    raise RuntimeError("SQLite WAL modunu etkinleştirmedi")
                phase = "foreign_keys"
                conn.execute("PRAGMA foreign_keys=ON")
                row = conn.execute("PRAGMA foreign_keys").fetchone()
                if row is None or row[0] != 1:
                    raise RuntimeError("SQLite yabancı anahtar denetimini etkinleştirmedi")
                phase = "normal_timeout"
                self._set_busy_timeout(conn, _NORMAL_BUSY_TIMEOUT_MS, phase)
            except Exception as exc:
                failure = exc if isinstance(exc, SchemaMigrationError) else self._setup_error(exc, phase)
                self._invalidate_connection(failure, connection=conn)
                if failure is exc:
                    raise
                raise failure from exc
            except BaseException as exc:
                self._invalidate_connection(exc, connection=conn)
                raise
            self._local.conn = conn
        return conn

    # ------------------------------------------------------------------ #
    # Şema hazırlama
    # ------------------------------------------------------------------ #
    def init_schema(self) -> None:
        """Şemayı hazırlar: BOŞ -> v2, geçerli v2 -> hiçbir şey, yetkili v1 -> göç.

        İzin verilmemiş v1, yeni (>v2), tutarsız veya hasarlı veritabanları
        şema DDL'i çalıştırılmadan SchemaMigrationError/SchemaVersionError ile reddedilir.
        Hata halinde bağlantı iptal edilir; açık uygulama yazıları kaybolabilir.
        Reddedilen durum sessizce 'taze kurulum'a çevrilmez.
        """
        self._local.schema_ready = False
        try:
            state = self._inspect_readonly()
            if state.kind != _STATE_V2:
                self._raise_if_unsupported(state)
                if state.kind in (_STATE_V1, _STATE_UNVERSIONED_V1) and not self._allow_migration:
                    raise self._authorization_error(state)
                self._initialize_or_migrate()
            self._get_raw_connection()
            self._local.schema_ready = True
        except BaseException as exc:
            self._invalidate_connection(exc)
            if isinstance(exc, SchemaMigrationError):
                _report_error(exc, log.error, "Veritabanı şeması hazırlanamadı [%s]: %s", exc.reason, exc)
            raise
        self._local.initialization_logging_error = None
        try:
            log.info("Veritabanı hazır: %s (schema v%d)", self.db_path, SCHEMA_VERSION)
        except Exception as logging_error:
            # Hazır bağlantıyı değiştirmeden günlük arızasını tanı için sakla.
            self._local.initialization_logging_error = logging_error

    def close(self) -> None:
        """Yalnızca çağıran thread'e ait SQLite bağlantısını kapatır."""
        self._invalidate_connection()

    # ------------------------------------------------------------------ #
    # Ön inceleme (kilitsiz, salt-okunur) ve reddetme kararları
    # ------------------------------------------------------------------ #
    def _inspect_readonly(self) -> _SchemaState:
        """Şemayı mode=ro ile sınıflandırır; WAL yan dosyaları üzerinde sıfır etki garanti etmez."""
        try:
            if not self.db_path.exists() or self.db_path.stat().st_size == 0:
                return _SchemaState(_STATE_FRESH, "dosya yok veya boş")
        except OSError as exc:
            raise SchemaVersionError(
                "Veritabanı dosyası incelenemedi: %s (%s)" % (self.db_path, exc),
                reason=REASON_UNREADABLE,
                db_path=self.db_path,
            ) from exc
        conn: Optional[sqlite3.Connection] = None
        primary: Optional[BaseException] = None
        try:
            conn = _open_readonly_connection(self.db_path, INSPECTION_BUSY_TIMEOUT_SEC)
            return _classify_schema(conn)
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "locked" in message or "busy" in message:
                primary = SchemaMigrationError(
                    "Veritabanı şu anda kilitli olduğu için şema durumu okunamadı: %s. "
                    "Göç DDL'i çalıştırılmadı."
                    % self.db_path + _PREPARATION_EFFECTS_NOTE,
                    reason=REASON_LOCK_TIMEOUT,
                    db_path=self.db_path,
                )
                raise primary from exc
            primary = SchemaVersionError(
                "Veritabanı okunamadı: %s (%s). Göç DDL'i çalıştırılmadı." % (self.db_path, exc)
                + _PREPARATION_EFFECTS_NOTE,
                reason=REASON_UNREADABLE,
                db_path=self.db_path,
            )
            raise primary from exc
        except sqlite3.DatabaseError as exc:
            primary = SchemaVersionError(
                "Dosya geçerli bir SQLite veritabanı değil veya bozuk: %s (%s). "
                "Göç DDL'i çalıştırılmadı." % (self.db_path, exc) + _PREPARATION_EFFECTS_NOTE,
                reason=REASON_INCONSISTENT_SCHEMA,
                db_path=self.db_path,
            )
            raise primary from exc
        except BaseException as exc:
            primary = exc
            raise
        finally:
            _close_temporary_connections((conn,), primary)

    def _raise_if_unsupported(self, state: _SchemaState) -> None:
        if state.kind == _STATE_NEWER:
            raise SchemaVersionError(
                "Veritabanı şema sürümü v%s, ama bu Vesper sürümü en fazla v%d destekler: %s. "
                "Göç DDL'i çalıştırılmadı. Daha yeni bir Vesper sürümü kullan."
                % (state.version, SCHEMA_VERSION, self.db_path) + _PREPARATION_EFFECTS_NOTE,
                reason=REASON_NEWER_SCHEMA,
                db_path=self.db_path,
            )
        if state.kind == _STATE_INVALID:
            raise SchemaVersionError(
                "Veritabanı şeması tutarsız veya desteklenmiyor: %s. Neden: %s. "
                "Otomatik onarım YAPILMAZ; göç DDL'i çalıştırılmadı. Veritabanını elle incele veya "
                "yedekten geri yükle." % (self.db_path, state.detail) + _PREPARATION_EFFECTS_NOTE,
                reason=REASON_INCONSISTENT_SCHEMA,
                db_path=self.db_path,
            )

    def _authorization_error(self, state: _SchemaState) -> SchemaMigrationError:
        label = "şema v1" if state.kind == _STATE_V1 else "sürüm satırı olmayan (eski) v1 şeması"
        return SchemaMigrationError(
            "Veritabanı %s (%s); bu Vesper sürümü şema v%d bekliyor ve otomatik göç izni "
            "verilmedi (allow_migration=False). Yedek ALINMADI, göç DDL'i çalıştırılmadı. "
            "Göç, ayrı ve açık bir onay adımıdır: yetkili göç için "
            "Database(db_path, allow_migration=True) kullanılmalıdır."
            % (label, self.db_path, SCHEMA_VERSION) + _PREPARATION_EFFECTS_NOTE,
            reason=REASON_AUTHORIZATION_REQUIRED,
            db_path=self.db_path,
        )

    # ------------------------------------------------------------------ #
    # Kurulum / göç (yazma kilidi altında)
    # ------------------------------------------------------------------ #
    def _initialize_or_migrate(self) -> None:
        """BEGIN IMMEDIATE altında yeniden sınıflandırıp BOŞ -> v2 kurar veya yetkili v1 -> v2 göç eder."""
        conn = self._get_raw_connection()
        if conn.in_transaction:
            raise SchemaMigrationError(
                "Şema hazırlanamadı: bu thread'in bağlantısında açık bir işlem var; "
                "bağlantı iptal edilip kapatma denenecek; commit edilmemiş uygulama yazıları kaybolabilir.",
                db_path=self.db_path,
            )
        transaction_started = False
        transaction_outcome: Optional[str] = None
        try:
            self._set_busy_timeout(conn, int(MIGRATION_BUSY_TIMEOUT_SEC * 1000), "migration_timeout")
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                code = getattr(exc, "sqlite_errorcode", None)
                locked = (code & 0xff) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) if code is not None else (
                    "locked" in str(exc).lower() or "busy" in str(exc).lower()
                )
                raise SchemaMigrationError(
                    "Veritabanı yazma işlemi başlatılamadı (bekleme sınırı %.0f sn): %s (%s). "
                    "Bağlantı kurulumu dosya sistemini etkilemiş olabilir."
                    % (MIGRATION_BUSY_TIMEOUT_SEC, self.db_path, exc),
                    reason=REASON_LOCK_TIMEOUT if locked else REASON_MIGRATION_FAILED,
                    db_path=self.db_path,
                ) from exc
            transaction_started = True
            # Kilit ALTINDA yeniden sınıflandır: bekleme sırasında başka bir süreç
            # kurulumu/göçü bitirmiş olabilir.
            state = self._classify_under_lock(conn)
            if state.kind == _STATE_V2:
                transaction_outcome = self._rollback_transaction(conn)
                transaction_started = False
                log.info("Şema başka bir işlem tarafından zaten v2'ye getirilmiş; yedek/göç yapılmadı.")
            else:
                self._raise_if_unsupported(state)
                if state.kind in (_STATE_V1, _STATE_UNVERSIONED_V1) and not self._allow_migration:
                    raise self._authorization_error(state)  # bekleme sırasında göçe BAŞLAMAZ
                if state.kind == _STATE_FRESH:
                    self._initialize_fresh(conn)
                else:
                    self._migrate_v1(conn, state)
                try:
                    conn.commit()
                    if conn.in_transaction:
                        raise RuntimeError("COMMIT döndü fakat işlem hâlâ açık")
                except Exception as exc:
                    failure = SchemaMigrationError(
                        "Şema işlemi COMMIT aşamasında hata verdi; kalıcılık sonucu henüz bilinmiyor: %s (%s)"
                        % (self.db_path, exc), reason=REASON_COMMIT_FAILED, db_path=self.db_path,
                    )
                    failure.phase = "commit"
                    failure.primary_error = exc
                    raise failure from exc
                except BaseException as exc:
                    exc.phase = "commit"
                    raise
                transaction_outcome = "committed"
        except BaseException as exc:
            # Geri alma kendi hatasında bağlantıyı iptal etmişse ikinci kez denenmez.
            if transaction_started and getattr(self._local, "conn", None) is conn:
                exc.transaction_outcome = self._rollback_transaction(conn, exc)
            raise
        self._set_busy_timeout(
            conn, _NORMAL_BUSY_TIMEOUT_MS, "restore_timeout", transaction_outcome,
        )

    def _rollback_transaction(
        self, conn: sqlite3.Connection, primary: Optional[BaseException] = None,
    ) -> str:
        """Geri almayı tek yerde yapar; belirsiz sonucu başarı diye bildirmez."""
        try:
            if not conn.in_transaction:
                if primary is not None:
                    return "unknown"  # COMMIT mi otomatik geri alma mı olduğu buradan anlaşılamaz.
                raise RuntimeError("Beklenen açık işlem bulunamadı")
            conn.rollback()
            if conn.in_transaction:
                raise RuntimeError("ROLLBACK döndü fakat işlem hâlâ açık")
            return "rolled_back"
        except BaseException as rollback_error:
            # Kesme/çıkış sinyali asıl hataysa türünü ve nesnesini koru.
            if primary is not None and not isinstance(primary, Exception):
                primary.rollback_error = rollback_error
                primary.transaction_outcome = "unknown"
                self._invalidate_connection(primary)
                _report_error(primary, log.error, "Kesme sonrasında geri alma doğrulanamadı", exc_info=True)
                return "unknown"
            if not isinstance(rollback_error, Exception):
                rollback_error.phase = "rollback"
                rollback_error.transaction_outcome = "unknown"
                rollback_error.primary_error = primary
                rollback_error.original_reason = getattr(primary, "reason", None)
                self._invalidate_connection(rollback_error)
                if primary is not None:
                    raise rollback_error from primary
                raise
            failure = SchemaMigrationError(
                "Şema işleminin geri alınması doğrulanamadı; bağlantı iptal edildi. "
                "İşlem sonucu bilinmiyor: %s (%s)" % (self.db_path, rollback_error),
                reason=REASON_ROLLBACK_FAILED, db_path=self.db_path,
            )
            failure.phase = "rollback"
            failure.transaction_outcome = "unknown"
            failure.original_reason = getattr(primary, "reason", None)
            failure.primary_error = primary
            failure.rollback_error = rollback_error
            self._invalidate_connection(failure)
            raise failure from (primary if primary is not None else rollback_error)

    def _classify_under_lock(self, conn: sqlite3.Connection) -> _SchemaState:
        """Yazma kilidi altındaki yeniden sınıflandırma; ham sqlite3 hatalarını açık şema hatasına çevirir."""
        try:
            return _classify_schema(conn)
        except sqlite3.Error as exc:
            raise SchemaVersionError(
                "Kilit altında şema durumu okunamadı: %s (%s: %s). Göç DDL'i çalıştırılmadı."
                % (self.db_path, type(exc).__name__, exc) + _PREPARATION_EFFECTS_NOTE,
                reason=REASON_UNREADABLE,
                db_path=self.db_path,
            ) from exc

    def _initialize_fresh(self, conn: sqlite3.Connection) -> None:
        try:
            for statement in _V1_BASELINE_STATEMENTS + _V2_STATEMENTS:
                self._execute_ddl_statement(conn, statement)
            self._write_schema_version(conn, insert=True)
            self._verify_v2_in_transaction(conn)
        except SchemaMigrationError:
            raise
        except Exception as exc:
            raise SchemaMigrationError(
                "Yeni veritabanı şeması DDL aşamasında kurulamadı: %s (%s: %s)"
                % (self.db_path, type(exc).__name__, exc),
                reason=REASON_DDL_FAILED,
                db_path=self.db_path,
            ) from exc
        log.info("Yeni schema v%d hazır (commit bekliyor): %s", SCHEMA_VERSION, self.db_path)

    def _migrate_v1(self, conn: sqlite3.Connection, state: _SchemaState) -> None:
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            tables = sorted({str(row[0]) for row in violations})
            raise SchemaMigrationError(
                "Göç reddedildi: veritabanında zaten %d yabancı anahtar ihlali var (tablolar: %s). "
                "Tutarsız ilişkisel veri üzerinde şema göçü yapılmaz ve veri OTOMATİK onarılmaz. "
                "Göç DDL'i çalıştırılmadı." % (len(violations), ", ".join(tables))
                + _PREPARATION_EFFECTS_NOTE,
                reason=REASON_FK_VIOLATIONS,
                db_path=self.db_path,
            )
        backup_path: Optional[Path] = None
        if any(_history_counts(conn)):
            backup_path = self._create_backup(conn)
        else:
            log.info("Tarihsel satır yok (boş v1); yedek alınmadı.")
        try:
            for statement in _V2_STATEMENTS:
                self._execute_ddl_statement(conn, statement)
            self._write_schema_version(conn, insert=(state.kind == _STATE_UNVERSIONED_V1))
            self._verify_v2_in_transaction(conn)
        except SchemaMigrationError:
            raise
        except Exception as exc:
            backup_note = (" Yedek korundu: %s." % backup_path) if backup_path else ""
            raise SchemaMigrationError(
                "Şema göçü (v1 -> v2) DDL aşamasında başarısız oldu: %s (%s: %s).%s"
                % (self.db_path, type(exc).__name__, exc, backup_note),
                reason=REASON_DDL_FAILED,
                db_path=self.db_path,
            ) from exc
        log.info(
            "Şema göçü hazır (commit bekliyor): v1 -> v2%s",
            (", yedek: %s" % backup_path) if backup_path else ", yedek gerekmedi",
        )

    def _execute_ddl_statement(self, conn: sqlite3.Connection, statement: str) -> None:
        """Tek bir DDL ifadesini çalıştırır (executescript KULLANILMAZ; açık işlem içinde)."""
        conn.execute(statement)

    def _write_schema_version(self, conn: sqlite3.Connection, insert: bool) -> None:
        if insert:
            conn.execute("INSERT INTO schema_metadata (schema_version) VALUES (?)", (SCHEMA_VERSION,))
            return
        cursor = conn.execute("UPDATE schema_metadata SET schema_version = ?", (SCHEMA_VERSION,))
        if cursor.rowcount != 1:
            raise SchemaMigrationError(
                "schema_metadata güncellemesi beklenmedik sayıda satırı etkiledi (%d)" % cursor.rowcount,
                reason=REASON_DDL_FAILED,
                db_path=self.db_path,
            )

    def _verify_v2_in_transaction(self, conn: sqlite3.Connection) -> None:
        problems = _structure_problems(conn, include_v2=True, strict=False)
        if _metadata_rows(conn) != [(SCHEMA_VERSION, "integer")]:
            problems.append("schema_metadata tam olarak bir tamsayı v%d satırı içermiyor" % SCHEMA_VERSION)
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            problems.append("foreign_key_check ihlal bildirdi")
        if problems:
            raise SchemaMigrationError(
                "Şema v2 doğrulaması başarısız (geri alma denenecek): %s" % "; ".join(problems),
                reason=REASON_DDL_FAILED,
                db_path=self.db_path,
            )

    # ------------------------------------------------------------------ #
    # Yedek (yazma kilidi ALTINDA, AYRI bağlantılarla)
    # ------------------------------------------------------------------ #
    def _create_backup(self, conn: sqlite3.Connection) -> Path:
        """v1 verisinin bağımsız okunabilir, doğrulanmış bir yedeğini alır.

        `conn` BEGIN IMMEDIATE'i tutan bağlantıdır; yedek ondan ALINMAZ, ayrı bir
        kaynak bağlantısıyla alınır. Yazma kilidi tutulduğu için kaynak, göç edilecek
        durumla birebir aynıdır. Başarısızlıkta YALNIZCA bu denemenin geçici dosyaları
        silinir; tamamlanmış yedekler asla silinmez.
        """
        backup_dir = self.db_path.parent / BACKUP_DIR_NAME
        partial: Optional[Path] = None
        staging_dir: Optional[Path] = None
        primary: Optional[BaseException] = None
        self._local.backup_cleanup_errors = ()
        try:
            expected_counts = _history_counts(conn)
            expected_digests = _history_digests(conn)
            expected_metadata = _metadata_rows(conn)
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            final = backup_dir / ("%s.pre-v2.%s.%s.db" % (self.db_path.stem, stamp, secrets.token_hex(4)))
            if final.exists():
                raise FileExistsError("yedek dosya adı çakışması: %s" % final)
            # mkdtemp mevcut dizini ASLA sahiplenmez; her denemenin ayrı alanı vardır.
            staging_dir = Path(tempfile.mkdtemp(prefix=".vesper-backup-", dir=str(backup_dir)))
            candidate = staging_dir / (final.name + ".partial")
            if any(Path(str(candidate) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
                raise FileExistsError("geçici yedek yan dosyası zaten var: %s" % candidate)
            with open(str(candidate), "xb"):
                pass
            partial = candidate  # yalnızca başarılı özel oluşturmadan sonra sahiplenilir
            _online_backup_copy(self.db_path, partial)
            problems = _verify_backup_file(partial, expected_counts, expected_digests, expected_metadata, False)
            if problems:
                raise RuntimeError("yedek doğrulaması başarısız: %s" % "; ".join(problems))
            with open(str(partial), "rb+") as handle:
                os.fsync(handle.fileno())
            _rename_without_overwrite(partial, final)
            partial = None  # artık geçici dosya yok; tamamlanmış yedek ASLA silinmez
            problems = _verify_backup_file(final, expected_counts, expected_digests, expected_metadata, True)
            if problems:
                raise RuntimeError(
                    "tamamlanan yedek yeniden doğrulanamadı (dosya incelenmek üzere bırakıldı: %s): %s"
                    % (final, "; ".join(problems))
                )
        except BaseException as exc:
            primary = exc
            if isinstance(exc, Exception):
                primary = SchemaMigrationError(
                    "Yedek alınamadı veya doğrulanamadı; göç DDL'i çalıştırılmadı: %s (%s: %s)"
                    % (self.db_path, type(exc).__name__, exc) + _PREPARATION_EFFECTS_NOTE,
                    reason=REASON_BACKUP_FAILED,
                    db_path=self.db_path,
                )
                raise primary from exc
            raise
        finally:
            cleanup_errors: List[BaseException] = []
            if partial is not None:
                cleanup_errors.extend(_cleanup_partial(partial))
            if staging_dir is not None:
                try:
                    staging_dir.rmdir()  # bilinmeyen içerik varsa korunur; özyinelemeli silme YOK
                except OSError as exc:
                    cleanup_errors.append(exc)
                    _report_error(exc, log.warning, "Bu denemenin geçici yedek dizini kaldırılamadı: %s", staging_dir, exc_info=True)
            self._local.backup_cleanup_errors = tuple(cleanup_errors)
            if primary is not None and cleanup_errors:
                primary.cleanup_errors = tuple(cleanup_errors)
        log.info("Göç öncesi yedek hazır ve doğrulandı: %s", final)
        return final
