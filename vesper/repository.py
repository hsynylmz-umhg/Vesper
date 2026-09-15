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

import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from .session import (
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

        return "\n".join([
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
