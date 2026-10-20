"""
Oturum deposu (repository).

Bu, veritabanına dokunan TEK katman. main.py ve ui.py asla ham SQL
çalıştırmaz; sadece burada tanımlı metodları çağırır. Bu sayede şema
değişse bile çağıran kod aynı kalabilir.

Zaman damgaları: dışarıdan gelen (started_at/ended_at gibi) parametreler
timezone-aware datetime nesneleri olarak kabul edilir ve ISO-8601 UTC
string olarak saklanır. Sunumda (History arayüzünde) yerel saate çevirmek
ui.py'nin işidir.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import urlsplit

from .session import (
    Artifact,
    ARTIFACT_LINK,
    ARTIFACT_KINDS,
    ARTIFACT_CAPTURE_METHODS,
    CAPTURE_MANUAL,
    Segment,
    Session,
    Summary,
    STATUS_ACTIVE,
    STATUS_COMPLETED,
    STATUS_INTERRUPTED,
    SUMMARY_FINAL,
    SUMMARY_LIVE,
)
from .storage import Database

log = logging.getLogger("repository")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_iso(value) -> str:
    """datetime -> ISO string; zaten string ise olduğu gibi bırak."""
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _validate_session_id(session_id: str) -> None:
    """Oturum kimliğini dönüştürmeden doğrular."""
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("Geçerli bir oturum kimliği gerekli.")


def _validate_positive_id(value: int) -> None:
    """SQLite'ın örtük tür dönüşümlerini kimlik doğrulamasında kullanmaz."""
    if type(value) is not int or value < 1:
        raise ValueError("Kimlik pozitif bir tamsayı olmalı.")


def _validate_artifact_content(kind: str, content: str, capture_method: str) -> None:
    """Notu aynen korur; bağlantıyı ağ erişimi veya içerik dönüşümü yapmadan doğrular."""
    if not isinstance(kind, str) or kind not in ARTIFACT_KINDS:
        raise ValueError("Desteklenmeyen ek bilgi türü.")
    if not isinstance(capture_method, str) or capture_method not in ARTIFACT_CAPTURE_METHODS:
        raise ValueError("Desteklenmeyen yakalama yöntemi.")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Ek bilgi içeriği boş olamaz.")
    if kind != ARTIFACT_LINK:
        return
    if (any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in content)
            or any(char in content for char in '\\<>"')
            or re.search(r"%(?![0-9A-Fa-f]{2})", content)):
        raise ValueError("Bağlantı geçersiz karakter veya yüzde kodlaması içeriyor.")
    try:
        parsed = urlsplit(content)
        host, port = parsed.hostname, parsed.port
        if (parsed.scheme not in ("http", "https") or not host or not parsed.netloc
                or parsed.username is not None or parsed.password is not None
                or parsed.netloc.endswith(":") or (port is not None and port < 1)):
            raise ValueError("Mutlak HTTP/HTTPS bağlantısı ve geçerli sunucu gerekli.")
        if ":" in host:
            if "%" in host or not re.fullmatch(r"\[[^\]]+\](?::[0-9]+)?", parsed.netloc):
                raise ValueError("IPv6 sunucu bildirimi geçersiz.")
            ipaddress.IPv6Address(host)
        else:
            domain = host.encode("idna").decode("ascii").removesuffix(".")
            labels = domain.split(".")
            if (len(domain) > 253 or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                                         for label in labels)):
                raise ValueError("Sunucu adı geçersiz.")
            if len(labels) > 1 and all(label.isdigit() for label in labels):
                ipaddress.IPv4Address(domain)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Geçerli bir HTTP/HTTPS bağlantısı gerekli.") from exc


def _artifact_markdown_literal(text: str) -> str:
    """Ek bilgi metnini kapanamayacağı bir kod çitinde, etkin işaretleme olmadan gösterir."""
    fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", text)), default=0))
    return fence + "text\n" + text + "\n" + fence


class SessionRepository:
    def close_thread_connection(self) -> None:
        """Çağıran thread'e ait veritabanı bağlantısını güvenli biçimde kapatır."""
        self.db.close()

    def __init__(self, database: Database):
        self.db = database
        self.db.init_schema()

    # ------------------------------------------------------------------ #
    # Sessions
    # ------------------------------------------------------------------ #
    def create_session(
        self,
        title: str,
        source_type: str = "system_audio",
        source_name: Optional[str] = None,
        source_language: Optional[str] = None,
        summary_language: Optional[str] = None,
    ) -> str:
        session_id = uuid.uuid4().hex
        now = _now_iso()
        conn = self.db.get_connection()
        with conn:
            conn.execute(
                """
                INSERT INTO sessions
                    (id, title, started_at, ended_at, status, source_type,
                     source_name, source_language, summary_language,
                     created_at, updated_at)
                VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id, title, now, STATUS_ACTIVE, source_type,
                    source_name, source_language, summary_language, now, now,
                ),
            )
        return session_id

    def finish_session(self, session_id: str, status: str = STATUS_COMPLETED) -> None:
        now = _now_iso()
        conn = self.db.get_connection()
        with conn:
            conn.execute(
                "UPDATE sessions SET status = ?, ended_at = ?, updated_at = ? WHERE id = ?",
                (status, now, now, session_id),
            )

    def rename_session(self, session_id: str, new_title: str) -> None:
        new_title = new_title.strip()
        if not new_title:
            raise ValueError("Başlık boş olamaz.")
        conn = self.db.get_connection()
        with conn:
            conn.execute(
                "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
                (new_title, _now_iso(), session_id),
            )

    def delete_session(self, session_id: str) -> None:
        conn = self.db.get_connection()
        with conn:
            # foreign_keys=ON (storage.py) -> segments/summaries otomatik silinir.
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    def get_session(self, session_id: str) -> Optional[Session]:
        conn = self.db.get_connection()
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return Session.from_row(row) if row else None

    def list_sessions(self) -> List[Session]:
        """Tüm oturumlar, en yeni önce. Transkript/özet YÜKLEMEZ (hafif)."""
        conn = self.db.get_connection()
        rows = conn.execute("SELECT * FROM sessions ORDER BY started_at DESC").fetchall()
        return [Session.from_row(r) for r in rows]

    def recover_interrupted_sessions(self) -> List[str]:
        """Uygulama başlarken çağrılır. Temiz kapanmamış (status='active')
        oturumları 'interrupted' olarak işaretler. Veriyi SİLMEZ."""
        conn = self.db.get_connection()
        with conn:
            rows = conn.execute(
                "SELECT id FROM sessions WHERE status = ?", (STATUS_ACTIVE,)
            ).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                conn.execute(
                    f"UPDATE sessions SET status = ?, updated_at = ? "
                    f"WHERE status = ?",
                    (STATUS_INTERRUPTED, _now_iso(), STATUS_ACTIVE),
                )
        if ids:
            log.warning("%d oturum temiz kapanmamış, 'interrupted' olarak işaretlendi: %s", len(ids), ids)
        return ids

    # ------------------------------------------------------------------ #
    # Segments
    # ------------------------------------------------------------------ #
    def append_segment(
        self,
        session_id: str,
        sequence: int,
        started_at,
        ended_at,
        text: str,
        language: Optional[str] = None,
    ) -> None:
        conn = self.db.get_connection()
        with conn:
            conn.execute(
                """
                INSERT INTO segments
                    (session_id, sequence, started_at, ended_at, text, language, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id, sequence, _to_iso(started_at), _to_iso(ended_at),
                    text, language, _now_iso(),
                ),
            )

    def get_segments(self, session_id: str) -> List[Segment]:
        conn = self.db.get_connection()
        rows = conn.execute(
            "SELECT * FROM segments WHERE session_id = ? ORDER BY sequence ASC",
            (session_id,),
        ).fetchall()
        return [Segment.from_row(r) for r in rows]

    def get_full_transcript_text(self, session_id: str) -> str:
        return " ".join(s.text for s in self.get_segments(session_id))

    # ------------------------------------------------------------------ #
    # Elle yakalanan ek bilgiler
    # ------------------------------------------------------------------ #
    def add_artifact(
        self,
        session_id: str,
        kind: str,
        content: str,
        capture_method: str = CAPTURE_MANUAL,
        source_segment_id: Optional[int] = None,
        *,
        require_active: bool = False,
    ) -> int:
        """Sahipliği tek INSERT ile doğrular; içerik aynen saklanır.

        Motor require_active=True kullanır; geçmişe açık depo API'si varsayılan
        olarak yalnızca oturumun varlığını arar. Diğer yazma metotları gibi
        çağıran thread'in bağlantısının işlem bağlamını kullanır.
        """
        _validate_session_id(session_id)
        _validate_artifact_content(kind, content, capture_method)
        if source_segment_id is not None:
            _validate_positive_id(source_segment_id)
        if type(require_active) is not bool:
            raise ValueError("require_active yalnızca bool olabilir.")
        conn = self.db.get_connection()
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO artifacts
                    (session_id, kind, content, capture_method, source_segment_id, created_at)
                SELECT s.id, ?, ?, ?, ?, ? FROM sessions AS s
                WHERE s.id = ?
                  AND (? = 0 OR (s.status = ? AND s.ended_at IS NULL))
                  AND (? IS NULL OR EXISTS (
                      SELECT 1 FROM segments AS g WHERE g.id = ? AND g.session_id = s.id
                  ))
                """,
                (kind, content, capture_method, source_segment_id, _now_iso(), session_id,
                 int(require_active), STATUS_ACTIVE, source_segment_id, source_segment_id),
            )
            if cursor.rowcount != 1:
                row = conn.execute("SELECT status, ended_at FROM sessions WHERE id = ?", (session_id,)).fetchone()
                if row is None:
                    raise ValueError("Oturum bulunamadı.")
                if require_active and (row["status"] != STATUS_ACTIVE or row["ended_at"] is not None):
                    raise RuntimeError("Ek bilgi için etkin ve bitmemiş bir oturum gerekli.")
                raise ValueError("Kaynak parça bu oturuma ait değil veya bulunamadı.")
            artifact_id = cursor.lastrowid
        return artifact_id

    def list_artifacts(self, session_id: str) -> List[Artifact]:
        """Yalnızca istenen oturumun ek bilgilerini zaman/id sırasıyla döndürür."""
        _validate_session_id(session_id)
        conn = self.db.get_connection()
        rows = conn.execute(
            "SELECT * FROM artifacts WHERE session_id = ? ORDER BY created_at ASC, id ASC", (session_id,),
        ).fetchall()
        return [Artifact.from_row(row) for row in rows]

    def delete_artifact(self, session_id: str, artifact_id: int) -> bool:
        """Kimlik ve oturum sahipliği aynı DELETE koşulunda doğrulanır."""
        _validate_session_id(session_id)
        _validate_positive_id(artifact_id)
        conn = self.db.get_connection()
        with conn:
            cursor = conn.execute(
                "DELETE FROM artifacts WHERE id = ? AND session_id = ?", (artifact_id, session_id),
            )
        return cursor.rowcount == 1

    # ------------------------------------------------------------------ #
    # Summaries
    # ------------------------------------------------------------------ #
    def save_summary(
        self, session_id: str, kind: str, content: str, model: Optional[str] = None
    ) -> None:
        conn = self.db.get_connection()
        with conn:
            conn.execute(
                """
                INSERT INTO summaries (session_id, kind, content, model, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, kind, content, model, _now_iso()),
            )

    def get_latest_summary(self, session_id: str, kind: str) -> Optional[Summary]:
        conn = self.db.get_connection()
        row = conn.execute(
            """
            SELECT * FROM summaries
            WHERE session_id = ? AND kind = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (session_id, kind),
        ).fetchone()
        return Summary.from_row(row) if row else None

    # ------------------------------------------------------------------ #
    # Export
    # ------------------------------------------------------------------ #
    def export_markdown(self, session_id: str) -> str:
        """Kalıcı (SQLite) veriden final Markdown üretir. Markdown dosyası
        artık kaynak değil, sadece bu fonksiyonun bir çıktısıdır."""
        session = self.get_session(session_id)
        if session is None:
            raise ValueError(f"Oturum bulunamadı: {session_id}")

        transcript = self.get_full_transcript_text(session_id)

        summary = self.get_latest_summary(session_id, SUMMARY_FINAL)
        if summary is None:
            # Final özet hiç üretilememişse (örn. kesilen oturum), en azından
            # son canlı özeti göster — sessizce boş bırakmaktansa.
            summary = self.get_latest_summary(session_id, SUMMARY_LIVE)
        summary_text = summary.content if summary else "Özet üretilemedi."

        markdown = "\n".join([
            f"# {session.title}",
            "",
            summary_text,
            "",
            "---",
            "",
            "## Tam Transkript",
            "",
            transcript if transcript.strip() else "(transkript boş)",
        ])
        artifacts = self.list_artifacts(session_id)
        if artifacts:
            materials = []
            for position, artifact in enumerate(artifacts, 1):
                label = {"link": "Link", "note": "Note"}.get(artifact.kind, "Artifact")
                literal = f"Kind: {artifact.kind}\nCreated at: {artifact.created_at}\n\n{artifact.content}"
                materials.append(f"### {position}. {label}\n\n" + _artifact_markdown_literal(literal))
            markdown += "\n\n---\n\n## Meeting Materials\n\n" + "\n\n".join(materials)
        return markdown
