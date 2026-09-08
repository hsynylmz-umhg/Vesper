"""
Kalıcılık katmanı (storage.py + repository.py + session.py) testleri.

ÖNEMLİ: Her test kendi geçici veritabanı dosyasını kullanır. Hiçbir test
data/vesper.db dosyasına dokunmaz.

Çalıştırma:
    python -m unittest discover -s tests -t .
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from repository import SessionRepository
from session import STATUS_ACTIVE, STATUS_COMPLETED, STATUS_INTERRUPTED, SUMMARY_FINAL, SUMMARY_LIVE
from storage import SCHEMA_VERSION, Database


class TempDbTestCase(unittest.TestCase):
    """Her test metodu için taze, izole bir sqlite dosyası verir."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "test_vesper.db"
        self.db = Database(self.db_path)
        self.repo = SessionRepository(self.db)

    def tearDown(self):
        self.db.close()
        self._tmpdir.cleanup()


class TestSchema(TempDbTestCase):
    def test_database_file_created(self):
        self.assertTrue(self.db_path.exists())

    def test_tables_exist(self):
        conn = self.db.get_connection()
        names = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for expected in ("sessions", "segments", "summaries", "schema_metadata"):
            self.assertIn(expected, names)

    def test_schema_version_recorded(self):
        conn = self.db.get_connection()
        row = conn.execute("SELECT schema_version FROM schema_metadata").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["schema_version"], SCHEMA_VERSION)

    def test_foreign_keys_enabled(self):
        conn = self.db.get_connection()
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_wal_mode_enabled(self):
        conn = self.db.get_connection()
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

    def test_init_schema_idempotent(self):
        # İkinci kez çağırmak hata vermemeli (IF NOT EXISTS + var olan sürüm satırını korur)
        self.db.init_schema()
        self.db.init_schema()
        conn = self.db.get_connection()
        count = conn.execute("SELECT COUNT(*) FROM schema_metadata").fetchone()[0]
        self.assertEqual(count, 1)


class TestSessionCrud(TempDbTestCase):
    def test_create_session_defaults(self):
        sid = self.repo.create_session(title="Webinar — 2026-09-05 19:30", source_language="tr", summary_language="tr")
        session = self.repo.get_session(sid)
        self.assertIsNotNone(session)
        self.assertEqual(session.title, "Webinar — 2026-09-05 19:30")
        self.assertEqual(session.status, STATUS_ACTIVE)
        self.assertIsNone(session.ended_at)
        self.assertEqual(session.source_language, "tr")

    def test_finish_session_sets_status_and_ended_at(self):
        sid = self.repo.create_session(title="Oturum A")
        self.repo.finish_session(sid, status=STATUS_COMPLETED)
        session = self.repo.get_session(sid)
        self.assertEqual(session.status, STATUS_COMPLETED)
        self.assertIsNotNone(session.ended_at)

    def test_list_sessions_newest_first(self):
        sid1 = self.repo.create_session(title="Birinci")
        # started_at aynı milisaniyede çakışmasın diye tabloyu doğrudan güncelliyoruz
        conn = self.db.get_connection()
        with conn:
            conn.execute("UPDATE sessions SET started_at = '2026-01-01T00:00:00+00:00' WHERE id = ?", (sid1,))
        sid2 = self.repo.create_session(title="İkinci")
        with conn:
            conn.execute("UPDATE sessions SET started_at = '2026-06-01T00:00:00+00:00' WHERE id = ?", (sid2,))

        sessions = self.repo.list_sessions()
        self.assertEqual([s.id for s in sessions][:2], [sid2, sid1])

    def test_rename_session(self):
        sid = self.repo.create_session(title="Eski başlık")
        self.repo.rename_session(sid, "Yeni başlık")
        self.assertEqual(self.repo.get_session(sid).title, "Yeni başlık")

    def test_rename_session_rejects_empty_title(self):
        sid = self.repo.create_session(title="Bir şey")
        with self.assertRaises(ValueError):
            self.repo.rename_session(sid, "   ")

    def test_delete_session_removes_dependents(self):
        sid = self.repo.create_session(title="Silinecek")
        self.repo.append_segment(sid, 1, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:05+00:00", "merhaba")
        self.repo.save_summary(sid, SUMMARY_LIVE, "kısa özet")

        other_sid = self.repo.create_session(title="Etkilenmemeli")
        self.repo.append_segment(other_sid, 1, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:05+00:00", "başka oturum")

        self.repo.delete_session(sid)

        self.assertIsNone(self.repo.get_session(sid))
        self.assertEqual(self.repo.get_segments(sid), [])
        self.assertIsNone(self.repo.get_latest_summary(sid, SUMMARY_LIVE))

        # başka oturum dokunulmamış olmalı
        self.assertIsNotNone(self.repo.get_session(other_sid))
        self.assertEqual(len(self.repo.get_segments(other_sid)), 1)

    def test_delete_session_actually_removes_rows_from_tables(self):
        # get_segments üzerinden değil, doğrudan tablo satır sayısıyla da doğrula
        sid = self.repo.create_session(title="X")
        self.repo.append_segment(sid, 1, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:05+00:00", "a")
        self.repo.save_summary(sid, SUMMARY_FINAL, "final")
        self.repo.delete_session(sid)
        conn = self.db.get_connection()
        seg_count = conn.execute("SELECT COUNT(*) FROM segments WHERE session_id = ?", (sid,)).fetchone()[0]
        sum_count = conn.execute("SELECT COUNT(*) FROM summaries WHERE session_id = ?", (sid,)).fetchone()[0]
        self.assertEqual(seg_count, 0)
        self.assertEqual(sum_count, 0)


class TestSegments(TempDbTestCase):
    def test_append_and_get_segments_in_order(self):
        sid = self.repo.create_session(title="Sıra testi")
        # kasıtlı olarak sırasız ekle
        self.repo.append_segment(sid, 2, "t0", "t1", "ikinci")
        self.repo.append_segment(sid, 1, "t0", "t1", "birinci")
        self.repo.append_segment(sid, 3, "t0", "t1", "üçüncü")

        segments = self.repo.get_segments(sid)
        self.assertEqual([s.text for s in segments], ["birinci", "ikinci", "üçüncü"])
        self.assertEqual([s.sequence for s in segments], [1, 2, 3])

    def test_full_transcript_text_joins_segments(self):
        sid = self.repo.create_session(title="Birleştirme")
        self.repo.append_segment(sid, 1, "t0", "t1", "Merhaba")
        self.repo.append_segment(sid, 2, "t1", "t2", "dünya.")
        self.assertEqual(self.repo.get_full_transcript_text(sid), "Merhaba dünya.")

    def test_turkish_characters_preserved(self):
        sid = self.repo.create_session(title="Türkçe testi")
        text = "Şu an İstanbul'da öğleden sonra güneşli, çok güzel bir gün."
        self.repo.append_segment(sid, 1, "t0", "t1", text)
        self.assertEqual(self.repo.get_segments(sid)[0].text, text)

    def test_duplicate_sequence_rejected(self):
        sid = self.repo.create_session(title="Çakışma")
        self.repo.append_segment(sid, 1, "t0", "t1", "ilk")
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.append_segment(sid, 1, "t0", "t1", "çakışan")


class TestSummaries(TempDbTestCase):
    def test_save_and_get_latest_live_summary(self):
        sid = self.repo.create_session(title="Canlı özet testi")
        self.repo.save_summary(sid, SUMMARY_LIVE, "ilk taslak", model="gemma4:e2b")
        self.repo.save_summary(sid, SUMMARY_LIVE, "ikinci taslak", model="gemma4:e2b")
        latest = self.repo.get_latest_summary(sid, SUMMARY_LIVE)
        self.assertEqual(latest.content, "ikinci taslak")

    def test_final_summary_distinct_from_live(self):
        sid = self.repo.create_session(title="Final ayrımı")
        self.repo.save_summary(sid, SUMMARY_LIVE, "canlı özet")
        self.repo.save_summary(sid, SUMMARY_FINAL, "final özet")
        self.assertEqual(self.repo.get_latest_summary(sid, SUMMARY_LIVE).content, "canlı özet")
        self.assertEqual(self.repo.get_latest_summary(sid, SUMMARY_FINAL).content, "final özet")

    def test_no_summary_returns_none(self):
        sid = self.repo.create_session(title="Özet yok")
        self.assertIsNone(self.repo.get_latest_summary(sid, SUMMARY_FINAL))


class TestInterruptedRecovery(TempDbTestCase):
    def test_active_session_marked_interrupted_on_recovery(self):
        sid = self.repo.create_session(title="Kesilen oturum")
        self.assertEqual(self.repo.get_session(sid).status, STATUS_ACTIVE)

        recovered_ids = self.repo.recover_interrupted_sessions()

        self.assertIn(sid, recovered_ids)
        session = self.repo.get_session(sid)
        self.assertEqual(session.status, STATUS_INTERRUPTED)

    def test_completed_session_not_touched_by_recovery(self):
        sid = self.repo.create_session(title="Temiz biten")
        self.repo.finish_session(sid, status=STATUS_COMPLETED)
        self.repo.recover_interrupted_sessions()
        self.assertEqual(self.repo.get_session(sid).status, STATUS_COMPLETED)

    def test_interrupted_session_keeps_its_data(self):
        sid = self.repo.create_session(title="Veri kaybolmamalı")
        self.repo.append_segment(sid, 1, "t0", "t1", "kesilmeden önce söylenen şey")
        self.repo.recover_interrupted_sessions()
        # veri hâlâ erişilebilir olmalı, silinmemiş olmalı
        self.assertEqual(len(self.repo.get_segments(sid)), 1)
        self.assertEqual(self.repo.get_session(sid).status, STATUS_INTERRUPTED)


class TestMarkdownExport(TempDbTestCase):
    def test_export_includes_title_summary_and_transcript(self):
        sid = self.repo.create_session(title="Pazartesi Webinarı")
        self.repo.append_segment(sid, 1, "t0", "t1", "Bugün konuşacağımız konu X.")
        self.repo.append_segment(sid, 2, "t1", "t2", "Devamında Y'yi ele alacağız.")
        self.repo.save_summary(
            sid, SUMMARY_FINAL,
            "## Genel Özet\n\nKonu X ve Y ele alındı.\n\n"
            "## Ana Noktalar\n\n- X\n- Y\n\n"
            "## Aksiyon Maddeleri / Kararlar\n\nBelirtilmedi",
            model="gemma4:e2b",
        )

        markdown = self.repo.export_markdown(sid)

        self.assertTrue(markdown.startswith("# Pazartesi Webinarı"))
        self.assertIn("## Genel Özet", markdown)
        self.assertIn("## Ana Noktalar", markdown)
        # Başlık tekrarlanmamalı: sadece final özetin içindeki tek bir kopya olmalı
        self.assertEqual(markdown.count("## Genel Özet"), 1)
        self.assertIn("## Tam Transkript", markdown)
        self.assertIn("Bugün konuşacağımız konu X.", markdown)
        self.assertIn("Devamında Y'yi ele alacağız.", markdown)
        # transkript, özetten SONRA gelmeli
        self.assertLess(markdown.index("## Tam Transkript"), markdown.index("Bugün konuşacağımız"))

    def test_export_falls_back_to_live_summary_when_no_final(self):
        sid = self.repo.create_session(title="Yarım kalan")
        self.repo.append_segment(sid, 1, "t0", "t1", "bir şeyler")
        self.repo.save_summary(sid, SUMMARY_LIVE, "sadece canlı özet var")
        markdown = self.repo.export_markdown(sid)
        self.assertIn("sadece canlı özet var", markdown)

    def test_export_missing_session_raises(self):
        with self.assertRaises(ValueError):
            self.repo.export_markdown("olmayan-id")


if __name__ == "__main__":
    unittest.main()
