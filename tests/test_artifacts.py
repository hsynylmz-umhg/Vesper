"""Ek bilgi deposunu geçici veritabanlarıyla ve thread-yerel bağlantılarla sınar.

Donanım veya ağ erişimi gerekmez; işçiler kendi bağlantılarını kapatır.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from vesper.repository import SessionRepository
from vesper.session import (
    ARTIFACT_CAPTURE_METHODS,
    ARTIFACT_KINDS,
    ARTIFACT_LINK,
    ARTIFACT_NOTE,
    CAPTURE_MANUAL,
    Artifact,
    STATUS_ACTIVE,
    STATUS_COMPLETED,
    STATUS_INTERRUPTED,
)
from vesper.storage import Database


class ArtifactDbTestCase(unittest.TestCase):
    """Her test için bağımsız, yeni bir şema v2 dosyası kurar."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db = Database(Path(self.temp_dir.name) / "artifacts.sqlite")
        self.addCleanup(self.db.close)
        self.repo = SessionRepository(self.db)
        self.session_id = self.repo.create_session("Artifact oturumu")

    def add_note(self, content="Bir not", **kwargs):
        return self.repo.add_artifact(
            self.session_id, ARTIFACT_NOTE, content, **kwargs
        )

    def add_segment(self, session_id=None):
        session_id = session_id or self.session_id
        self.repo.append_segment(session_id, 1, "t0", "t1", "Bir konuşma")
        return self.repo.get_segments(session_id)[0].id

    def artifact_ids(self, session_id=None):
        return [
            item.id
            for item in self.repo.list_artifacts(session_id or self.session_id)
        ]


class TestArtifactModel(ArtifactDbTestCase):
    def test_supported_values_are_explicit(self):
        self.assertEqual(ARTIFACT_LINK, "link")
        self.assertEqual(ARTIFACT_NOTE, "note")
        self.assertEqual(CAPTURE_MANUAL, "manual")
        self.assertEqual(ARTIFACT_KINDS, ("link", "note"))
        self.assertEqual(ARTIFACT_CAPTURE_METHODS, ("manual",))

    def test_model_matches_persisted_columns(self):
        self.assertEqual(
            [field.name for field in fields(Artifact)],
            ["id", "session_id", "kind", "content", "capture_method",
             "source_segment_id", "created_at"],
        )
        artifact_id = self.add_note()
        row = self.db.get_connection().execute(
            "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
        ).fetchone()
        artifact = Artifact.from_row(row)
        self.assertEqual(artifact, self.repo.list_artifacts(self.session_id)[0])
        self.assertEqual(artifact.id, artifact_id)
        self.assertEqual(artifact.session_id, self.session_id)
        self.assertEqual(artifact.kind, ARTIFACT_NOTE)
        self.assertEqual(artifact.capture_method, CAPTURE_MANUAL)
        self.assertIsNone(artifact.source_segment_id)
        self.assertTrue(artifact.created_at)


class TestArtifactValidation(ArtifactDbTestCase):
    def test_valid_link_is_stored_without_rewriting(self):
        content = "HTTPS://Example.COM:443/a%20b?q=Bir#Bölüm"
        artifact_id = self.repo.add_artifact(
            self.session_id, ARTIFACT_LINK, content
        )
        artifact = self.repo.list_artifacts(self.session_id)[0]
        self.assertEqual(artifact.id, artifact_id)
        self.assertEqual(artifact.content, content)
        self.assertEqual(artifact.kind, ARTIFACT_LINK)

    def test_note_preserves_meaningful_formatting(self):
        content = "  Başlık\n\n\t- İlk madde\r\n- İkinci madde  \n"
        self.add_note(content)
        self.assertEqual(
            self.repo.list_artifacts(self.session_id)[0].content, content
        )

    def test_note_can_contain_a_manual_email_excerpt(self):
        content = "Kimden: ekip@example.com\nKonu: Karar\n\nYarın görüşelim."
        self.add_note(content)
        artifact = self.repo.list_artifacts(self.session_id)[0]
        self.assertEqual(artifact.kind, ARTIFACT_NOTE)
        self.assertEqual(artifact.capture_method, CAPTURE_MANUAL)
        self.assertEqual(artifact.content, content)

    def test_sql_text_is_data_not_a_statement(self):
        content = "'); DELETE FROM sessions; --\nBir alıntı: \"merhaba\""
        artifact_id = self.add_note(content)
        self.assertIsNotNone(self.repo.get_session(self.session_id))
        self.assertEqual(self.artifact_ids(), [artifact_id])
        self.assertEqual(
            self.repo.list_artifacts(self.session_id)[0].content, content
        )

    def test_empty_and_whitespace_content_rejected(self):
        for kind in ARTIFACT_KINDS:
            for content in ("", " ", "\t\r\n", "\u2003\u00a0"):
                with self.subTest(kind=kind, content=repr(content)):
                    with self.assertRaises(ValueError):
                        self.repo.add_artifact(self.session_id, kind, content)
        self.assertEqual(self.artifact_ids(), [])

    def test_non_string_content_rejected(self):
        for content in (None, 7, True, b"not", [], {}):
            with self.subTest(content=repr(content)):
                with self.assertRaises(ValueError):
                    self.add_note(content)
        self.assertEqual(self.artifact_ids(), [])

    def test_unsupported_and_non_string_kinds_rejected(self):
        for kind in ("email", "chat", "LINK", "note ", "", None, True, [], {}):
            with self.subTest(kind=repr(kind)):
                with self.assertRaises(ValueError):
                    self.repo.add_artifact(self.session_id, kind, "Bir not")
        self.assertEqual(self.artifact_ids(), [])

    def test_unsupported_and_non_string_capture_methods_rejected(self):
        for method in ("automatic", "clipboard", "MANUAL", "manual ", "", None,
                       True, [], {}):
            with self.subTest(method=repr(method)):
                with self.assertRaises(ValueError):
                    self.add_note(capture_method=method)
        self.assertEqual(self.artifact_ids(), [])

    def test_explicit_manual_capture_accepted(self):
        self.add_note(capture_method=CAPTURE_MANUAL)
        self.assertEqual(
            self.repo.list_artifacts(self.session_id)[0].capture_method,
            CAPTURE_MANUAL,
        )

    def test_http_https_and_supported_host_forms_accepted(self):
        contents = (
            "http://localhost:8080/",
            "https://127.0.0.1/path",
            "https://[::1]:8443/a",
            "https://örnek.example/İstanbul?q=şöyle",
            "https://xn--rnek-4qa.example/%C3%BC",
            "https://example.com/a?x=1&y=2#başlık",
        )
        for content in contents:
            with self.subTest(content=content):
                self.repo.add_artifact(self.session_id, ARTIFACT_LINK, content)
        self.assertCountEqual(
            [item.content for item in self.repo.list_artifacts(self.session_id)],
            contents,
        )

    def test_unsupported_schemes_and_relative_urls_rejected(self):
        for content in (
            "ftp://example.com/file", "file:///C:/notes.txt",
            "javascript:alert(1)", "mailto:user@example.com", "example.com/a",
            "/relative/path", "//example.com/a", "https:example.com",
        ):
            with self.subTest(content=content):
                with self.assertRaises(ValueError):
                    self.repo.add_artifact(self.session_id, ARTIFACT_LINK, content)
        self.assertEqual(self.artifact_ids(), [])

    def test_url_whitespace_and_control_characters_rejected(self):
        for content in (
            " https://example.com", "https://example.com ",
            "https://example.com/a b", "https://example.com/\tpath",
            "https://exam\nple.com/", "https://example.com/\x00",
            "https://example.com/\x7f", "https://example.com/\u00a0",
        ):
            with self.subTest(content=repr(content)):
                with self.assertRaises(ValueError):
                    self.repo.add_artifact(self.session_id, ARTIFACT_LINK, content)
        self.assertEqual(self.artifact_ids(), [])

    def test_url_credentials_backslashes_and_bad_percent_rejected(self):
        for content in (
            "https://user@example.com/", "https://user:secret@example.com/",
            "https://example.com\\evil", "https://example.com/path\\other",
            "https://example.com/%", "https://example.com/%2",
            "https://example.com/%GG",
        ):
            with self.subTest(content=content):
                with self.assertRaises(ValueError):
                    self.repo.add_artifact(self.session_id, ARTIFACT_LINK, content)
        self.assertEqual(self.artifact_ids(), [])

    def test_url_missing_or_malformed_hosts_rejected(self):
        for content in (
            "https://", "https:///path", "https://:443/path", "https://[]/",
            "https://[::1/path", "https://-example.com/", "https://example-.com/",
            "https://example..com/", "https://exa_mple.com/",
        ):
            with self.subTest(content=content):
                with self.assertRaises(ValueError):
                    self.repo.add_artifact(self.session_id, ARTIFACT_LINK, content)
        self.assertEqual(self.artifact_ids(), [])

    def test_url_invalid_ports_rejected(self):
        for content in (
            "https://example.com:abc/", "https://example.com:-1/",
            "https://example.com:65536/", "https://example.com:/",
        ):
            with self.subTest(content=content):
                with self.assertRaises(ValueError):
                    self.repo.add_artifact(self.session_id, ARTIFACT_LINK, content)
        self.assertEqual(self.artifact_ids(), [])


class TestArtifactOwnership(ArtifactDbTestCase):
    def test_missing_session_rejected_without_creating_rows(self):
        with self.assertRaises(ValueError):
            self.repo.add_artifact("missing-session", ARTIFACT_NOTE, "Bir not")
        self.assertEqual(self.artifact_ids(), [])

    def test_session_ids_must_be_nonempty_strings(self):
        for session_id in (None, "", "  ", 1, True, [], {}):
            with self.subTest(session_id=repr(session_id)):
                with self.assertRaises(ValueError):
                    self.repo.add_artifact(session_id, ARTIFACT_NOTE, "Bir not")
        self.assertEqual(self.artifact_ids(), [])

    def test_nullable_segment_reference_is_preserved(self):
        self.add_note(source_segment_id=None)
        self.assertIsNone(
            self.repo.list_artifacts(self.session_id)[0].source_segment_id
        )

    def test_same_session_segment_reference_is_preserved(self):
        segment_id = self.add_segment()
        artifact_id = self.add_note(source_segment_id=segment_id)
        artifact = self.repo.list_artifacts(self.session_id)[0]
        self.assertEqual(artifact.id, artifact_id)
        self.assertEqual(artifact.source_segment_id, segment_id)
        self.assertEqual(artifact.session_id, self.session_id)

    def test_missing_segment_rejected(self):
        with self.assertRaises(ValueError):
            self.add_note(source_segment_id=999999)
        self.assertEqual(self.artifact_ids(), [])

    def test_cross_session_segment_rejected(self):
        other_session = self.repo.create_session("Başka oturum")
        segment_id = self.add_segment(other_session)
        with self.assertRaises(ValueError):
            self.add_note(source_segment_id=segment_id)
        self.assertEqual(self.artifact_ids(), [])
        self.assertEqual(self.artifact_ids(other_session), [])
        self.assertEqual(self.repo.get_segments(other_session)[0].id, segment_id)

    def test_segment_ids_must_be_positive_exact_integers(self):
        segment_id = self.add_segment()
        self.assertEqual(segment_id, 1)
        for invalid_id in (True, False, 0, -1, 1.0, "1", [], {}):
            with self.subTest(invalid_id=repr(invalid_id)):
                with self.assertRaises(ValueError):
                    self.add_note(source_segment_id=invalid_id)
        self.assertEqual(self.artifact_ids(), [])

    def test_listing_is_scoped_to_session(self):
        own_id = self.add_note("Birinci")
        other_session = self.repo.create_session("Başka oturum")
        other_id = self.repo.add_artifact(other_session, ARTIFACT_NOTE, "İkinci")
        self.assertEqual(self.artifact_ids(), [own_id])
        self.assertEqual(self.artifact_ids(other_session), [other_id])

    def test_listing_uses_timestamp_then_id_order(self):
        with patch("vesper.repository._now_iso", return_value="2026-10-20T12:00:02+00:00"):
            last_id = self.add_note("Son")
        with patch("vesper.repository._now_iso", return_value="2026-10-20T12:00:01+00:00"):
            first_id = self.add_note("İlk")
            second_id = self.add_note("İkinci")
        self.assertEqual(self.artifact_ids(), [first_id, second_id, last_id])
        self.assertEqual(self.artifact_ids(), [first_id, second_id, last_id])

    def test_delete_requires_matching_session(self):
        artifact_id = self.add_note()
        other_session = self.repo.create_session("Başka oturum")
        self.assertFalse(self.repo.delete_artifact(other_session, artifact_id))
        self.assertEqual(self.artifact_ids(), [artifact_id])
        self.assertTrue(self.repo.delete_artifact(self.session_id, artifact_id))
        self.assertFalse(self.repo.delete_artifact(self.session_id, artifact_id))
        self.assertEqual(self.artifact_ids(), [])

    def test_delete_does_not_affect_neighboring_artifacts(self):
        first_id = self.add_note("Birinci")
        second_id = self.add_note("İkinci")
        self.assertTrue(self.repo.delete_artifact(self.session_id, first_id))
        self.assertEqual(self.artifact_ids(), [second_id])

    def test_delete_ids_must_be_positive_exact_integers(self):
        artifact_id = self.add_note()
        self.assertEqual(artifact_id, 1)
        for invalid_id in (None, True, False, 0, -1, 1.0, "1", [], {}):
            with self.subTest(invalid_id=repr(invalid_id)):
                with self.assertRaises(ValueError):
                    self.repo.delete_artifact(self.session_id, invalid_id)
        self.assertEqual(self.artifact_ids(), [artifact_id])

    def test_deleting_session_cascades_artifacts_with_and_without_segments(self):
        segment_id = self.add_segment()
        self.add_note("Bağlı", source_segment_id=segment_id)
        self.add_note("Bağımsız")
        other_session = self.repo.create_session("Korunacak oturum")
        other_id = self.repo.add_artifact(other_session, ARTIFACT_NOTE, "Korunacak")
        self.repo.delete_session(self.session_id)
        self.assertIsNone(self.repo.get_session(self.session_id))
        self.assertEqual(self.artifact_ids(), [])
        self.assertEqual(self.repo.get_segments(self.session_id), [])
        self.assertEqual(self.artifact_ids(other_session), [other_id])

    def test_composite_foreign_key_remains_enforced(self):
        other_session = self.repo.create_session("Başka oturum")
        segment_id = self.add_segment(other_session)
        conn = self.db.get_connection()
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            with conn:
                conn.execute(
                    "INSERT INTO artifacts (session_id, kind, content, capture_method, "
                    "source_segment_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (self.session_id, ARTIFACT_NOTE, "Geçersiz", CAPTURE_MANUAL,
                     segment_id, "2026-10-20T00:00:00+00:00"),
                )
        self.assertEqual(self.artifact_ids(), [])
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_referenced_segment_deletion_does_not_silently_reassign_artifact(self):
        segment_id = self.add_segment()
        artifact_id = self.add_note(source_segment_id=segment_id)
        conn = self.db.get_connection()
        with self.assertRaises(sqlite3.IntegrityError):
            with conn:
                conn.execute("DELETE FROM segments WHERE id = ?", (segment_id,))
        self.assertEqual(self.artifact_ids(), [artifact_id])
        self.assertEqual(
            self.repo.list_artifacts(self.session_id)[0].source_segment_id,
            segment_id,
        )


class TestArtifactSessionState(ArtifactDbTestCase):
    def test_active_guard_accepts_persisted_active_session(self):
        artifact_id = self.add_note(require_active=True)
        self.assertEqual(self.artifact_ids(), [artifact_id])
        session = self.repo.get_session(self.session_id)
        self.assertEqual(session.status, STATUS_ACTIVE)
        self.assertIsNone(session.ended_at)

    def test_active_guard_rejects_completed_and_interrupted_sessions(self):
        for status in (STATUS_COMPLETED, STATUS_INTERRUPTED):
            with self.subTest(status=status):
                session_id = self.repo.create_session("Bitmiş oturum")
                self.repo.finish_session(session_id, status=status)
                with self.assertRaises(RuntimeError):
                    self.repo.add_artifact(
                        session_id, ARTIFACT_NOTE, "Geç not", require_active=True
                    )
                self.assertEqual(self.artifact_ids(session_id), [])

    def test_active_guard_rejects_active_status_with_end_timestamp(self):
        conn = self.db.get_connection()
        with conn:
            conn.execute(
                "UPDATE sessions SET ended_at = ? WHERE id = ?",
                ("2026-10-20T00:00:00+00:00", self.session_id),
            )
        with self.assertRaises(RuntimeError):
            self.add_note(require_active=True)
        self.assertEqual(self.artifact_ids(), [])

    def test_active_guard_rejects_nonactive_status_without_end_timestamp(self):
        conn = self.db.get_connection()
        with conn:
            conn.execute(
                "UPDATE sessions SET status = ? WHERE id = ?",
                (STATUS_INTERRUPTED, self.session_id),
            )
        with self.assertRaises(RuntimeError):
            self.add_note(require_active=True)
        self.assertEqual(self.artifact_ids(), [])

    def test_active_guard_missing_session_is_still_validation_error(self):
        with self.assertRaises(ValueError):
            self.repo.add_artifact(
                "missing-session", ARTIFACT_NOTE, "Bir not", require_active=True
            )
        self.assertEqual(self.artifact_ids(), [])

    def test_history_api_accepts_existing_ended_session_by_default(self):
        self.repo.finish_session(self.session_id)
        artifact_id = self.add_note("Tarihsel not")
        self.assertEqual(self.artifact_ids(), [artifact_id])
        self.assertEqual(self.repo.get_session(self.session_id).status, STATUS_COMPLETED)


class TestArtifactThreadLocalRepository(ArtifactDbTestCase):
    def test_same_thread_reuses_connection_and_reopens_after_explicit_close(self):
        original = self.db.get_connection()
        first_id = self.add_note("İlk")
        self.assertIs(self.db.get_connection(), original)
        self.repo.close_thread_connection()
        with self.assertRaises(sqlite3.ProgrammingError):
            original.execute("SELECT 1")
        second_id = self.add_note("İkinci")
        replacement = self.db.get_connection()
        self.assertIsNot(replacement, original)
        self.assertEqual(self.artifact_ids(), [first_id, second_id])
        self.assertEqual(replacement.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_worker_closure_preserves_other_thread_and_main_connections(self):
        main_conn = self.db.get_connection()
        ready = threading.Barrier(2)
        first_closed = threading.Event()

        def first_worker():
            try:
                conn = self.db.get_connection()
                artifact_id = self.add_note("Birinci işçi")
                self.assertIs(self.db.get_connection(), conn)
                ready.wait(timeout=15)
                self.repo.close_thread_connection()
                with self.assertRaises(sqlite3.ProgrammingError):
                    conn.execute("SELECT 1")
                first_closed.set()
                return conn, artifact_id
            finally:
                first_closed.set()
                self.repo.close_thread_connection()

        def second_worker():
            try:
                conn = self.db.get_connection()
                artifact_id = self.add_note("İkinci işçi")
                self.assertIs(self.db.get_connection(), conn)
                ready.wait(timeout=15)
                self.assertTrue(first_closed.wait(timeout=15))
                self.assertIs(self.db.get_connection(), conn)
                self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)
                self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertIn(artifact_id, self.artifact_ids())
                return conn, artifact_id
            finally:
                self.repo.close_thread_connection()

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(first_worker)
            second_future = executor.submit(second_worker)
            first_conn, first_id = first_future.result(timeout=30)
            second_conn, second_id = second_future.result(timeout=30)
        self.assertIsNot(first_conn, second_conn)
        self.assertIsNot(first_conn, main_conn)
        self.assertIsNot(second_conn, main_conn)
        self.assertIs(self.db.get_connection(), main_conn)
        self.assertEqual(main_conn.execute("SELECT 1").fetchone()[0], 1)
        self.assertCountEqual(self.artifact_ids(), [first_id, second_id])


if __name__ == "__main__":
    unittest.main()
