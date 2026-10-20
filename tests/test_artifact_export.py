"""Ek bilgi dışa aktarımını geçici SQLite ve bağımsız Markdown işleyicisiyle sınar."""

from html.parser import HTMLParser
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import markdown2

from vesper.repository import SessionRepository
from vesper.session import SUMMARY_FINAL, SUMMARY_LIVE
from vesper.storage import Database


class ParsedHtml(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.text = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)

    def handle_data(self, data):
        self.text.append(data)


class TestArtifactExport(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / "export.db")
        self.addCleanup(self.db.close)
        self.repo = SessionRepository(self.db)
        self.sid = self.repo.create_session("Toplantı")
        self.repo.append_segment(self.sid, 1, "t0", "t1", "Özgün transkript")
        self.repo.save_summary(self.sid, SUMMARY_FINAL, "## Genel Özet\n\nÖzgün özet")

    def render(self):
        markdown = self.repo.export_markdown(self.sid)
        return markdown, ParsedHtml(markdown2.markdown(markdown, extras=["fenced-code-blocks", "tables"]))

    def test_empty_materials_preserve_original_export_exactly(self):
        self.assertEqual(self.repo.export_markdown(self.sid),
                         "# Toplantı\n\n## Genel Özet\n\nÖzgün özet\n\n---\n\n## Tam Transkript\n\nÖzgün transkript")

    def test_link_and_note_include_kind_content_and_saved_timestamp(self):
        with patch("vesper.repository._now_iso", return_value="2026-10-20T10:00:00+00:00"):
            self.repo.add_artifact(self.sid, "link", "https://example.com/?q=one#part")
            self.repo.add_artifact(self.sid, "note", "Bir not")
        markdown, html = self.render()
        self.assertIn("## Meeting Materials", markdown)
        for text in ("Kind: link", "Kind: note", "https://example.com/?q=one#part", "Bir not", "2026-10-20T10:00:00+00:00"):
            self.assertIn(text, "".join(html.text))
        self.assertNotIn("a", html.tags)

    def test_existing_sections_and_formatting_remain_an_exact_prefix(self):
        original = self.repo.export_markdown(self.sid)
        self.repo.add_artifact(self.sid, "note", "Ek bilgi")
        exported = self.repo.export_markdown(self.sid)
        self.assertTrue(exported.startswith(original + "\n\n---\n\n## Meeting Materials\n\n"))
        self.assertEqual(exported.count("## Tam Transkript"), 1)
        self.assertEqual(exported.count("## Genel Özet"), 1)

    def test_order_is_saved_timestamp_then_id(self):
        with patch("vesper.repository._now_iso", return_value="2026-10-20T10:00:02+00:00"):
            self.repo.add_artifact(self.sid, "note", "LAST-CONTENT")
        with patch("vesper.repository._now_iso", return_value="2026-10-20T10:00:01+00:00"):
            self.repo.add_artifact(self.sid, "note", "FIRST-CONTENT")
            self.repo.add_artifact(self.sid, "note", "SECOND-CONTENT")
        exported = self.repo.export_markdown(self.sid)
        self.assertLess(exported.index("FIRST-CONTENT"), exported.index("SECOND-CONTENT"))
        self.assertLess(exported.index("SECOND-CONTENT"), exported.index("LAST-CONTENT"))
        self.assertEqual(exported, self.repo.export_markdown(self.sid))

    def test_only_selected_sessions_materials_are_exported(self):
        other = self.repo.create_session("Başka toplantı")
        self.repo.add_artifact(other, "note", "OTHER-SECRET")
        self.repo.add_artifact(self.sid, "note", "OWN-CONTENT")
        exported = self.repo.export_markdown(self.sid)
        self.assertIn("OWN-CONTENT", exported)
        self.assertNotIn("OTHER-SECRET", exported)

    def test_deletions_are_reflected_by_fresh_database_export(self):
        aid = self.repo.add_artifact(self.sid, "note", "DELETE-ME")
        self.assertIn("DELETE-ME", self.repo.export_markdown(self.sid))
        self.repo.delete_artifact(self.sid, aid)
        self.assertNotIn("DELETE-ME", self.repo.export_markdown(self.sid))
        self.assertNotIn("## Meeting Materials", self.repo.export_markdown(self.sid))

    def test_multiline_notes_remain_literal_in_rendered_output(self):
        content = "  Başlık\n\n\t- Liste\n# Sahte başlık\n[link](https://evil.example)\n**kalın**"
        self.repo.add_artifact(self.sid, "note", content)
        markdown, html = self.render()
        self.assertIn(content, markdown)
        # Markdown işleyicisi sekmeleri görsel boşluğa genişletir; dosyadaki özgün metin üstte doğrulanır.
        self.assertIn(content.expandtabs(4), "".join(html.text))
        self.assertNotIn("a", html.tags)
        self.assertNotIn("strong", html.tags)
        self.assertNotIn("li", html.tags)

    def test_html_and_markdown_commands_cannot_escape_literal_blocks(self):
        content = '<script>alert(1)</script>\n<img src=x onerror=alert(2)>\n</code></pre>\n[go](javascript:alert(3))\n<https://evil.example>'
        self.repo.add_artifact(self.sid, "note", content)
        _, html = self.render()
        for tag in ("script", "img", "a", "iframe"):
            self.assertNotIn(tag, html.tags)
        self.assertIn(content, "".join(html.text))

    def test_backtick_fence_injection_cannot_activate_html_or_markdown(self):
        for count in (3, 4, 8, 32):
            with self.subTest(count=count):
                content = "`" * count + '\n<script>bad()</script>\n![image](https://evil.example)\n' + "`" * count
                aid = self.repo.add_artifact(self.sid, "note", content)
                _, html = self.render()
                self.assertNotIn("script", html.tags)
                self.assertNotIn("img", html.tags)
                self.assertIn(content, "".join(html.text))
                self.repo.delete_artifact(self.sid, aid)

    def test_carriage_returns_and_alternate_fences_stay_literal(self):
        content = "~~~\r\n<script>bad()</script>\r\n~~~\r\n```\r\n# inert"
        self.repo.add_artifact(self.sid, "note", content)
        markdown, html = self.render()
        self.assertIn(content, markdown)
        self.assertNotIn("script", html.tags)
        self.assertIn("# inert", "".join(html.text))

    def test_untrusted_database_metadata_is_literal_too(self):
        conn = self.db.get_connection()
        with conn:
            conn.execute("INSERT INTO artifacts(session_id,kind,content,capture_method,created_at) VALUES(?,?,?,?,?)",
                         (self.sid, "```\n<script>kind()</script>", "Not", "manual", "```\n<img src=x>"))
        _, html = self.render()
        self.assertNotIn("script", html.tags)
        self.assertNotIn("img", html.tags)
        self.assertIn("<script>kind()</script>", "".join(html.text))

    def test_existing_live_summary_fallback_remains_available(self):
        sid = self.repo.create_session("Canlı")
        self.repo.save_summary(sid, SUMMARY_LIVE, "Canlı özet")
        self.repo.add_artifact(sid, "note", "Ek not")
        exported = self.repo.export_markdown(sid)
        self.assertIn("Canlı özet", exported)
        self.assertIn("(transkript boş)", exported)
        self.assertIn("Ek not", exported)

    def test_missing_session_still_rejected(self):
        with self.assertRaises(ValueError):
            self.repo.export_markdown("missing")


if __name__ == "__main__":
    unittest.main()
