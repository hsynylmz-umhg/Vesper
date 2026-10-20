"""
Oturum (session) alan modeli.

Bu dosya sadece veri şekillerini tanımlar; SQL burada YOK (bkz. storage.py /
repository.py). sqlite3.Row nesnelerinden dataclass'a çeviren from_row()
yardımcıları, repository.py'nin tek bir yerden tip-güvenli nesne dönmesini
sağlar.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Oturum durumları
STATUS_ACTIVE = "active"          # şu anda kaydediliyor
STATUS_COMPLETED = "completed"    # Durdur ile temiz şekilde bitti
STATUS_INTERRUPTED = "interrupted"  # temiz kapanmadı: uygulama/Python çöktü
                                     # YA DA ses kaynağı (ör. cihaz kaybı/host
                                     # hatası) oturum SIRASINDA beklenmedik
                                     # şekilde koptu (v0.3.0)

# Özet türleri
SUMMARY_LIVE = "live"
SUMMARY_FINAL = "final"

# Elle yakalanan ek bilgi türleri ve yakalama yöntemleri.
ARTIFACT_LINK = "link"
ARTIFACT_NOTE = "note"
CAPTURE_MANUAL = "manual"
ARTIFACT_KINDS = (ARTIFACT_LINK, ARTIFACT_NOTE)
ARTIFACT_CAPTURE_METHODS = (CAPTURE_MANUAL,)


@dataclass
class Session:
    id: str
    title: str
    started_at: str          # ISO-8601 UTC
    ended_at: Optional[str]
    status: str
    source_type: str
    source_name: Optional[str]
    source_language: Optional[str]
    summary_language: Optional[str]
    created_at: str
    updated_at: str

    @staticmethod
    def from_row(row) -> "Session":
        return Session(
            id=row["id"],
            title=row["title"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            status=row["status"],
            source_type=row["source_type"],
            source_name=row["source_name"],
            source_language=row["source_language"],
            summary_language=row["summary_language"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass
class Segment:
    id: int
    session_id: str
    sequence: int
    started_at: str
    ended_at: str
    text: str
    language: Optional[str]
    created_at: str

    @staticmethod
    def from_row(row) -> "Segment":
        return Segment(
            id=row["id"],
            session_id=row["session_id"],
            sequence=row["sequence"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            text=row["text"],
            language=row["language"],
            created_at=row["created_at"],
        )


@dataclass
class Summary:
    id: int
    session_id: str
    kind: str  # SUMMARY_LIVE | SUMMARY_FINAL
    content: str
    model: Optional[str]
    created_at: str

    @staticmethod
    def from_row(row) -> "Summary":
        return Summary(
            id=row["id"],
            session_id=row["session_id"],
            kind=row["kind"],
            content=row["content"],
            model=row["model"],
            created_at=row["created_at"],
        )


@dataclass
class Artifact:
    """Şema v2 ile uyumlu, tek bir oturuma ait elle yakalanmış ek bilgi."""

    id: int
    session_id: str
    kind: str
    content: str
    capture_method: str
    source_segment_id: Optional[int]
    created_at: str

    @staticmethod
    def from_row(row) -> "Artifact":
        return Artifact(
            id=row["id"],
            session_id=row["session_id"],
            kind=row["kind"],
            content=row["content"],
            capture_method=row["capture_method"],
            source_segment_id=row["source_segment_id"],
            created_at=row["created_at"],
        )
