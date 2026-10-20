"""Başlatıcı yardımcılarını geçici verilerle sınar; CLI veya gerçek UI çalıştırılmaz."""

import builtins
from concurrent.futures import Future
from contextlib import closing
import hashlib
from importlib import metadata
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import sqlite3
import stat
import signal
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

from vesper.repository import SessionRepository
from vesper.storage import Database


TOOLING_MANIFEST = Path(__file__).resolve().parents[1] / "release-tooling.json"


def resolve_launcher(manifest_path=TOOLING_MANIFEST, override=None):
    """Sürüm kanıtındaki aynı baytları farklı konumdan da kabul eder; eksiklikte açıkça durur."""
    record = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if record.get("format") != "vesper-release-tooling-v1":
        raise RuntimeError("Unsupported release tooling manifest")
    selected = override if override is not None else os.environ.get("VESPER_ACCEPTANCE_LAUNCHER")
    path = Path(selected or record["launcher"]["path"])
    if not path.is_absolute() or not path.is_file():
        raise RuntimeError("Provision the reviewed external launcher; see release-tooling.json")
    if hashlib.sha256(path.read_bytes()).hexdigest() != record["launcher"]["sha256"]:
        raise RuntimeError("External launcher SHA-256 mismatch; execution refused")
    return path


LAUNCHER = resolve_launcher()
spec = importlib.util.spec_from_file_location("acceptance_tooling_under_test", LAUNCHER)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


class TestAcceptanceLauncher(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "cases"
        self.case = tool.prepare_case(self.root / "sample", self.root)

    def test_prepare_is_exclusive_and_does_not_create_a_database(self):
        self.assertFalse((self.case / "database" / "acceptance.sqlite").exists())
        with self.assertRaises(FileExistsError):
            tool.prepare_case(self.case, self.root)
        self.assertEqual(tool.load_case(self.case, self.root), self.case)

    def test_wrong_case_paths_are_rejected_before_metadata_inspection(self):
        for path in (Path(self.temp.name) / "elsewhere", self.case / "nested", Path("relative")):
            with self.subTest(path=path), patch.object(tool, "no_reparse") as inspect:
                with self.assertRaises(tool.AcceptanceError):
                    tool.case_path(path, self.root)
                inspect.assert_not_called()

    def test_reparse_ancestor_is_rejected(self):
        info = types.SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        with patch.object(Path, "lstat", return_value=info):
            with self.assertRaisesRegex(tool.AcceptanceError, "Reparse"):
                tool.case_path(self.case, self.root)

    def test_tampered_ownership_is_rejected(self):
        marker = self.case / "ownership.json"
        content = json.loads(marker.read_text(encoding="utf-8"))
        content["case_root"] = str(self.root / "other")
        marker.write_text(json.dumps(content), encoding="utf-8")
        with self.assertRaisesRegex(tool.AcceptanceError, "ownership"):
            tool.load_case(self.case, self.root)

    def test_child_reparse_is_rejected_before_ownership_file_is_opened(self):
        original = tool.no_reparse

        def inspect(path):
            if Path(path).name == "ownership.json":
                raise tool.AcceptanceError("redirected child")
            return original(path)

        with patch.object(tool, "no_reparse", side_effect=inspect), patch.object(builtins, "open") as opened:
            with self.assertRaisesRegex(tool.AcceptanceError, "redirected"):
                tool.load_case(self.case, self.root)
            opened.assert_not_called()

    def test_restart_reuses_database_path_and_allocates_new_run_outputs(self):
        first = tool.allocate_run(self.case)
        second = tool.allocate_run(self.case)
        self.assertNotEqual(first, second)
        configs = [types.SimpleNamespace(), types.SimpleNamespace()]
        with patch.dict(sys.modules):
            sys.modules.pop("vesper.engine", None)
            for run, config in zip((first, second), configs):
                tool.configure_paths(self.case, run, config)
        self.assertEqual(configs[0].DB_PATH, configs[1].DB_PATH)
        self.assertNotEqual(configs[0].OUTPUT_DIR, configs[1].OUTPUT_DIR)
        for run in (first, second):
            for name in ("outputs", "temp", "nicegui-storage", "logs"):
                self.assertTrue((run / name).is_dir())

    def test_configuration_rejects_already_imported_engine(self):
        with patch.dict(sys.modules, {"vesper.engine": types.ModuleType("vesper.engine")}):
            with self.assertRaisesRegex(tool.AcceptanceError, "before disposable"):
                tool.configure_paths(self.case, self.case, types.SimpleNamespace())

    def test_engine_path_and_migration_authorization_are_both_verified(self):
        expected = self.case / "database" / "acceptance.sqlite"
        engine = types.SimpleNamespace(_default_database=Database(expected))
        tool.check_engine_database(engine, self.case)
        engine._default_database = Database(expected, allow_migration=True)
        with self.assertRaisesRegex(tool.AcceptanceError, "authorization"):
            tool.check_engine_database(engine, self.case)
        engine._default_database = Database(self.case / "wrong.sqlite")
        with self.assertRaisesRegex(tool.AcceptanceError, "path mismatch"):
            tool.check_engine_database(engine, self.case)

    def test_launcher_does_not_report_success_when_stop_future_failed_after_engine_completion(self):
        assistant = types.SimpleNamespace(session_id="disposable", last_stop_drained=True, last_stop_finalized=True)
        state = types.SimpleNamespace(assistant=assistant, stop_failure=None)
        self.assertTrue(tool._finalization_confirmed(state))
        state.stop_failure = OSError("cleanup failed after commit")
        self.assertFalse(tool._finalization_confirmed(state))

    def test_launcher_finalization_confirmation_requires_engine_evidence(self):
        assistant = types.SimpleNamespace(session_id="disposable", last_stop_drained=True, last_stop_finalized=None)
        state = types.SimpleNamespace(assistant=assistant, stop_failure=None)
        self.assertFalse(tool._finalization_confirmed(state))
        self.assertFalse(tool._finalization_confirmed(None))
        assistant.session_id = None
        self.assertTrue(tool._finalization_confirmed(state))

    def test_sqlite_guard_accepts_only_owned_database_and_memory(self):
        guard = tool.PathGuard(self.case, self.case / "outputs")
        for target in (":memory:", str(guard.database), guard.readonly_uri):
            guard("sqlite3.connect", (target,))
        for target in ("relative.sqlite", str(self.case / "other.sqlite"),
                       guard.database.as_uri(), guard.readonly_uri + "&immutable=1", "file::memory:"):
            with self.subTest(target=target), patch.object(tool, "no_reparse") as inspect:
                with self.assertRaisesRegex(tool.AcceptanceError, "SQLite"):
                    guard("sqlite3.connect", (target,))
                inspect.assert_not_called()

    def test_real_audit_hook_blocks_wrong_sqlite_target_in_disposable_child(self):
        code = '''
import importlib.util, pathlib, sqlite3, sys
spec = importlib.util.spec_from_file_location("isolated_acceptance", sys.argv[1])
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)
case = pathlib.Path(sys.argv[2])
guard = tool.PathGuard(case, case / "outputs")
sys.addaudithook(guard)
sqlite3.connect = tool.guarded_connect(sqlite3.connect, guard)
conn = sqlite3.connect(str(guard.database))
conn.execute("CREATE TABLE proof (id INTEGER)")
try:
    conn.execute("ATTACH DATABASE ? AS escaped", (str(case / "attached.sqlite"),))
except sqlite3.DatabaseError:
    pass
else:
    raise AssertionError("unexpected database was attached")
conn.close()
wrong = case / "wrong.sqlite"
try:
    sqlite3.connect(str(wrong))
except tool.AcceptanceError:
    pass
else:
    raise AssertionError("unexpected database was opened")
guard.enabled = False
assert not wrong.exists()
assert not (case / "attached.sqlite").exists()
'''
        result = subprocess.run([sys.executable, "-B", "-c", code, str(LAUNCHER), str(self.case)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.case / "database" / "acceptance.sqlite").is_file())
        self.assertFalse((self.case / "wrong.sqlite").exists())

    def test_guard_rejects_outside_writes_and_repository_data_reads(self):
        guard = tool.PathGuard(self.case, self.case / "outputs")
        with self.assertRaises(tool.AcceptanceError):
            guard("open", (str(Path(self.temp.name) / "outside.txt"), "w", os.O_WRONLY | os.O_CREAT))
        # Yol yalnızca metin olarak karşılaştırılır; hedef dosyaya dokunulmaz.
        with patch.object(tool, "no_reparse") as inspect:
            with self.assertRaisesRegex(tool.AcceptanceError, "Production"):
                guard("open", (str(tool.PROJECT / "data" / "never-open.sqlite"), "r", 0))
            inspect.assert_not_called()

    def test_connection_guard_keeps_primary_error_when_cleanup_also_fails(self):
        guard = tool.PathGuard(self.case, self.case / "outputs")
        primary = RuntimeError("authorizer failed")
        conn = Mock()
        conn.set_authorizer.side_effect = primary
        conn.close.side_effect = OSError("close failed")
        connect = tool.guarded_connect(Mock(return_value=conn), guard)
        with self.assertRaises(RuntimeError) as captured:
            connect(":memory:")
        self.assertIs(captured.exception, primary)
        self.assertIn("close failed", primary.__notes__[0])

    def test_output_creation_is_atomic_and_cannot_overwrite(self):
        outputs = self.case / "outputs"
        outputs.mkdir()
        target = outputs / "meeting.md"
        opener = tool.exclusive_output_open(builtins.open, outputs)
        with opener(target, "w", encoding="utf-8") as stream:
            stream.write("original")
        with self.assertRaises(FileExistsError):
            opener(target, "w", encoding="utf-8")
        with self.assertRaises(tool.AcceptanceError):
            opener(target, "a", encoding="utf-8")
        self.assertEqual(target.read_text(encoding="utf-8"), "original")
        guard = tool.PathGuard(self.case, outputs)
        with self.assertRaisesRegex(tool.AcceptanceError, "exclusive"):
            guard("open", (str(target), "w", os.O_WRONLY | os.O_CREAT))

    def test_io_open_wrapper_also_uses_exclusive_creation(self):
        outputs = self.case / "outputs"
        outputs.mkdir()
        target = outputs / "meeting.md"
        opener = tool.exclusive_output_open(io.open, outputs)
        with opener(target, "w", encoding="utf-8") as stream:
            stream.write("saved")
        with self.assertRaises(FileExistsError):
            opener(target, "w", encoding="utf-8")

    def test_ui_wrapper_forces_loopback_and_registers_once_across_page_execution(self):
        engine = types.SimpleNamespace(_default_database=Database(self.case / "database" / "acceptance.sqlite"))
        calls, registrations = [], []
        wrapped = tool.ui_run_wrapper(lambda **kwargs: calls.append(kwargs), engine, self.case, 8099,
                                      lambda: registrations.append(True))
        for _ in range(2):
            wrapped(host="0.0.0.0", port=8080, reload=False)
        self.assertEqual(registrations, [True])
        self.assertEqual(calls, [{"host": "127.0.0.1", "port": 8099, "reload": False}] * 2)
        with self.assertRaisesRegex(tool.AcceptanceError, "reload"):
            wrapped(reload=True)

    def test_ui_run_wrapper_rechecks_database_before_every_page(self):
        engine = types.SimpleNamespace(_default_database=Database(self.case / "wrong.sqlite"))
        called = []
        wrapped = tool.ui_run_wrapper(lambda **kwargs: called.append(True), engine, self.case, 8080)
        with self.assertRaises(tool.AcceptanceError):
            wrapped(reload=False)
        self.assertEqual(called, [])

    def test_launch_seam_selects_ui_script_without_executing_it(self):
        source = Path(__file__).resolve().parents[1] / "ui.py"
        with patch.object(tool.runpy, "run_path", return_value={}) as run:
            tool.launch_ui(source)
        run.assert_called_once_with(str(source), run_name="__main__")

    def test_model_snapshot_uses_local_files_only(self):
        model = self.case / "model"
        model.mkdir()
        for name in (*tool._REQUIRED_MODEL_FILES, "vocabulary.json"):
            (model / name).write_text("test", encoding="utf-8")
        self.assertEqual(tool.local_model_snapshot(str(model)), model)
        (model / "tokenizer.json").unlink()
        with self.assertRaisesRegex(tool.AcceptanceError, "incomplete"):
            tool.local_model_snapshot(str(model))

    def test_missing_cache_never_calls_download(self):
        fake = types.ModuleType("huggingface_hub")
        fake.try_to_load_from_cache = lambda *args, **kwargs: None
        with patch.dict(sys.modules, {"huggingface_hub": fake}):
            with self.assertRaisesRegex(tool.AcceptanceError, "downloads are forbidden"):
                tool.local_model_snapshot("unavailable/model")

    def test_existing_cached_snapshot_is_resolved_without_downloader(self):
        model = self.case / "snapshot"
        model.mkdir()
        for name in tool._REQUIRED_MODEL_FILES:
            (model / name).write_text("test", encoding="utf-8")
        fake = types.ModuleType("huggingface_hub")
        fake.try_to_load_from_cache = lambda *args, **kwargs: str(model / "model.bin")
        with patch.dict(sys.modules, {"huggingface_hub": fake}):
            self.assertEqual(tool.local_model_snapshot("already/cached"), model)

    def test_active_case_lock_blocks_verify_and_cleanup(self):
        with tool.CaseLock(self.case):
            for action in (tool.verify_case, tool.cleanup_case):
                with self.subTest(action=action), self.assertRaisesRegex(tool.AcceptanceError, "active"):
                    action(self.case, self.root)
        self.assertTrue(self.case.exists())

    def make_database(self, completed=True):
        db = Database(self.case / "database" / "acceptance.sqlite")
        self.addCleanup(db.close)
        repo = SessionRepository(db)
        sid = repo.create_session("Disposable acceptance")
        repo.add_artifact(sid, "note", "Persistent note")
        if completed:
            repo.finish_session(sid)
        db.close()
        return sid

    def test_verify_is_readonly_and_never_initializes_an_engine(self):
        self.make_database()
        calls = []
        original = sqlite3.connect

        def observed(*args, **kwargs):
            calls.append((args, kwargs))
            return original(*args, **kwargs)

        with patch.object(tool.sqlite3, "connect", side_effect=observed):
            result = tool.verify_case(self.case, self.root)
        self.assertEqual(result["result"], "PASS")
        self.assertEqual((result["sessions"], result["artifacts"]), (1, 1))
        self.assertTrue(calls[0][0][0].endswith("?mode=ro"))
        self.assertTrue(calls[0][1]["uri"])

    def test_verify_rejects_active_session_without_recovering_it(self):
        sid = self.make_database(completed=False)
        with self.assertRaisesRegex(tool.AcceptanceError, "session-status"):
            tool.verify_case(self.case, self.root)
        with closing(sqlite3.connect(self.case / "database" / "acceptance.sqlite")) as conn:
            self.assertEqual(conn.execute("SELECT status FROM sessions WHERE id=?", (sid,)).fetchone()[0], "active")

    def test_verify_rejects_version_two_with_wrong_structural_fingerprint(self):
        self.make_database()
        with closing(sqlite3.connect(self.case / "database" / "acceptance.sqlite")) as conn:
            conn.execute("CREATE UNIQUE INDEX unexpected_unique ON artifacts(content)")
            conn.commit()
        with self.assertRaisesRegex(tool.AcceptanceError, "fingerprint"):
            tool.verify_case(self.case, self.root)

    def test_verified_complete_case_can_be_cleaned(self):
        self.make_database()
        run = tool.allocate_run(self.case)
        tool.write_json(run / "completed.json", {"result": "PASS"})
        tool.verify_case(self.case, self.root)
        tool.cleanup_case(self.case, self.root)
        self.assertFalse(self.case.exists())

    def test_runtime_identity_rejects_wrong_interpreter_without_sqlite_open(self):
        with patch.object(sys, "executable", str(self.case / "wrong.exe")), patch.object(sqlite3, "connect") as opened:
            with self.assertRaisesRegex(tool.AcceptanceError, "interpreter"):
                tool.runtime_identity()
            opened.assert_not_called()

    def test_runtime_identity_records_source_id_and_loaded_dll(self):
        class NativeCall:
            def __init__(self, callback):
                self.callback = callback

            def __call__(self, *args):
                return self.callback(*args)

        dll = str(tool.EXPECTED_PYTHON.parent / "Library" / "bin" / "sqlite3.dll")

        def filename(handle, buffer, size):
            buffer.value = dll
            return len(dll)

        kernel = types.SimpleNamespace(GetModuleHandleW=NativeCall(lambda name: 123),
                                       GetModuleFileNameW=NativeCall(filename))
        with patch.object(tool.ctypes, "WinDLL", return_value=kernel):
            evidence = tool.runtime_identity()
        self.assertEqual(evidence["sqlite_dll"], dll)
        self.assertEqual(evidence["sqlite"], "3.53.4")
        self.assertTrue(evidence["sqlite_source_id"])

    def test_runtime_identity_closes_memory_connection_on_query_failure(self):
        conn = types.SimpleNamespace(execute=lambda *args: (_ for _ in ()).throw(RuntimeError("source failed")),
                                     close=lambda: None)
        with patch.object(conn, "close") as closed, patch.object(sqlite3, "connect", return_value=conn):
            with self.assertRaisesRegex(RuntimeError, "source failed"):
                tool.runtime_identity()
            closed.assert_called_once()

    def test_cleanup_removes_only_owned_unused_case(self):
        sibling = tool.prepare_case(self.root / "sibling", self.root)
        tool.cleanup_case(self.case, self.root)
        self.assertFalse(self.case.exists())
        self.assertTrue((sibling / "ownership.json").is_file())

    def test_failed_pending_and_incomplete_cases_are_preserved(self):
        run = tool.allocate_run(self.case)
        for marker in ("failure.json", "pending.json"):
            (run / marker).write_text("{}", encoding="utf-8")
            with self.assertRaises(tool.AcceptanceError):
                tool.cleanup_case(self.case, self.root)
            self.assertTrue(run.exists())
            (run / marker).unlink()
        with self.assertRaises(tool.AcceptanceError):
            tool.cleanup_case(self.case, self.root)

    def test_occupied_port_gate_does_not_start_server(self):
        import socket
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            with self.assertRaisesRegex(tool.AcceptanceError, "occupied"):
                tool.require_free_port(port)


class TestAcceptanceLogOwnership(unittest.TestCase):
    """Yalnızca geçici günlüklerle gerçek dosya açma ve Uvicorn kapanışını sınar."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name) / "run"
        (self.run / "logs").mkdir(parents=True)
        self.log_path = self.run / "logs" / "application.log"

    def handler(self):
        handler = tool.OwnedRunFileHandler(self.run)
        self.addCleanup(handler.close)
        return handler

    def emit(self, handler, text):
        handler.handle(logging.LogRecord("acceptance", logging.INFO, __file__, 1, text, (), None))
        handler.flush()

    def test_exclusive_initial_creation_refuses_existing_run_log(self):
        self.log_path.write_text("Foreign run log\n", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            tool.OwnedRunFileHandler(self.run)
        self.assertEqual(self.log_path.read_text(encoding="utf-8"), "Foreign run log\n")

    def test_second_handler_cannot_claim_first_handlers_log(self):
        handler = self.handler()
        self.emit(handler, "Owned entry")
        with self.assertRaises(FileExistsError):
            tool.OwnedRunFileHandler(self.run)
        self.assertEqual(self.log_path.read_text(encoding="utf-8"), "Owned entry\n")

    def test_repeated_logging_and_reopening_only_append(self):
        handler = self.handler()
        for text in ("first", "second", "third"):
            self.emit(handler, text)
            handler.close()
        self.assertEqual(self.log_path.read_text(encoding="utf-8"), "first\nsecond\nthird\n")

    def test_reopening_rejects_replaced_file_without_writing_to_it(self):
        handler = self.handler()
        self.emit(handler, "Original entry")
        handler.close()
        previous = self.log_path.with_name("previous.log")
        self.log_path.rename(previous)
        self.log_path.write_text("Replacement entry\n", encoding="utf-8")
        with self.assertRaisesRegex(tool.AcceptanceError, "ownership"):
            self.emit(handler, "Must not append")
        self.assertEqual(self.log_path.read_text(encoding="utf-8"), "Replacement entry\n")
        self.assertEqual(previous.read_text(encoding="utf-8"), "Original entry\n")

    def test_reopening_does_not_recreate_deleted_log(self):
        handler = self.handler()
        handler.close()
        self.log_path.unlink()
        with self.assertRaises(FileNotFoundError):
            self.emit(handler, "Must not recreate")
        self.assertFalse(self.log_path.exists())

    def test_distinct_run_logs_remain_isolated(self):
        other = Path(self.temp.name) / "other"
        (other / "logs").mkdir(parents=True)
        first = self.handler()
        second = tool.OwnedRunFileHandler(other)
        self.addCleanup(second.close)
        self.emit(first, "First run")
        self.emit(second, "Second run")
        self.assertEqual(self.log_path.read_text(encoding="utf-8"), "First run\n")
        self.assertEqual((other / "logs" / "application.log").read_text(encoding="utf-8"), "Second run\n")

    def test_real_uvicorn_configuration_closes_handler_but_subsequent_logging_works(self):
        code = '''
import importlib.util, logging, pathlib, sys
from uvicorn import Config
spec = importlib.util.spec_from_file_location("isolated_log_test", sys.argv[1])
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)
run = pathlib.Path(sys.argv[2])
handler = tool.OwnedRunFileHandler(run)
logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
logging.info("before uvicorn")
Config("unused:app", reload=False, log_level="warning")
assert handler.stream is None
logging.info("after uvicorn")
Config("unused:app", reload=False, log_level="warning")
logging.info("after repeated configuration")
handler.close()
logging.info("after explicit closure")
handler.close()
assert (run / "logs" / "application.log").read_text(encoding="utf-8").splitlines() == [
    "INFO:root:before uvicorn", "INFO:root:after uvicorn",
    "INFO:root:after repeated configuration", "INFO:root:after explicit closure"]
assert "vesper.engine" not in sys.modules and "ui" not in sys.modules
'''
        result = subprocess.run([sys.executable, "-B", "-c", code, str(LAUNCHER), str(self.run)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


class TestTerminalReporting(unittest.TestCase):
    """Gerçek raporlama sınırını sahte UI/işçilerle uçtan uca çalıştırır; donanım yüklenmez."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name)
        self.outcome = tool.RunOutcome(self.run)
        self.future = Future()
        self.future.set_result("özet")
        self.repo = types.SimpleNamespace(close_thread_connection=Mock())
        self.assistant = types.SimpleNamespace(
            session_id="disposable", _running=False, last_stop_drained=True,
            last_stop_finalized=True, _threads=[], repository=self.repo,
            capture=types.SimpleNamespace(_thread=None),
        )
        self.state = types.SimpleNamespace(assistant=self.assistant, stop_future=self.future,
                                           stop_worker=None, stop_failure=None, logging_errors=[])
        self.app = types.SimpleNamespace(state=types.SimpleNamespace(_vesper_assistant_lifecycle=self.state),
                                         is_stopped=False)
        event = threading.Event()
        event.set()
        self.server = types.SimpleNamespace(
            started=True, should_exit=True, force_exit=False, _captured_signals=[],
            servers=[types.SimpleNamespace(is_serving=lambda: False)],
            lifespan=types.SimpleNamespace(shutdown_event=event, shutdown_failed=False, error_occurred=False),
        )
        self.outcome.observe_cleanup(self.repo)

    def launch(self, error=None, after_snapshot=None):
        self.repo.close_thread_connection()
        self.outcome.shutdown_snapshot(self.app, self.server)
        self.assertFalse((self.run / "terminal.json").exists())
        self.app.is_stopped = True
        if after_snapshot:
            after_snapshot()
        if error is not None:
            raise error

    def execute(self, launch):
        return tool._execute_ui_and_report(self.outcome, launch, self.app, lambda: self.server)

    def report(self, expected):
        report = json.loads((self.run / "terminal.json").read_text(encoding="utf-8"))
        self.assertEqual(report["result"], expected)
        self.assertFalse((self.run / "completed.json").exists())
        self.assertFalse((self.run / "failure.json").exists())
        self.assertFalse((self.run / "pending.json").exists())
        return report

    def test_normal_return_has_one_terminal_result_after_intermediate_snapshot(self):
        self.assertEqual(self.execute(self.launch)["result"], "PASS")
        self.report("PASS")
        snapshot = json.loads((self.run / "shutdown-snapshot.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["kind"], "intermediate")
        self.assertFalse(snapshot["evidence"]["app_stopped"])
        with self.assertRaisesRegex(tool.AcceptanceError, "already attempted"):
            self.outcome.finish(self.app, self.server)

    def test_installed_uvicorn_signal_replay_succeeds_only_after_confirmed_cleanup(self):
        from uvicorn import Config, Server
        real_server = Server(Config("unused:app", log_config=None))

        def launch():
            self.launch()
            # Yalnızca kurulu Uvicorn bağlamı çalışır; sinyal üretimi ve kayıtları sahtedir.
            with patch("signal.signal"), patch("signal.raise_signal", side_effect=KeyboardInterrupt("replayed")):
                try:
                    with real_server.capture_signals():
                        real_server.handle_exit(signal.SIGINT, None)
                finally:
                    self.server._captured_signals = list(real_server._captured_signals)

        self.execute(launch)
        report = self.report("PASS")
        self.assertEqual(report["interrupt_classification"], "confirmed_signal_replay")
        self.assertEqual(report["exception"]["type"], "KeyboardInterrupt")
        self.assertIn("replayed", report["exception"]["traceback"])

    def test_early_interrupt_retains_original_and_unknown_outcome(self):
        original = KeyboardInterrupt("before startup")
        with self.assertRaises(KeyboardInterrupt) as caught:
            self.execute(lambda: (_ for _ in ()).throw(original))
        self.assertIs(caught.exception, original)
        self.report("UNKNOWN")

    def test_interrupt_without_captured_signal_evidence_is_not_success(self):
        with self.assertRaises(KeyboardInterrupt):
            self.execute(lambda: self.launch(KeyboardInterrupt("unproven")))
        self.report("UNKNOWN")

    def test_late_teardown_failure_overrides_successful_snapshot(self):
        original = OSError("framework teardown failed")
        with self.assertRaises(OSError) as caught:
            self.execute(lambda: self.launch(original))
        self.assertIs(caught.exception, original)
        self.assertIn("framework teardown failed", self.report("FAIL")["exception"]["traceback"])

    def test_pending_stop_or_live_worker_never_passes_on_interrupt(self):
        for pending in (True, False):
            with self.subTest(pending=pending), tempfile.TemporaryDirectory() as folder:
                self.run = Path(folder)
                self.outcome = tool.RunOutcome(self.run)
                self.outcome.observe_cleanup(self.repo)
                self.state.stop_future = Future() if pending else self.future
                self.state.stop_worker = None if pending else types.SimpleNamespace(is_alive=lambda: True)
                self.server._captured_signals = [signal.SIGINT]
                with self.assertRaises(KeyboardInterrupt):
                    self.execute(lambda: self.launch(KeyboardInterrupt()))
                self.report("UNKNOWN")

    def test_cleanup_failure_cannot_be_hidden_by_successful_engine_flags(self):
        close = self.repo.close_thread_connection
        self.repo.close_thread_connection = Mock(side_effect=OSError("close failed"))
        self.outcome.observe_cleanup(self.repo)

        def launch():
            try:
                self.repo.close_thread_connection()
            except OSError as exc:
                self.assertIn("close failed", str(exc))
            self.outcome.shutdown_snapshot(self.app, self.server)
            self.app.is_stopped = True

        with self.assertRaises(tool.AcceptanceError):
            self.execute(launch)
        self.assertIn("close failed", self.report("FAIL")["evidence"]["cleanup_errors"][0])
        self.repo.close_thread_connection = close

    def test_asgi_teardown_failure_is_failure_even_after_app_stopped(self):
        def failed():
            self.server.lifespan.shutdown_failed = True
        with self.assertRaises(tool.AcceptanceError):
            self.execute(lambda: self.launch(after_snapshot=failed))
        self.report("FAIL")

    def test_missing_asgi_completion_is_unknown(self):
        with self.assertRaises(tool.AcceptanceError):
            self.execute(lambda: self.launch(after_snapshot=self.server.lifespan.shutdown_event.clear))
        self.report("UNKNOWN")

    def test_server_still_serving_or_app_not_stopped_is_unknown(self):
        for field in ("server", "app"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as folder:
                self.run = Path(folder)
                self.outcome = tool.RunOutcome(self.run)
                self.outcome.observe_cleanup(self.repo)

                def incomplete():
                    self.app.is_stopped = field != "app"
                    self.server.servers = [types.SimpleNamespace(is_serving=lambda: field == "server")]

                with self.assertRaises(tool.AcceptanceError):
                    self.execute(lambda: self.launch(after_snapshot=incomplete))
                self.report("UNKNOWN")

    def test_stop_failure_retains_original_worker_traceback(self):
        failure = ValueError("finalization error")
        try:
            raise failure
        except ValueError:
            self.state.stop_failure = failure
        with self.assertRaises(tool.AcceptanceError):
            self.execute(self.launch)
        evidence = self.report("FAIL")["evidence"]
        self.assertIn("finalization error", evidence["stop_error"]["traceback"])

    def test_force_exit_and_multiple_interrupts_are_not_success(self):
        self.server.force_exit = True
        self.server._captured_signals = [signal.SIGINT, signal.SIGINT]
        with self.assertRaises(KeyboardInterrupt):
            self.execute(lambda: self.launch(KeyboardInterrupt()))
        self.report("FAIL")

    def test_idle_without_meeting_or_stop_future_requires_full_cleanup_too(self):
        self.assistant.session_id = None
        self.assistant.last_stop_drained = None
        self.assistant.last_stop_finalized = None
        self.state.stop_future = None
        self.execute(self.launch)
        self.report("PASS")

    def test_failed_stop_future_cannot_pass_with_otherwise_successful_flags(self):
        self.state.stop_future = Future()
        self.state.stop_future.set_exception(ValueError("worker failure"))
        with self.assertRaises(tool.AcceptanceError):
            self.execute(self.launch)
        self.assertIn("worker failure", self.report("FAIL")["evidence"]["stop_error"]["traceback"])

    def test_retained_logging_error_prevents_clean_shutdown_claim(self):
        self.state.logging_errors.append(OSError("logging failed"))
        with self.assertRaises(tool.AcceptanceError):
            self.execute(self.launch)
        self.report("FAIL")

    def test_terminal_collision_preserves_existing_evidence_and_primary_error(self):
        target = self.run / "terminal.json"
        target.write_text("owned previous evidence", encoding="utf-8")
        original = ValueError("primary")
        with self.assertRaises(ValueError) as caught:
            self.execute(lambda: (_ for _ in ()).throw(original))
        self.assertIs(caught.exception, original)
        self.assertIn("Terminal reporting also failed", original.__notes__[0])
        self.assertEqual(target.read_text(encoding="utf-8"), "owned previous evidence")

    def test_reporting_failure_preserves_already_chained_primary_exception(self):
        target = self.run / "terminal.json"
        target.write_text("owned previous evidence", encoding="utf-8")
        original = ValueError("primary")
        cause = KeyError("original cause")
        tracebacks = []

        def launch():
            try:
                raise original from cause
            except ValueError:
                tracebacks.append(original.__traceback__)
                raise

        try:
            self.execute(launch)
        except ValueError as caught:
            self.assertIs(caught, original)
            caught_traceback = caught.__traceback__
        else:
            self.fail("Asıl hata yeniden yükseltilmeliydi")
        self.assertIs(original.__cause__, cause)
        self.assertTrue(original.__suppress_context__)
        self.assertIsInstance(original.reporting_error, FileExistsError)
        self.assertIn("Terminal reporting also failed", original.__notes__[0])
        cursor = caught_traceback
        while cursor is not None and cursor is not tracebacks[0]:
            cursor = cursor.tb_next
        self.assertIs(cursor, tracebacks[0])
        self.assertEqual(target.read_text(encoding="utf-8"), "owned previous evidence")


class TestReleaseToolingManifest(unittest.TestCase):
    """Dış araç kanıtını doğrular; eksik araç veya farklı baytlar sessizce atlanmaz."""

    def test_manifest_matches_launcher_runtime_and_dependency_metadata(self):
        record = json.loads(TOOLING_MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(hashlib.sha256(LAUNCHER.read_bytes()).hexdigest(), record["launcher"]["sha256"])
        self.assertEqual(sys.version.split()[0], record["runtime"]["python"])
        self.assertEqual(sqlite3.sqlite_version, record["runtime"]["sqlite"])
        for name, version in record["tool_dependencies"].items():
            self.assertEqual(metadata.version(name), version)
        for evidence in record["provenance"]["evidence_files"]:
            self.assertEqual(hashlib.sha256(Path(evidence["path"]).read_bytes()).hexdigest(), evidence["sha256"])

    def test_relocated_identical_tool_is_allowed_but_missing_or_tampered_is_not(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "acceptance_launcher.py"
            with self.assertRaisesRegex(RuntimeError, "Provision"):
                resolve_launcher(override=str(path))
            path.write_bytes(LAUNCHER.read_bytes())
            self.assertEqual(resolve_launcher(override=str(path)), path)
            path.write_bytes(path.read_bytes() + b"\n\n")
            with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                resolve_launcher(override=str(path))


if __name__ == "__main__":
    unittest.main()
