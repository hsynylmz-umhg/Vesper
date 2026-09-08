"""
Sahte (fake) AudioCapture / Transcriber / Summarizer ile main.WebinarAssistant'ın
GERÇEK start()/stop() akışını uçtan uca test eder — GPU, Windows veya Ollama
gerekmeden.

Asıl amaç: stop() çağrıldığı anda kuyrukta birikmiş BİRDEN FAZLA ses
parçasının kaybolmadığını doğrulamak (v0.2.0'da düzeltilen yarış durumu —
bkz. main.py'deki _transcribe_loop / stop() yorumları).

Çalıştırma:
    python -m unittest discover -s tests -t .
"""

import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# audio_capture.py and transcriber.py do module-level `import webrtcvad` /
# `import ctranslate2` / `from faster_whisper import ...`. Those packages are
# GPU/Windows-oriented and genuinely unnecessary here: the classes that use
# them (AudioCapture, Transcriber) are fully replaced by fakes below, so
# nothing from these packages is ever actually called — we only need the
# bare `import` statements in the real modules to resolve. Stubbing them in
# sys.modules avoids pulling in heavy ML/native dependencies just to satisfy
# an import line whose class we never instantiate.
if "webrtcvad" not in sys.modules:
    sys.modules["webrtcvad"] = types.ModuleType("webrtcvad")
if "ctranslate2" not in sys.modules:
    sys.modules["ctranslate2"] = types.ModuleType("ctranslate2")
if "faster_whisper" not in sys.modules:
    _fake_fw = types.ModuleType("faster_whisper")
    _fake_fw.WhisperModel = type("WhisperModel", (), {})
    _fake_fw.download_model = lambda *a, **k: None
    sys.modules["faster_whisper"] = _fake_fw

import config
from repository import SessionRepository
from session import STATUS_COMPLETED, SUMMARY_FINAL
from storage import Database


class FakeAudioCapture:
    """Gerçek WASAPI/pyaudiowpatch'in yerini alır. start()/stop() dışında
    hiçbir şey yapmaz — testler 'ses'i doğrudan queue'ya koyar."""

    def __init__(self, output_queue):
        self.output_queue = output_queue
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class FakeTranscriber:
    """Gerçek Whisper'a gerek kalmadan, verilen 'chunk'ı (burada düz string)
    olduğu gibi metne çevirir; küçük bir gecikme ile gerçekçi kılar."""

    def __init__(self):
        pass

    def transcribe(self, chunk) -> str:
        time.sleep(0.05)
        return str(chunk)


class FakeSummarizer:
    def __init__(self):
        pass

    def summarize(self, transcript: str, is_final: bool = False) -> str:
        kind = "FINAL" if is_final else "LIVE"
        return f"## Genel Özet\n\n[{kind}] {transcript}"


class TestLifecycleIntegration(unittest.TestCase):
    def setUp(self):
        self._db_tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._db_tmpdir.name) / "lifecycle_test.db"
        self.repo = SessionRepository(Database(db_path))

        # outputs/*.md hâlâ üretiliyor mu diye kontrol edeceğiz; gerçek
        # proje klasörünü kirletmemek için ayrı bir çalışma dizinine geçiyoruz.
        self._work_tmpdir = tempfile.TemporaryDirectory()
        self._orig_cwd = os.getcwd()
        os.chdir(self._work_tmpdir.name)

    def tearDown(self):
        self.repo.db.close()
        os.chdir(self._orig_cwd)
        self._db_tmpdir.cleanup()
        self._work_tmpdir.cleanup()

    def test_stop_drains_backlogged_queue_without_losing_segments(self):
        with patch("main.AudioCapture", FakeAudioCapture), \
             patch("main.Transcriber", FakeTranscriber), \
             patch("main.Summarizer", FakeSummarizer):
            import main as main_module

            assistant = main_module.WebinarAssistant(repository=self.repo)
            assistant.start()
            self.assertIsNotNone(assistant.session_id)

            # Kasıtlı olarak kuyruğa art arda 5 parça koyup HEMEN stop()
            # çağırıyoruz — eski koddaki hatanın tam olarak ortaya çıktığı
            # senaryo (transcribe thread henüz hiçbirini işlememişken durdurma).
            for i in range(5):
                assistant._audio_queue.put(f"parca-{i}")

            final_summary = assistant.stop()

            segments = self.repo.get_segments(assistant.session_id)
            self.assertEqual(len(segments), 5, "Kuyruktaki 5 parçanın tamamı işlenmeliydi")
            self.assertEqual(
                [s.text for s in segments],
                [f"parca-{i}" for i in range(5)],
                "Parçalar sırayla kalmalı",
            )

            session = self.repo.get_session(assistant.session_id)
            self.assertEqual(session.status, STATUS_COMPLETED)
            self.assertIsNotNone(session.ended_at)

            final = self.repo.get_latest_summary(assistant.session_id, SUMMARY_FINAL)
            self.assertIsNotNone(final)
            self.assertIn("FINAL", final.content)
            self.assertIn("FINAL", final_summary)

    def test_markdown_file_still_written_for_backward_compatibility(self):
        with patch("main.AudioCapture", FakeAudioCapture), \
             patch("main.Transcriber", FakeTranscriber), \
             patch("main.Summarizer", FakeSummarizer):
            import main as main_module

            assistant = main_module.WebinarAssistant(repository=self.repo)
            assistant.start()
            assistant._audio_queue.put("test cumlesi")
            assistant.stop()

            outputs = list(Path(config.OUTPUT_DIR).glob("webinar_*.md"))
            self.assertEqual(len(outputs), 1)
            content = outputs[0].read_text(encoding="utf-8")
            self.assertIn("## Genel Özet", content)
            self.assertIn("## Tam Transkript", content)
            self.assertIn("test cumlesi", content)

    def test_double_start_is_a_no_op(self):
        with patch("main.AudioCapture", FakeAudioCapture), \
             patch("main.Transcriber", FakeTranscriber), \
             patch("main.Summarizer", FakeSummarizer):
            import main as main_module

            assistant = main_module.WebinarAssistant(repository=self.repo)
            assistant.start()
            first_session_id = assistant.session_id
            assistant.start()  # ikinci çağrı hiçbir şey yapmamalı (orijinal davranış)
            self.assertEqual(assistant.session_id, first_session_id)
            assistant.stop()


if __name__ == "__main__":
    unittest.main()
