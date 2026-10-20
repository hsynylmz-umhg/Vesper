"""Elle eklenen ek bilgilerin motor sahipliğini geçici verilerle sınar.

Ses ve model katmanları sahtedir; yarış testleri işçileri olaylarla eşgüdümler.
"""

import gc
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from tests import test_lifecycle_integration as lifecycle
from vesper.repository import SessionRepository
from vesper.session import CAPTURE_MANUAL, STATUS_ACTIVE, STATUS_COMPLETED
from vesper.storage import Database


class _ObservedLock:
    """Durdurma işçisinin kilide gerçekten ulaşmasını görünür kılar."""

    def __init__(self, lock, attempted):
        self.lock = lock
        self.attempted = attempted

    def __enter__(self):
        if threading.current_thread().name == "artifact-stop":
            self.attempted.set()
        if not self.lock.acquire(timeout=10):
            raise AssertionError("Eser oturum kilidi zamanında alınamadı.")
        return self

    def __exit__(self, *args):
        self.lock.release()


class TestArtifactLifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(gc.collect)
        self.temp_path = Path(self.temp.name)
        database = Database(self.temp_path / "artifact_lifecycle.db")
        self.addCleanup(database.close)
        self.repo = SessionRepository(database)
        self.original_cwd = os.getcwd()
        self.addCleanup(os.chdir, self.original_cwd)
        os.chdir(self.temp_path)
        for target, replacement in (
            ("vesper.engine.AudioCapture", lifecycle.FakeAudioCapture),
            ("vesper.engine.Transcriber", lifecycle.FakeTranscriber),
            ("vesper.engine.Summarizer", lifecycle.FakeSummarizer),
            ("vesper.engine.config.OUTPUT_DIR", str(self.temp_path / "outputs")),
        ):
            patcher = patch(target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        log_patcher = patch("vesper.engine.log")
        log_patcher.start()
        self.addCleanup(log_patcher.stop)
        from vesper import engine

        self.engine = engine
        self.assistant = engine.WebinarAssistant(repository=self.repo)
        self.worker_threads = []

    def tearDown(self):
        self.assistant.capture.confirm_stopped = True
        if self.assistant._running:
            self.assistant.stop()
        transcriber = self.assistant._transcribe_thread
        if transcriber is not None and transcriber.is_alive():
            self.assistant._audio_queue.put(self.engine._TRANSCRIBE_SENTINEL)
        threads = self.worker_threads + list(self.assistant._threads)
        for worker in set(threads):
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive(), "Test bir motor işçisini canlı bıraktı.")

    def _start(self):
        self.assistant.start()
        self.worker_threads.extend(self.assistant._threads)
        return self.assistant.session_id

    def _note(self, session_id, content="Elle girilen not", **kwargs):
        return self.assistant.add_artifact(session_id, "note", content, **kwargs)

    def _append_segment(self, session_id):
        now = datetime.now(timezone.utc)
        self.repo.append_segment(session_id, 1, now, now, "Kaynak konuşma")
        return self.repo.get_segments(session_id)[0].id

    def test_active_session_accepts_link_and_preserves_note_formatting(self):
        session_id = self._start()
        content = "  İlk satır\n\n- İkinci satır\t  "
        note_id = self._note(session_id, content)
        link_id = self.assistant.add_artifact(
            session_id, "link", "https://example.com/path?q=1#section"
        )
        artifacts = self.assistant.list_artifacts(session_id)
        self.assertEqual([item.id for item in artifacts], [note_id, link_id])
        self.assertEqual(artifacts[0].content, content)
        self.assertEqual(artifacts[0].capture_method, CAPTURE_MANUAL)
        self.assertIsNone(artifacts[0].source_segment_id)
        self.assertEqual(artifacts[1].kind, "link")
        self.assertTrue(all(item.session_id == session_id for item in artifacts))

    def test_creation_without_started_session_is_rejected(self):
        with self.assertRaises(RuntimeError):
            self._note("missing")
        self.assertEqual(self.repo.list_sessions(), [])

    def test_another_persisted_active_session_is_not_the_engine_session(self):
        current_id = self._start()
        other_id = self.repo.create_session("Başka oturum")
        with self.assertRaises(RuntimeError):
            self._note(other_id)
        self.assertEqual(self.repo.list_artifacts(other_id), [])
        self.assertEqual(self.repo.list_artifacts(current_id), [])

    def test_missing_private_token_rejects_otherwise_current_running_session(self):
        session_id = self._start()
        self.assistant._artifact_session_id = None
        with self.assertRaises(RuntimeError):
            self._note(session_id)
        self.assertEqual(self.repo.list_artifacts(session_id), [])

    def test_public_session_id_reassignment_cannot_redirect_artifact(self):
        original_id = self._start()
        other_id = self.repo.create_session("Yanlış hedef")
        self.assistant.session_id = other_id
        try:
            with self.assertRaises(RuntimeError):
                self._note(other_id)
            with self.assertRaises(RuntimeError):
                self._note(original_id)
            self.assertEqual(self.repo.list_artifacts(other_id), [])
            self.assertEqual(self.repo.list_artifacts(original_id), [])
        finally:
            self.assistant.session_id = original_id

    def test_external_session_completion_is_rejected_while_engine_runs(self):
        session_id = self._start()
        self.repo.finish_session(session_id)
        self.assertTrue(self.assistant._running)
        with self.assertRaises(RuntimeError):
            self._note(session_id)
        self.assertEqual(self.repo.list_artifacts(session_id), [])

    def test_active_status_with_non_null_end_time_is_rejected(self):
        session_id = self._start()
        conn = self.repo.db.get_connection()
        with conn:
            conn.execute(
                "UPDATE sessions SET ended_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), session_id),
            )
        self.assertEqual(self.repo.get_session(session_id).status, STATUS_ACTIVE)
        with self.assertRaises(RuntimeError):
            self._note(session_id)
        self.assertEqual(self.repo.list_artifacts(session_id), [])

    def test_external_session_deletion_is_rejected(self):
        session_id = self._start()
        self.repo.delete_session(session_id)
        with self.assertRaises((RuntimeError, ValueError)):
            self._note(session_id)
        self.assertIsNone(self.repo.get_session(session_id))
        self.assertEqual(self.repo.list_artifacts(session_id), [])

    def test_stale_request_after_restart_does_not_attach_to_new_session(self):
        first_id = self._start()
        first_artifact = self._note(first_id, "Birinci oturum")
        self.assistant.stop()
        second_id = self._start()
        self.assertNotEqual(first_id, second_id)
        with self.assertRaises(RuntimeError):
            self._note(first_id, "Geciken istek")
        second_artifact = self._note(second_id, "İkinci oturum")
        self.assertEqual([a.id for a in self.repo.list_artifacts(first_id)], [first_artifact])
        self.assertEqual([a.id for a in self.repo.list_artifacts(second_id)], [second_artifact])

    def test_stopped_session_retains_history_but_is_not_active(self):
        session_id = self._start()
        artifact_id = self._note(session_id)
        self.assistant.stop()
        self.assertEqual(self.assistant.session_id, session_id)
        self.assertIsNone(self.assistant._artifact_session_id)
        with self.assertRaises(RuntimeError):
            self._note(session_id)
        self.assertEqual([a.id for a in self.assistant.list_artifacts(session_id)], [artifact_id])

    def test_failed_capture_stop_cannot_keep_artifact_session_active(self):
        session_id = self._start()
        self.assistant.capture.confirm_stopped = False
        self.assistant.stop()
        self.assertFalse(self.assistant._running)
        self.assertEqual(self.repo.get_session(session_id).status, STATUS_ACTIVE)
        self.assertIsNone(self.assistant._artifact_session_id)
        with self.assertRaises(RuntimeError):
            self._note(session_id)
        self.assertEqual(self.repo.list_artifacts(session_id), [])

    def test_failed_final_summary_cannot_keep_artifact_session_active(self):
        session_id = self._start()
        with patch.object(self.assistant.summarizer, "summarize", side_effect=RuntimeError("Özet hatası")):
            self.assistant.stop()
        self.assertEqual(self.repo.get_session(session_id).status, STATUS_ACTIVE)
        self.assertIsNone(self.assistant._artifact_session_id)
        with self.assertRaises(RuntimeError):
            self._note(session_id)

    def test_old_retained_id_is_rejected_during_capture_start(self):
        old_id = self._start()
        self.assistant.stop()
        callback_ran = []
        real_start = self.assistant.capture.start

        def checking_start():
            callback_ran.append(True)
            self.assertTrue(self.assistant._running)
            self.assertEqual(self.assistant.session_id, old_id)
            self.assertIsNone(self.assistant._artifact_session_id)
            with self.assertRaises(RuntimeError):
                self._note(old_id, "Başlatma aralığında geciken istek")
            real_start()

        with patch.object(self.assistant.capture, "start", checking_start):
            new_id = self._start()
        self.assertEqual(callback_ran, [True])
        self.assertNotEqual(new_id, old_id)
        self.assertEqual(self.repo.list_artifacts(old_id), [])
        self.assertEqual(self.repo.list_artifacts(new_id), [])

    def test_failed_capture_start_clears_previous_artifact_token(self):
        old_id = self._start()
        self.assistant.stop()
        with patch.object(self.assistant.capture, "start", side_effect=RuntimeError("Yakalama hatası")):
            with self.assertRaises(RuntimeError):
                self._start()
        self.assertFalse(self.assistant._running)
        self.assertIsNone(self.assistant._artifact_session_id)
        with self.assertRaises(RuntimeError):
            self._note(old_id)
        self.assertEqual(self.repo.list_artifacts(old_id), [])

    def test_failed_persistent_session_creation_rejects_artifacts(self):
        with patch.object(self.repo, "create_session", side_effect=RuntimeError("Kayıt hatası")):
            self._start()
        self.assertTrue(self.assistant._running)
        self.assertIsNone(self.assistant.session_id)
        self.assertIsNone(self.assistant._artifact_session_id)
        with self.assertRaises(RuntimeError):
            self._note("missing")
        self.assertEqual(self.repo.list_sessions(), [])

    def test_repository_failure_is_propagated_without_changing_current_owner(self):
        session_id = self._start()
        with patch.object(self.repo, "add_artifact", side_effect=RuntimeError("Yazım hatası")):
            with self.assertRaisesRegex(RuntimeError, "Yazım hatası"):
                self._note(session_id)
        self.assertEqual(self.assistant._artifact_session_id, session_id)
        artifact_id = self._note(session_id, "Sonraki geçerli istek")
        self.assertEqual([a.id for a in self.repo.list_artifacts(session_id)], [artifact_id])

    def test_engine_propagates_kind_capture_content_and_link_validation(self):
        session_id = self._start()
        for kind, content, capture in (
            ("chat", "Metin", CAPTURE_MANUAL),
            ("note", "Metin", "automatic"),
            ("note", " \n\t", CAPTURE_MANUAL),
            ("link", "javascript:alert(1)", CAPTURE_MANUAL),
        ):
            with self.subTest(kind=kind, content=content, capture=capture):
                with self.assertRaises(ValueError):
                    self.assistant.add_artifact(session_id, kind, content, capture_method=capture)
        self.assertEqual(self.repo.list_artifacts(session_id), [])

    def test_source_segment_and_manual_capture_are_forwarded_unchanged(self):
        session_id = self._start()
        segment_id = self._append_segment(session_id)
        artifact_id = self._note(
            session_id, "Kaynağa bağlı not", capture_method=CAPTURE_MANUAL,
            source_segment_id=segment_id,
        )
        artifact = self.repo.list_artifacts(session_id)[0]
        self.assertEqual(artifact.id, artifact_id)
        self.assertEqual(artifact.source_segment_id, segment_id)
        self.assertEqual(artifact.capture_method, CAPTURE_MANUAL)

    def test_cross_session_source_segment_is_not_reassigned(self):
        session_id = self._start()
        other_id = self.repo.create_session("Başka kaynak")
        segment_id = self._append_segment(other_id)
        with self.assertRaises(ValueError):
            self._note(session_id, source_segment_id=segment_id)
        self.assertEqual(self.repo.list_artifacts(session_id), [])
        self.assertEqual(self.repo.list_artifacts(other_id), [])
        self.assertEqual(self.repo.get_segments(other_id)[0].id, segment_id)

    def test_history_listing_and_deletion_remain_available_after_stop(self):
        session_id = self._start()
        artifact_id = self._note(session_id)
        self.assistant.stop()
        self.assertEqual([a.id for a in self.assistant.list_artifacts(session_id)], [artifact_id])
        self.assistant.delete_artifact(session_id, artifact_id)
        self.assertEqual(self.assistant.list_artifacts(session_id), [])
        self.assertEqual(self.repo.get_session(session_id).status, STATUS_COMPLETED)

    def test_history_deletion_does_not_delete_another_sessions_artifact(self):
        first_id = self._start()
        artifact_id = self._note(first_id)
        self.assistant.stop()
        second_id = self._start()
        self.assertFalse(self.assistant.delete_artifact(second_id, artifact_id))
        self.assertEqual([a.id for a in self.repo.list_artifacts(first_id)], [artifact_id])
        self.assertEqual(self.repo.list_artifacts(second_id), [])

    def test_external_finish_between_engine_check_and_insert_is_rejected(self):
        session_id = self._start()
        real_add = self.repo.add_artifact

        def finish_before_insert(*args, **kwargs):
            self.assertTrue(kwargs.get("require_active"))
            self.repo.finish_session(session_id)
            return real_add(*args, **kwargs)

        with patch.object(self.repo, "add_artifact", finish_before_insert):
            with self.assertRaises(RuntimeError):
                self._note(session_id)
        self.assertEqual(self.repo.list_artifacts(session_id), [])
        self.assertEqual(self.repo.get_session(session_id).status, STATUS_COMPLETED)

    def test_stop_waits_for_accepted_artifact_then_closes_creation_boundary(self):
        session_id = self._start()
        main_connection = self.repo.db.get_connection()
        insertion_entered = threading.Event()
        release_insertion = threading.Event()
        stop_attempted = threading.Event()
        stop_done = threading.Event()
        results = {}
        errors = []
        real_add = self.repo.add_artifact
        self.assistant._artifact_session_lock = _ObservedLock(
            self.assistant._artifact_session_lock, stop_attempted
        )

        def paused_add(*args, **kwargs):
            insertion_entered.set()
            if not release_insertion.wait(timeout=10):
                raise AssertionError("Test eser yazımını serbest bırakmadı.")
            results["worker_connection"] = self.repo.db.get_connection()
            return real_add(*args, **kwargs)

        def create_artifact():
            try:
                results["artifact_id"] = self._note(session_id, "Durdurmadan önce kabul edildi")
            except BaseException as exc:
                errors.append(exc)
            finally:
                self.repo.close_thread_connection()

        def stop_session():
            try:
                self.assistant.stop()
            except BaseException as exc:
                errors.append(exc)
            finally:
                self.repo.close_thread_connection()
                stop_done.set()

        creator = threading.Thread(target=create_artifact, name="artifact-create")
        stopper = threading.Thread(target=stop_session, name="artifact-stop")
        with patch.object(self.repo, "add_artifact", paused_add):
            try:
                creator.start()
                self.assertTrue(insertion_entered.wait(timeout=10))
                stopper.start()
                self.assertTrue(stop_attempted.wait(timeout=10))
                self.assertTrue(self.assistant._running)
                self.assertFalse(self.assistant.capture.stop_called)
                self.assertFalse(stop_done.is_set())
            finally:
                release_insertion.set()
                creator.join(timeout=10)
                if stopper.ident is not None:
                    stopper.join(timeout=10)
        self.assertFalse(creator.is_alive())
        self.assertFalse(stopper.is_alive())
        self.assertEqual(errors, [])
        self.assertIsNot(results["worker_connection"], main_connection)
        self.assertIs(self.repo.db.get_connection(), main_connection)
        self.assertEqual(main_connection.execute("SELECT 1").fetchone()[0], 1)
        self.assertEqual([a.id for a in self.repo.list_artifacts(session_id)], [results["artifact_id"]])
        self.assertEqual(self.repo.get_session(session_id).status, STATUS_COMPLETED)
        self.assertIsNone(self.assistant._artifact_session_id)
        with self.assertRaises(RuntimeError):
            self._note(session_id)

    def test_creation_is_rejected_as_soon_as_stop_reaches_capture_shutdown(self):
        session_id = self._start()
        capture_stop_entered = threading.Event()
        release_capture_stop = threading.Event()
        errors = []
        real_stop = self.assistant.capture.stop

        def paused_stop():
            capture_stop_entered.set()
            if not release_capture_stop.wait(timeout=10):
                raise AssertionError("Test yakalama kapanışını serbest bırakmadı.")
            return real_stop()

        def stop_session():
            try:
                self.assistant.stop()
            except BaseException as exc:
                errors.append(exc)
            finally:
                self.repo.close_thread_connection()

        stopper = threading.Thread(target=stop_session)
        with patch.object(self.assistant.capture, "stop", paused_stop):
            try:
                stopper.start()
                self.assertTrue(capture_stop_entered.wait(timeout=10))
                self.assertEqual(self.repo.get_session(session_id).status, STATUS_ACTIVE)
                self.assertFalse(self.assistant._running)
                self.assertIsNone(self.assistant._artifact_session_id)
                with self.assertRaises(RuntimeError):
                    self._note(session_id)
                self.assertEqual(self.repo.list_artifacts(session_id), [])
            finally:
                release_capture_stop.set()
                stopper.join(timeout=10)
        self.assertFalse(stopper.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.repo.get_session(session_id).status, STATUS_COMPLETED)


if __name__ == "__main__":
    unittest.main()
