"""Gerçek motor/depo ile UI olaylarını sahte NiceGUI üzerinde sınar.

Veriler ve çıktılar geçicidir; sunucu, tarayıcı, ses veya model başlatılmaz.
"""

import asyncio
import gc
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from tests import test_lifecycle_integration as lifecycle
from vesper.repository import SessionRepository
from vesper.session import SUMMARY_FINAL
from vesper.storage import Database


class FakeElement:
    def __init__(self, ui, kind, *args, **kwargs):
        self.ui, self.kind = ui, kind
        self.args, self.kwargs = args, kwargs
        self.text = args[0] if args else ""
        self.value = kwargs.get("value", "")
        self.content = self.text
        self.children = []
        self.parent = ui.stack[-1] if ui.stack else None
        self.events = {}
        self.enabled = True
        self.visible = True
        self.opened = False
        self.class_names = ""
        self.styles = ""
        if self.parent:
            self.parent.children.append(self)
        ui.elements.append(self)

    def __enter__(self):
        self.ui.stack.append(self)
        return self

    def __exit__(self, *args):
        self.ui.stack.pop()

    def classes(self, value="", **kwargs):
        self.class_names = kwargs.get("replace", self.class_names + " " + value)
        return self

    def props(self, *args, **kwargs):
        return self

    def style(self, value):
        self.styles = value
        return self

    def on(self, event, callback):
        self.events[event] = callback
        return self

    def enable(self):
        self.enabled = True
        return self

    def disable(self):
        self.enabled = False
        return self

    def set_value(self, value):
        self.value = value

    def set_text(self, value):
        self.text = value

    def set_content(self, value):
        self.content = value

    def set_visibility(self, value):
        self.visible = value

    def clear(self):
        self.children.clear()

    def open(self):
        self.opened = True

    def close(self):
        self.opened = False

    def click(self):
        return self.kwargs.get("on_click", self.events.get("click"))()

    def descendants(self):
        return [child for item in self.children for child in [item] + item.descendants()]


class FakeUi:
    def __init__(self):
        self.stack = []
        self.elements = []
        self.notifications = []
        self.head = []
        self.body = []
        self.timers = []
        self.run_calls = []

    def __getattr__(self, name):
        return lambda *args, **kwargs: FakeElement(self, name, *args, **kwargs)

    def notify(self, text, **kwargs):
        self.notifications.append((text, kwargs.get("type")))

    def add_head_html(self, value):
        self.head.append(value)

    def add_body_html(self, value):
        self.body.append(value)

    def timer(self, interval, callback):
        self.timers.append((interval, callback))

    def run(self, **kwargs):
        self.run_calls.append(kwargs)


class FakeApp:
    """Süreç kayıtlarını ve kapanış geri çağrılarını bellekte tutar."""

    def __init__(self):
        self.state = types.SimpleNamespace()
        self.shutdown_handlers = []

    def on_shutdown(self, callback):
        self.shutdown_handlers.append(callback)


class TestArtifactUi(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(gc.collect)
        self.db = Database(Path(self.temp.name) / "ui.db")
        self.addCleanup(self.db.close)
        self.repo = SessionRepository(self.db)
        self.assistant = lifecycle._engine_module.WebinarAssistant(repository=self.repo)
        self.workers = []
        output_patch = patch.object(lifecycle._engine_module.config, "OUTPUT_DIR", str(Path(self.temp.name) / "outputs"))
        output_patch.start()
        self.addCleanup(output_patch.stop)
        self.addCleanup(self.stop_workers)
        self.fake = FakeUi()
        nicegui = types.ModuleType("nicegui")
        nicegui.ui = self.fake
        nicegui.app = FakeApp()

        async def io_bound(callback):
            return callback()

        nicegui.run = types.SimpleNamespace(io_bound=io_bound)
        engine = types.ModuleType("vesper.engine")
        engine.WebinarAssistant = lambda: self.assistant
        spec = importlib.util.spec_from_file_location("isolated_material_ui", Path(__file__).resolve().parents[1] / "ui.py")
        self.page = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"nicegui": nicegui, "vesper.engine": engine}):
            spec.loader.exec_module(self.page)

    def stop_workers(self):
        if self.assistant._running:
            self.assistant.stop()
        for worker in set(self.workers + self.assistant._threads):
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())

    def start(self):
        self.page.on_start()
        self.workers.extend(self.assistant._threads)
        return self.assistant.session_id

    def editor(self):
        children = self.page.live_materials_editor.descendants()
        return (next(n for n in children if n.kind == "select"),
                next(n for n in children if n.kind == "textarea"),
                next(n for n in children if n.kind == "button" and n.text == "Ekle"))

    def add(self, content="Bir not", kind="note"):
        select, field, button = self.editor()
        select.set_value(kind)
        field.set_value(content)
        button.click()
        return field, button

    def labels(self, container):
        return [node.text for node in container.descendants() if node.kind == "label"]

    def delete_button(self, container):
        return next(n for n in container.descendants() if n.kind == "button" and n.kwargs.get("icon") == "delete")

    def dialog_button(self, text):
        dialog = next(n for n in reversed(self.fake.elements) if n.kind == "dialog" and n.opened)
        return next(n for n in dialog.descendants() if n.kind == "button" and n.text == text)

    def test_page_uses_fake_server_timer_and_existing_design(self):
        self.assertEqual(len(self.fake.run_calls), 1)
        self.assertEqual(self.fake.timers[0][0], 5.0)
        self.assertIn(".vesper-glass", self.fake.head[0])
        self.assertIn(".vesper-split", self.fake.head[0])
        self.assertIn("vesper-resize-handle", self.fake.body[0])
        self.assertFalse(self.page.history_view.visible)
        self.assertEqual(self.repo.list_sessions(), [])
        self.assertFalse(any(n.kind == "textarea" for n in self.page.live_materials_editor.descendants()))

    def test_live_note_creation_is_immediate_and_preserves_plain_text(self):
        sid = self.start()
        content = '  Bir not\n<script>bad()</script>\n**kalın değil**'
        field, _ = self.add(content)
        self.assertEqual(field.value, "")
        self.assertEqual(self.repo.list_artifacts(sid)[0].content, content)
        labels = self.labels(self.page.live_materials_list)
        self.assertIn(content, labels)
        self.assertIn("Note", labels)
        self.assertIn(self.page._format_local_datetime(self.repo.list_artifacts(sid)[0].created_at), labels)
        self.assertFalse(any(n.kind in ("html", "markdown") for n in self.page.live_materials_list.descendants()))

    def test_live_link_creation_uses_backend_validation(self):
        sid = self.start()
        content = "https://example.com/path?q=1#part"
        self.add(content, "link")
        self.assertEqual(self.repo.list_artifacts(sid)[0].kind, "link")
        self.assertIn(content, self.labels(self.page.live_materials_list))

    def test_invalid_input_has_feedback_and_keeps_draft(self):
        sid = self.start()
        for content, kind in ((" \n", "note"), ("javascript:alert(1)", "link"), ("text", "chat")):
            with self.subTest(content=content, kind=kind):
                field, _ = self.add(content, kind)
                self.assertEqual(field.value, content)
                self.assertEqual(self.fake.notifications[-1][1], "negative")
                self.assertEqual(self.repo.list_artifacts(sid), [])

    def test_poll_refreshes_external_additions_without_losing_draft(self):
        sid = self.start()
        _, field, _ = self.editor()
        field.set_value("Kaydedilmemiş taslak")
        self.repo.add_artifact(sid, "note", "Dışarıdan eklenen")
        self.page.refresh()
        self.assertEqual(self.editor()[1], field)
        self.assertEqual(field.value, "Kaydedilmemiş taslak")
        self.assertIn("Dışarıdan eklenen", self.labels(self.page.live_materials_list))

    def test_delete_requires_confirmation_and_cancel_preserves_row(self):
        sid = self.start()
        self.add()
        self.delete_button(self.page.live_materials_list).click()
        self.assertEqual(len(self.repo.list_artifacts(sid)), 1)
        self.dialog_button("İptal").click()
        self.assertEqual(len(self.repo.list_artifacts(sid)), 1)
        self.delete_button(self.page.live_materials_list).click()
        self.dialog_button("Sil").click()
        self.assertEqual(self.repo.list_artifacts(sid), [])
        self.assertNotIn("Bir not", self.labels(self.page.live_materials_list))

    def test_history_shows_only_selected_session_and_has_no_creation_form(self):
        first = self.repo.create_session("İlk")
        second = self.repo.create_session("İkinci")
        self.repo.add_artifact(first, "note", "FIRST-SECRET")
        self.repo.add_artifact(second, "note", "SECOND-SECRET")
        self.page.open_session_detail(first)
        self.assertIn("FIRST-SECRET", self.labels(self.page.history_materials_list))
        self.assertNotIn("SECOND-SECRET", self.labels(self.page.history_materials_list))
        self.page.open_session_detail(second)
        self.assertIn("SECOND-SECRET", self.labels(self.page.history_materials_list))
        self.assertNotIn("FIRST-SECRET", self.labels(self.page.history_materials_list))
        self.assertFalse(any(n.kind in ("textarea", "input", "select") for n in self.page.history_detail_content.descendants()))

    def test_history_deletion_removes_only_the_selected_artifact(self):
        first = self.repo.create_session("İlk")
        second = self.repo.create_session("İkinci")
        self.repo.add_artifact(first, "note", "FIRST")
        self.repo.add_artifact(second, "note", "SECOND")
        self.page.open_session_detail(first)
        self.delete_button(self.page.history_materials_list).click()
        self.dialog_button("Sil").click()
        self.assertEqual(self.repo.list_artifacts(first), [])
        self.assertEqual(self.repo.list_artifacts(second)[0].content, "SECOND")

    def test_pending_history_confirmation_keeps_original_owner_after_selection_change(self):
        first = self.repo.create_session("İlk")
        second = self.repo.create_session("İkinci")
        self.repo.add_artifact(first, "note", "FIRST")
        self.repo.add_artifact(second, "note", "SECOND")
        self.page.open_session_detail(first)
        self.delete_button(self.page.history_materials_list).click()
        confirm = self.dialog_button("Sil")
        self.page.open_session_detail(second)
        confirm.click()
        self.assertEqual(self.repo.list_artifacts(first), [])
        self.assertEqual(self.repo.list_artifacts(second)[0].content, "SECOND")
        self.assertIn("SECOND", self.labels(self.page.history_materials_list))

    def test_stale_live_event_cannot_attach_to_new_meeting(self):
        first = self.start()
        _, old_field, old_button = self.editor()
        old_field.set_value("OLD-DRAFT")
        asyncio.run(self.page.on_stop())
        second = self.start()
        self.editor()[1].set_value("NEW-DRAFT")
        old_button.click()
        self.assertEqual(self.repo.list_artifacts(first), [])
        self.assertEqual(self.repo.list_artifacts(second), [])
        self.assertEqual(self.editor()[1].value, "NEW-DRAFT")
        self.assertEqual(self.fake.notifications[-1][1], "negative")

    def test_external_end_rejects_old_form_and_disables_creation(self):
        sid = self.start()
        _, field, button = self.editor()
        field.set_value("Çok geç")
        self.repo.finish_session(sid)
        button.click()
        self.assertEqual(self.repo.list_artifacts(sid), [])
        self.assertEqual(self.fake.notifications[-1][1], "negative")
        self.assertFalse(any(n.kind == "textarea" for n in self.page.live_materials_editor.descendants()))

    def test_shutdown_closes_form_before_background_stop_is_called(self):
        sid = self.start()
        _, field, button = self.editor()
        field.set_value("Shutdown sırasında")
        original = self.assistant.stop

        def check_stop():
            self.assertTrue(self.page._materials_state["stopping"])
            button.click()
            self.assertEqual(self.repo.list_artifacts(sid), [])
            return original()

        with patch.object(self.assistant, "stop", check_stop):
            asyncio.run(self.page.on_stop())
        self.assertFalse(any(n.kind == "textarea" for n in self.page.live_materials_editor.descendants()))

    def test_stopped_meeting_keeps_materials_visible_and_next_meeting_clears_them(self):
        first = self.start()
        self.add("PREVIOUS-CONTENT")
        asyncio.run(self.page.on_stop())
        self.assertIn("PREVIOUS-CONTENT", self.labels(self.page.live_materials_list))
        second = self.start()
        self.assertNotEqual(first, second)
        self.assertNotIn("PREVIOUS-CONTENT", self.labels(self.page.live_materials_list))
        self.assertEqual(self.repo.list_artifacts(first)[0].content, "PREVIOUS-CONTENT")

    def test_history_summary_and_transcript_are_preserved(self):
        sid = self.repo.create_session("Özgün başlık")
        self.repo.append_segment(sid, 1, "t0", "t1", "Özgün konuşma")
        self.repo.save_summary(sid, SUMMARY_FINAL, "Özgün özet")
        self.page.open_session_detail(sid)
        self.assertEqual(self.page.history_detail_title_label.text, "Özgün başlık")
        self.assertEqual(self.page.history_detail_summary_area.content, "Özgün özet")
        self.assertEqual(self.page.history_detail_transcript_area.content, "Özgün konuşma")
        self.assertIs(self.page.history_detail_summary_area.parent.parent, self.page.history_detail_transcript_area.parent.parent)

    def test_clearing_or_deleting_last_history_session_removes_materials(self):
        sid = self.repo.create_session("Son oturum")
        self.repo.add_artifact(sid, "note", "REMOVED")
        self.page.open_session_detail(sid)
        self.repo.delete_session(sid)
        self.page.refresh_history_list()
        self.assertIsNone(self.page._history_state["selected_id"])
        self.assertNotIn("REMOVED", self.labels(self.page.history_materials_list))
        self.assertFalse(self.page.history_detail_content.visible)

    def test_storage_read_error_clears_old_list_and_disables_form(self):
        self.start()
        self.add("SECRET")
        with patch.object(self.assistant, "list_artifacts", side_effect=RuntimeError("test read failure")):
            self.page.refresh_live_materials()
        self.assertNotIn("SECRET", self.labels(self.page.live_materials_list))
        self.assertFalse(any(n.kind == "textarea" for n in self.page.live_materials_editor.descendants()))
        self.page.refresh_live_materials()
        self.assertIn("SECRET", self.labels(self.page.live_materials_list))

    def test_creation_failure_does_not_clear_user_input(self):
        sid = self.start()
        with patch.object(self.assistant, "add_artifact", side_effect=OSError("test write failure")):
            field, _ = self.add("KEEP-DRAFT")
        self.assertEqual(field.value, "KEEP-DRAFT")
        self.assertEqual(self.fake.notifications[-1][1], "negative")
        self.assertEqual(self.repo.list_artifacts(sid), [])

    def test_delete_failure_preserves_row_and_confirmation(self):
        sid = self.start()
        self.add()
        self.delete_button(self.page.live_materials_list).click()
        with patch.object(self.assistant, "delete_artifact", side_effect=OSError("test delete failure")):
            self.dialog_button("Sil").click()
        self.assertEqual(len(self.repo.list_artifacts(sid)), 1)
        self.assertEqual(self.fake.notifications[-1][1], "negative")
        self.assertIsNotNone(self.dialog_button("İptal"))


if __name__ == "__main__":
    unittest.main()
