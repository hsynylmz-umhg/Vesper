"""UI süreç sahipliğini sahte NiceGUI/donanım ve geçici depoyla doğrular.

Gerçek sunucu, tarayıcı, CUDA, WASAPI veya Ollama başlatılmaz.
"""

import asyncio
from concurrent.futures import Future
import importlib.util
import os
from pathlib import Path
import runpy
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

from tests import test_lifecycle_integration as lifecycle
from tests.test_artifact_ui import FakeApp, FakeUi
from vesper.repository import SessionRepository
from vesper.storage import Database


class TestUiStartup(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        previous_directory = os.getcwd()
        os.chdir(root)
        self.addCleanup(os.chdir, previous_directory)
        self.db = Database(root / "startup.db")
        self.addCleanup(self.db.close)
        self.repo = SessionRepository(self.db)
        self.assistant = None
        self.assistants = []
        self.releases = []
        self.expected_stop_failure = False
        for key, value in (("DB_PATH", str(root / "startup.db")),
                           ("OUTPUT_DIR", str(root / "outputs"))):
            current_patch = patch.object(lifecycle._engine_module.config, key, value)
            current_patch.start()
            self.addCleanup(current_patch.stop)
        self.addCleanup(self.finish_workers)
        self.fake = FakeUi()
        self.app = FakeApp()
        self.nicegui = types.ModuleType("nicegui")
        self.nicegui.ui = self.fake
        self.nicegui.app = self.app
        self.engine = types.ModuleType("vesper.engine")
        self.engine.WebinarAssistant = self.make_assistant
        self.source = Path(__file__).resolve().parents[1] / "ui.py"

    def make_assistant(self):
        self.assistant = lifecycle._engine_module.WebinarAssistant(repository=self.repo)
        self.assistants.append(self.assistant)
        return self.assistant

    def load_page(self, name="startup_page"):
        spec = importlib.util.spec_from_file_location(name, self.source)
        page = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"nicegui": self.nicegui, "vesper.engine": self.engine}):
            spec.loader.exec_module(page)
        return page

    def finish_workers(self):
        for event in self.releases:
            event.set()
        state = getattr(self.app.state, "_vesper_assistant_lifecycle", None)
        if state is not None:
            try:
                state.wait_for_stop(timeout=5)
            except BaseException:
                if not self.expected_stop_failure:
                    raise
            finally:
                if state.stop_worker is not None:
                    state.stop_worker.join(timeout=5)
                    self.assertFalse(state.stop_worker.is_alive())
        for assistant in self.assistants:
            if assistant._running:
                assistant.stop()
            for worker in assistant._threads:
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive())

    def blocking_stop(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = self.assistant.stop

        def blocked():
            entered.set()
            if not release.wait(timeout=5):
                raise TimeoutError("Test kapanış serbest bırakmasını alamadı.")
            return original()

        return entered, release, blocked

    def test_repeated_page_execution_constructs_one_assistant_and_recovers_once(self):
        with patch.object(self.repo, "recover_interrupted_sessions", wraps=self.repo.recover_interrupted_sessions) as recover:
            first = self.load_page()
            second = self.load_page("startup_page_second")
        self.assertIs(first.assistant, second.assistant)
        self.assertEqual(len(self.assistants), 1)
        self.assertEqual(recover.call_count, 1)
        self.assertEqual(len(self.app.shutdown_handlers), 1)

    def test_runpy_page_reexecution_preserves_active_session_and_configuration(self):
        first = self.load_page()
        first.on_start()
        sid = self.assistant.session_id
        self.repo.add_artifact(sid, "note", "Yeniden yürütmede korunur")
        with patch.dict(sys.modules, {"nicegui": self.nicegui, "vesper.engine": self.engine}):
            page = runpy.run_path(str(self.source), run_name="__main__")
        self.assertIs(page["assistant"], first.assistant)
        self.assertEqual(len(self.assistants), 1)
        self.assertEqual(self.repo.get_session(sid).status, "active")
        self.assertEqual(self.repo.list_artifacts(sid)[0].content, "Yeniden yürütmede korunur")
        self.assertEqual(len(self.app.shutdown_handlers), 1)

    def test_changed_database_configuration_is_rejected_before_new_assistant(self):
        page = self.load_page()
        with patch.object(lifecycle._engine_module.config, "DB_PATH", str(Path(self.temp.name) / "other.db")):
            with self.assertRaisesRegex(RuntimeError, "veri yolları"):
                self.load_page("wrong_database_page")
        self.assertEqual(len(self.assistants), 1)
        self.assertIs(page.assistant, self.assistant)
        self.assertFalse((Path(self.temp.name) / "other.db").exists())

    def test_changed_output_configuration_is_rejected(self):
        self.load_page()
        with patch.object(lifecycle._engine_module.config, "OUTPUT_DIR", str(Path(self.temp.name) / "other_outputs")):
            with self.assertRaisesRegex(RuntimeError, "veri yolları"):
                self.load_page("wrong_output_page")
        self.assertEqual(len(self.assistants), 1)

    def test_idle_shutdown_is_idempotent_and_never_calls_engine_stop(self):
        page = self.load_page()
        with patch.object(self.assistant, "stop", wraps=self.assistant.stop) as stop:
            self.app.shutdown_handlers[0]()
            self.app.shutdown_handlers[0]()
        self.assertEqual(stop.call_count, 0)
        self.assertTrue(page._assistant_lifecycle.snapshot()["closing"])
        self.assertFalse(page._assistant_lifecycle.snapshot()["stop_pending"])
        self.assertIsNone(page._assistant_lifecycle.stop_worker)
        page.on_start()
        self.assertFalse(self.assistant._running)
        self.assertEqual(self.fake.notifications[-1][1], "negative")

    def test_active_shutdown_returns_with_pending_worker_and_finalizes_once(self):
        page = self.load_page()
        page.on_start()
        sid = self.assistant.session_id
        entered, release, blocked = self.blocking_stop()
        with patch.object(self.assistant, "stop", side_effect=blocked) as stop:
            self.app.shutdown_handlers[0]()
            self.assertTrue(entered.wait(timeout=1))
            self.app.shutdown_handlers[0]()
            snapshot = page._assistant_lifecycle.snapshot()
            self.assertTrue(snapshot["stop_pending"])
            self.assertTrue(snapshot["stop_worker_alive"])
            self.assertFalse(page._assistant_lifecycle.stop_worker.daemon)
            self.assertIsNone(snapshot["last_stop_finalized"])
            self.assertEqual(self.repo.get_session(sid).status, "active")
            with self.assertRaises(TimeoutError):
                page._assistant_lifecycle.wait_for_stop(timeout=0.01)
            release.set()
            final = page._assistant_lifecycle.wait_for_stop(timeout=5)
        self.assertEqual(stop.call_count, 1)
        self.assertFalse(final["stop_pending"])
        self.assertFalse(final["stop_worker_alive"])
        self.assertTrue(final["last_stop_finalized"])
        self.assertEqual(self.repo.get_session(sid).status, "completed")

    def test_normal_stop_and_shutdown_share_the_same_worker(self):
        page = self.load_page()
        page.on_start()
        entered, release, blocked = self.blocking_stop()

        async def exercise():
            stopping = asyncio.create_task(page.on_stop())
            while not entered.is_set():
                await asyncio.sleep(0.001)
            future = page._assistant_lifecycle.stop_future
            self.app.shutdown_handlers[0]()
            self.assertIs(page._assistant_lifecycle.stop_future, future)
            self.assertTrue(page._assistant_lifecycle.snapshot()["closing"])
            release.set()
            await stopping

        with patch.object(self.assistant, "stop", side_effect=blocked) as stop:
            asyncio.run(exercise())
            page._assistant_lifecycle.wait_for_stop(timeout=5)
        self.assertEqual(stop.call_count, 1)
        self.assertTrue(self.assistant.last_stop_finalized)

    def test_cancelled_page_wait_does_not_cancel_finalization_or_duplicate_shutdown(self):
        page = self.load_page()
        page.on_start()
        entered, release, blocked = self.blocking_stop()

        async def exercise():
            stopping = asyncio.create_task(page.on_stop())
            while not entered.is_set():
                await asyncio.sleep(0.001)
            future = page._assistant_lifecycle.stop_future
            stopping.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await stopping
            self.assertFalse(future.cancelled())
            self.app.shutdown_handlers[0]()
            self.assertIs(page._assistant_lifecycle.stop_future, future)
            release.set()
            await asyncio.shield(asyncio.wrap_future(future))

        with patch.object(self.assistant, "stop", side_effect=blocked) as stop:
            asyncio.run(exercise())
            page._assistant_lifecycle.wait_for_stop(timeout=5)
        self.assertEqual(stop.call_count, 1)
        self.assertTrue(self.assistant.last_stop_finalized)

    def test_old_and_new_pages_reject_start_after_shutdown(self):
        first = self.load_page()
        self.app.shutdown_handlers[0]()
        second = self.load_page("second_page")
        first.on_start()
        second.on_start()
        self.assertIs(first.assistant, second.assistant)
        self.assertEqual(len(self.assistants), 1)
        self.assertEqual(self.repo.list_sessions(), [])
        self.assertEqual([kind for _, kind in self.fake.notifications[-2:]], ["negative", "negative"])

    def test_new_page_cannot_start_while_normal_final_summary_is_pending(self):
        first = self.load_page()
        first.on_start()
        sid = self.assistant.session_id
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = self.assistant.summarizer.summarize

        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(timeout=5):
                raise TimeoutError("Test final özet serbest bırakmasını alamadı.")
            return original(*args, **kwargs)

        with patch.object(self.assistant.summarizer, "summarize", side_effect=blocked):
            future = first._request_stop(first._assistant_lifecycle)
            self.assertTrue(entered.wait(timeout=2))
            self.assertFalse(self.assistant._running)
            second = self.load_page("pending_summary_page")
            second.on_start()
            self.assertEqual(self.assistant.session_id, sid)
            self.assertEqual(len(self.repo.list_sessions()), 1)
            self.assertEqual(self.repo.get_session(sid).status, "active")
            self.assertFalse(future.done())
            self.assertEqual(self.fake.notifications[-1][1], "negative")
            release.set()
            first._assistant_lifecycle.wait_for_stop(timeout=5)
        self.assertEqual(self.repo.get_session(sid).status, "completed")

    def test_shutdown_closes_connections_only_on_their_own_threads(self):
        page = self.load_page()
        page.on_start()
        owner_ids = []
        original = self.repo.close_thread_connection

        def close():
            owner_ids.append(threading.get_ident())
            original()

        main_thread = threading.get_ident()
        with patch.object(self.repo, "close_thread_connection", side_effect=close):
            self.app.shutdown_handlers[0]()
            page._assistant_lifecycle.wait_for_stop(timeout=5)
        self.assertIn(main_thread, owner_ids)
        self.assertIn(page._assistant_lifecycle.stop_worker.ident, owner_ids)
        self.assertFalse(page._assistant_lifecycle.stop_worker.is_alive())

    def test_old_material_form_and_reexecuted_page_reject_pending_shutdown(self):
        page = self.load_page()
        page.on_start()
        sid = self.assistant.session_id
        children = page.live_materials_editor.descendants()
        field = next(element for element in children if element.kind == "textarea")
        add = next(element for element in children if element.kind == "button" and element.text == "Ekle")
        field.set_value("Kapanış sırasında eklenmemeli")
        entered, release, blocked = self.blocking_stop()
        with patch.object(self.assistant, "stop", side_effect=blocked):
            self.app.shutdown_handlers[0]()
            self.assertTrue(entered.wait(timeout=1))
            self.assertTrue(self.assistant._running)
            add.click()
            second = self.load_page("shutdown_material_page")
            self.assertEqual(self.repo.list_artifacts(sid), [])
            self.assertEqual(self.fake.notifications[-1][1], "negative")
            self.assertFalse(any(element.kind == "textarea" for element in second.live_materials_editor.descendants()))
            release.set()
            page._assistant_lifecycle.wait_for_stop(timeout=5)


    def prepare_worker_case(self):
        """Ses işçisi açmadan, geçici oturuma sahip UI kapanışını kurar."""
        page = self.load_page()
        self.assistant.session_id = self.repo.create_session("Kapanış arızası testi")
        self.assistant._running = True
        return page

    def test_worker_exception_settles_future_and_reports_failure_without_hanging(self):
        page = self.prepare_worker_case()
        error = RuntimeError("finalization failed")
        self.expected_stop_failure = True

        def fail():
            self.assistant._running = False
            raise error

        async def exercise():
            await asyncio.wait_for(page.on_stop(), timeout=2)

        with patch.object(self.assistant, "stop", side_effect=fail) as stop:
            asyncio.run(exercise())
            page._assistant_lifecycle.stop_worker.join(timeout=2)
        self.assertIs(page._assistant_lifecycle.stop_future.exception(), error)
        self.assertEqual(stop.call_count, 1)
        self.assertFalse(self.assistant.last_stop_finalized)
        self.assertEqual(self.fake.notifications[-1][1], "negative")
        self.assertTrue(page.start_button.enabled)
        self.assertEqual(self.repo.get_session(self.assistant.session_id).status, "active")
        self.assertFalse(any(kind == "positive" for _, kind in self.fake.notifications))

    def test_logging_failure_cannot_replace_primary_failure_or_leave_future_pending(self):
        page = self.prepare_worker_case()
        primary = ValueError("original finalization failure")
        self.expected_stop_failure = True

        def fail():
            self.assistant._running = False
            raise primary

        with patch.object(self.assistant, "stop", side_effect=fail), \
                patch.object(page.log, "log", side_effect=OSError("logger failed")):
            asyncio.run(asyncio.wait_for(page.on_stop(), timeout=2))
            page._assistant_lifecycle.stop_worker.join(timeout=2)
        state = page._assistant_lifecycle
        self.assertTrue(state.stop_future.done())
        self.assertIs(state.stop_future.exception(), primary)
        self.assertGreaterEqual(len(state.logging_errors), 2)
        self.assertEqual(self.fake.notifications[-1][1], "negative")

    def test_successful_finalization_settles_before_status_logging_failure(self):
        page = self.prepare_worker_case()

        def succeed():
            self.assistant._running = False
            self.assistant.last_stop_drained = self.assistant.last_stop_finalized = True
            return "Tamamlanan özet"

        with patch.object(self.assistant, "stop", side_effect=succeed), \
                patch.object(page.log, "log", side_effect=OSError("status log unavailable")):
            future = page._request_stop(page._assistant_lifecycle)
            page._assistant_lifecycle.stop_worker.join(timeout=2)
        self.assertEqual(future.result(timeout=1), "Tamamlanan özet")
        self.assertEqual(len(page._assistant_lifecycle.logging_errors), 1)
        self.assertIsNone(page._assistant_lifecycle.stop_failure)

    def test_worker_cleanup_failure_is_retained_without_masking_primary(self):
        page = self.prepare_worker_case()
        primary, cleanup = RuntimeError("primary"), OSError("cleanup")
        self.expected_stop_failure = True

        def fail():
            self.assistant._running = False
            raise primary

        with patch.object(self.assistant, "stop", side_effect=fail), \
                patch.object(self.repo, "close_thread_connection", side_effect=cleanup), \
                patch.object(page.log, "log", side_effect=RuntimeError("logging")):
            future = page._request_stop(page._assistant_lifecycle)
            page._assistant_lifecycle.stop_worker.join(timeout=2)
        self.assertIs(future.exception(timeout=1), primary)
        self.assertIs(page._assistant_lifecycle.stop_cleanup_failure, cleanup)

    def test_cleanup_only_failure_settles_as_failure(self):
        page = self.prepare_worker_case()
        self.expected_stop_failure = True
        cleanup = OSError("connection cleanup failed")

        def succeed():
            self.assistant._running = False
            self.assistant.last_stop_drained = self.assistant.last_stop_finalized = True
            return "Final özet"

        with patch.object(self.assistant, "stop", side_effect=succeed), \
                patch.object(self.repo, "close_thread_connection", side_effect=cleanup):
            future = page._request_stop(page._assistant_lifecycle)
            page._assistant_lifecycle.stop_worker.join(timeout=2)
        self.assertIs(future.exception(timeout=1), cleanup)

    def test_unconfirmed_return_is_not_success(self):
        page = self.prepare_worker_case()
        self.expected_stop_failure = True

        def incomplete():
            self.assistant._running = False
            self.assistant.last_stop_drained = True
            self.assistant.last_stop_finalized = False
            return "Önceki canlı özet"

        with patch.object(self.assistant, "stop", side_effect=incomplete):
            asyncio.run(asyncio.wait_for(page.on_stop(), timeout=2))
            page._assistant_lifecycle.stop_worker.join(timeout=2)
        self.assertIsInstance(page._assistant_lifecycle.stop_future.exception(), RuntimeError)
        self.assertEqual(self.fake.notifications[-1][1], "negative")
        self.assertFalse(any(kind == "positive" for _, kind in self.fake.notifications))

    def test_future_settles_exactly_once_on_success_and_failure(self):
        page = self.prepare_worker_case()

        class CountedFuture(Future):
            def __init__(self):
                super().__init__()
                self.settlements = 0

            def set_result(self, result):
                self.settlements += 1
                return super().set_result(result)

            def set_exception(self, exception):
                self.settlements += 1
                return super().set_exception(exception)

        for fail in (False, True):
            with self.subTest(fail=fail):
                self.assistant.session_id = self.repo.create_session("Ayrı kapanış denemesi")
                self.assistant._running = True
                self.expected_stop_failure = fail

                def stop():
                    self.assistant._running = False
                    if fail:
                        raise RuntimeError("worker failed")
                    self.assistant.last_stop_drained = self.assistant.last_stop_finalized = True
                    return "Özet"

                with patch.object(page, "Future", CountedFuture), patch.object(self.assistant, "stop", side_effect=stop):
                    future = page._request_stop(page._assistant_lifecycle)
                    page._assistant_lifecycle.stop_worker.join(timeout=2)
                    self.assertIs(page._request_stop(page._assistant_lifecycle), future)
                    self.app.shutdown_handlers[0]()
                self.assertTrue(future.done())
                self.assertEqual(future.settlements, 1)

    def test_failed_stop_is_not_automatically_retried_even_if_running_flag_remains_true(self):
        page = self.prepare_worker_case()
        self.expected_stop_failure = True
        try:
            with patch.object(self.assistant, "stop", side_effect=RuntimeError("early failure")) as stop:
                future = page._request_stop(page._assistant_lifecycle)
                page._assistant_lifecycle.stop_worker.join(timeout=2)
                self.assertIs(page._request_stop(page._assistant_lifecycle), future)
                self.app.shutdown_handlers[0]()
                self.assertIs(page._assistant_lifecycle.stop_future, future)
                self.assertEqual(stop.call_count, 1)
        finally:
            self.assistant._running = False

    def test_interrupt_in_worker_is_preserved_in_settled_future(self):
        page = self.prepare_worker_case()
        self.expected_stop_failure = True
        interrupt = KeyboardInterrupt("worker interrupt")

        def fail():
            self.assistant._running = False
            raise interrupt

        with patch.object(self.assistant, "stop", side_effect=fail):
            future = page._request_stop(page._assistant_lifecycle)
            page._assistant_lifecycle.stop_worker.join(timeout=2)
        self.assertIs(future.exception(timeout=1), interrupt)

    def test_thread_start_failure_settles_future_and_is_not_retried(self):
        page = self.prepare_worker_case()
        self.expected_stop_failure = True
        failure = RuntimeError("thread start failed")
        try:
            with patch.object(page.threading.Thread, "start", side_effect=failure) as start:
                with self.assertRaises(RuntimeError):
                    page._request_stop(page._assistant_lifecycle)
                future = page._assistant_lifecycle.stop_future
                self.assertIs(future.exception(timeout=1), failure)
                self.assertIs(page._request_stop(page._assistant_lifecycle), future)
                self.assertEqual(start.call_count, 1)
        finally:
            self.assistant._running = False

    def test_running_stop_future_cannot_be_cancelled_externally(self):
        page = self.load_page()
        page.on_start()
        entered, release, blocked = self.blocking_stop()
        with patch.object(self.assistant, "stop", side_effect=blocked):
            future = page._request_stop(page._assistant_lifecycle)
            self.assertTrue(entered.wait(timeout=1))
            self.assertFalse(future.cancel())
            release.set()
            page._assistant_lifecycle.wait_for_stop(timeout=5)
        self.assertFalse(future.cancelled())

    def test_shutdown_logging_failure_does_not_block_connection_closure(self):
        page = self.load_page()
        page.on_start()
        entered, release, blocked = self.blocking_stop()
        with patch.object(self.assistant, "stop", side_effect=blocked), \
                patch.object(self.repo, "close_thread_connection", wraps=self.repo.close_thread_connection) as close, \
                patch.object(page.log, "log", side_effect=OSError("logging unavailable")):
            self.app.shutdown_handlers[0]()
            self.assertTrue(entered.wait(timeout=1))
            self.assertGreaterEqual(close.call_count, 1)
            self.app.shutdown_handlers[0]()
            release.set()
            page._assistant_lifecycle.wait_for_stop(timeout=5)
        self.assertTrue(page._assistant_lifecycle.logging_errors)

    def test_cancellation_never_refreshes_widgets_in_deleted_slot(self):
        page = self.load_page()
        page.on_start()
        entered, release, blocked = self.blocking_stop()

        async def exercise():
            task = asyncio.create_task(page.on_stop())
            while not entered.is_set():
                await asyncio.sleep(0.001)
            page.stop_button.is_deleted = True
            with patch.object(page, "refresh_live_materials", side_effect=RuntimeError("deleted slot")) as live, \
                    patch.object(page, "refresh_history_materials", side_effect=RuntimeError("deleted slot")) as history:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                live.assert_not_called()
                history.assert_not_called()
            release.set()
            await asyncio.shield(asyncio.wrap_future(page._assistant_lifecycle.stop_future))

        with patch.object(self.assistant, "stop", side_effect=blocked) as stop:
            asyncio.run(exercise())
            page._assistant_lifecycle.wait_for_stop(timeout=5)
        self.assertEqual(stop.call_count, 1)
        self.assertTrue(self.assistant.last_stop_finalized)

    def test_completed_stop_does_not_update_deleted_page(self):
        page = self.load_page()
        page.on_start()
        entered, release, blocked = self.blocking_stop()

        async def exercise():
            task = asyncio.create_task(page.on_stop())
            while not entered.is_set():
                await asyncio.sleep(0.001)
            page.stop_button.is_deleted = True
            with patch.object(page, "refresh_live_materials", side_effect=RuntimeError("deleted slot")) as refresh:
                release.set()
                await asyncio.wait_for(task, timeout=3)
                refresh.assert_not_called()

        with patch.object(self.assistant, "stop", side_effect=blocked):
            asyncio.run(exercise())
            page._assistant_lifecycle.wait_for_stop(timeout=5)

    def test_cancelled_page_observes_late_worker_failure_without_orphan_warning(self):
        page = self.prepare_worker_case()
        self.expected_stop_failure = True
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        primary = RuntimeError("late finalization failure")
        wrappers = []
        original_wrap = asyncio.wrap_future

        def wrap(*args, **kwargs):
            future = original_wrap(*args, **kwargs)
            wrappers.append(future)
            return future

        def fail():
            entered.set()
            release.wait(timeout=3)
            self.assistant._running = False
            raise primary

        async def exercise():
            task = asyncio.create_task(page.on_stop())
            while not entered.is_set():
                await asyncio.sleep(0.001)
            page.stop_button.is_deleted = True
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            release.set()
            while not page._assistant_lifecycle.stop_future.done():
                await asyncio.sleep(0.001)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertEqual(len(wrappers), 1)
            self.assertFalse(wrappers[0]._log_traceback)
            self.assertIs(page._assistant_lifecycle.stop_future.exception(), primary)

        with patch.object(self.assistant, "stop", side_effect=fail), patch.object(asyncio, "wrap_future", side_effect=wrap):
            asyncio.run(asyncio.wait_for(exercise(), timeout=4))
            page._assistant_lifecycle.stop_worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
