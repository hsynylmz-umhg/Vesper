"""
Sahte (fake) AudioCapture / Transcriber / Summarizer ile main.WebinarAssistant'ın
GERÇEK start()/stop() akışını uçtan uca test eder — GPU, Windows veya Ollama
gerekmeden.

Phase A (güvenli kapanış): bu dosyanın asıl amacı artık stop()'un, kuyrukta
bekleyen VE o an işlenmekte olan (in-flight) sesin tamamı bitene kadar
oturumu TAMAMLANDI işaretlemediğini kanıtlamak — sadece stop()'un sonunda
döndüğünü değil. Zamanlamaya güvenmemek için testler threading.Event ile
deterministik şekilde kontrol edilir.

Çalıştırma:
    python -m unittest discover -s tests -t .
"""

import os
import sys
import tempfile
import threading
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
from session import STATUS_ACTIVE, STATUS_COMPLETED, STATUS_INTERRUPTED, SUMMARY_FINAL
from storage import Database


class FakeAudioCapture:
    """Gerçek WASAPI/pyaudiowpatch'in yerini alır.

    `confirm_stopped` (varsayılan True): stop() ve wait_until_stopped()'ın
    ne döndüreceğini kontrol eder. Testler bunu instance üzerinde
    (assistant.capture.confirm_stopped = False) değiştirerek 'üretici kesin
    olarak durduğunu kanıtlayamadı' senaryosunu deterministik şekilde
    tetikleyebilir."""

    def __init__(self, output_queue):
        self.output_queue = output_queue
        self.started = False
        self.stop_called = False
        self.confirm_stopped = True

    def start(self):
        self.started = True

    def stop(self) -> bool:
        self.stop_called = True
        return self.confirm_stopped

    def wait_until_stopped(self, timeout: float) -> bool:
        return self.confirm_stopped


class FakeTranscriber:
    """Verilen 'chunk'ı (burada düz string) olduğu gibi metne çevirir."""

    def __init__(self):
        pass

    def transcribe(self, chunk) -> str:
        time.sleep(0.02)
        return str(chunk)


class ControllableFakeTranscriber:
    """FakeTranscriber gibi davranır, ama `block_on` ile eşleşen bir chunk'ta
    `release_event` set edilene kadar bloke olur. Bu, "stop() in-flight bir
    işi beklemeden tamamlanmamalı" gibi ırk durumlarını gerçek zamanlamaya
    güvenmeden, Event ile deterministik test etmeyi sağlar.

    main.py, Transcriber()'ı argümansız çağırdığı için bu alanlar constructor'da
    DEĞİL, instance oluşturulduktan sonra testin kendisi tarafından ayarlanır
    (bkz. assistant.transcriber.block_on = ...)."""

    def __init__(self):
        self.block_on = None
        self.release_event = None
        self.reached_block = threading.Event()

    def transcribe(self, chunk) -> str:
        if self.block_on is not None and chunk == self.block_on:
            self.reached_block.set()
            self.release_event.wait()
        return str(chunk)


class FakeSummarizer:
    def __init__(self):
        pass

    def summarize(self, transcript: str, is_final: bool = False) -> str:
        kind = "FINAL" if is_final else "LIVE"
        return f"## Genel Özet\n\n[{kind}] {transcript}"


class FailingFakeSummarizer:
    def __init__(self):
        pass

    def summarize(self, transcript: str, is_final: bool = False) -> str:
        raise RuntimeError("simulated Ollama failure")


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
        import gc
        self.repo.close_thread_connection()
        self.repo.db.close()
        gc.collect()
        os.chdir(self._orig_cwd)
        self._db_tmpdir.cleanup()
        self._work_tmpdir.cleanup()

    # ------------------------------------------------------------------ #
    # Mevcut testler (davranış aynı, artık last_stop_* de doğrulanıyor)
    # ------------------------------------------------------------------ #

    def test_stop_drains_backlogged_queue_without_losing_segments(self):
        with patch("main.AudioCapture", FakeAudioCapture), \
             patch("main.Transcriber", FakeTranscriber), \
             patch("main.Summarizer", FakeSummarizer):
            import main as main_module

            assistant = main_module.WebinarAssistant(repository=self.repo)
            assistant.start()
            self.assertIsNotNone(assistant.session_id)

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

            self.assertTrue(assistant.last_stop_drained)
            self.assertTrue(assistant.last_stop_finalized)

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
            assistant.start()
            self.assertEqual(assistant.session_id, first_session_id)
            assistant.stop()

    # ------------------------------------------------------------------ #
    # Phase A — yeni testler
    # ------------------------------------------------------------------ #

    def test_stop_waits_for_in_flight_chunk_before_completing(self):
        """Asıl v0.2 güvencesi: stop() çağrıldığı anda bir chunk hâlâ
        transkripsiyon halindeyken (kuyruktan çıkmış ama bitmemiş), stop()
        o iş bitmeden ne oturumu TAMAMLANDI işaretleyebilir ne de dönebilir."""
        with patch("main.AudioCapture", FakeAudioCapture), \
             patch("main.Transcriber", ControllableFakeTranscriber), \
             patch("main.Summarizer", FakeSummarizer):
            import main as main_module

            assistant = main_module.WebinarAssistant(repository=self.repo)
            release_event = threading.Event()
            assistant.transcriber.block_on = "SLOW"
            assistant.transcriber.release_event = release_event

            assistant.start()
            session_id = assistant.session_id
            self.assertIsNotNone(session_id)

            for item in ("once-1", "once-2", "SLOW", "sonra-1", "sonra-2"):
                assistant._audio_queue.put(item)

            self.assertTrue(
                assistant.transcriber.reached_block.wait(timeout=5),
                "İşçi 5 saniye içinde SLOW parçasına ulaşmadı",
            )

            result = {}
            def do_stop():
                try:
                    result["summary"] = assistant.stop()
                finally:
                    self.repo.close_thread_connection()

            stop_thread = threading.Thread(target=do_stop)
            stop_thread.start()

            # stop() SLOW hâlâ bloke olmuşken kesinlikle dönmemeli ve oturum
            # kesinlikle 'active' kalmalı — asıl kanıtlanması gereken şey bu.
            time.sleep(0.3)
            self.assertTrue(stop_thread.is_alive(), "stop() SLOW bitmeden dönmemeliydi")
            self.assertEqual(self.repo.get_session(session_id).status, STATUS_ACTIVE)

            release_event.set()
            stop_thread.join(timeout=10)
            self.assertFalse(stop_thread.is_alive(), "stop() serbest bırakıldıktan sonra zamanında dönmeliydi")

            expected = ["once-1", "once-2", "SLOW", "sonra-1", "sonra-2"]
            segments = self.repo.get_segments(session_id)
            self.assertEqual([s.text for s in segments], expected)

            session = self.repo.get_session(session_id)
            self.assertEqual(session.status, STATUS_COMPLETED)
            self.assertTrue(assistant.last_stop_drained)
            self.assertTrue(assistant.last_stop_finalized)

            final = self.repo.get_latest_summary(session_id, SUMMARY_FINAL)
            self.assertIsNotNone(final)
            for text in expected:
                self.assertIn(text, final.content)
                self.assertIn(text, result["summary"])

            markdown = self.repo.export_markdown(session_id)
            for text in expected:
                self.assertIn(text, markdown)

    def test_stop_reports_incomplete_drain_on_timeout_without_leaking_worker(self):
        with patch("main.AudioCapture", FakeAudioCapture), \
             patch("main.Transcriber", ControllableFakeTranscriber), \
             patch("main.Summarizer", FakeSummarizer), \
             patch("main.TRANSCRIBE_JOIN_TIMEOUT_SEC", 0.2):
            import main as main_module

            assistant = main_module.WebinarAssistant(repository=self.repo)
            release_event = threading.Event()
            assistant.transcriber.block_on = "STUCK"
            assistant.transcriber.release_event = release_event

            assistant.start()
            session_id = assistant.session_id
            assistant._audio_queue.put("STUCK")
            self.assertTrue(assistant.transcriber.reached_block.wait(timeout=5))

            final_summary = assistant.stop()

            self.assertFalse(assistant.last_stop_drained)
            self.assertIsNone(assistant.last_stop_finalized, "finalizasyon adımına hiç girilmemeliydi")
            self.assertEqual(self.repo.get_session(session_id).status, STATUS_ACTIVE)
            self.assertIsNone(self.repo.get_latest_summary(session_id, SUMMARY_FINAL))
            self.assertEqual(final_summary, assistant.live_summary)

            # İşçiyi sonsuza kadar canlı BIRAKMA: serbest bırak, gerçekten
            # çıktığını kanıtla (Windows'ta SQLite/temp dizin temizliği için önemli).
            release_event.set()
            assistant._transcribe_thread.join(timeout=10)
            self.assertFalse(assistant._transcribe_thread.is_alive(),
                              "İşçi serbest bırakıldıktan sonra çıkmalıydı")

            recovered = self.repo.recover_interrupted_sessions()
            self.assertIn(session_id, recovered)
            self.assertEqual(self.repo.get_session(session_id).status, STATUS_INTERRUPTED)

    def test_stop_does_not_enqueue_sentinel_when_capture_producer_not_confirmed_dead(self):
        """Asıl düzeltilen yarış: capture.stop() üreticinin öldüğünü
        kanıtlayamazsa, sentinel HİÇ konulmamalı — aksi halde AudioCapture'ın
        finally bloğundaki son ses parçası sentinel'den sonra gelip hiç
        işlenmeyebilirdi."""
        with patch("main.AudioCapture", FakeAudioCapture), \
             patch("main.Transcriber", FakeTranscriber), \
             patch("main.Summarizer", FakeSummarizer), \
             patch("main.CAPTURE_EXTRA_WAIT_SEC", 0.1):
            import main as main_module

            assistant = main_module.WebinarAssistant(repository=self.repo)
            assistant.capture.confirm_stopped = False  # stop() VE wait_until_stopped() False dönecek

            assistant.start()
            session_id = assistant.session_id
            assistant._audio_queue.put("bir-parca")

            final_summary = assistant.stop()

            self.assertFalse(assistant.last_stop_drained)
            self.assertIsNone(assistant.last_stop_finalized)
            self.assertEqual(self.repo.get_session(session_id).status, STATUS_ACTIVE)
            self.assertEqual(final_summary, assistant.live_summary)

            # Asıl kanıt: işçi HÂLÂ CANLI olmalı. "bir-parca" start() sonrası
            # kuyruğa konduğu için işçi onu zaten işlemiş olabilir (zamanlamaya
            # bağlı) — asıl önemli olan şey bu değil. Önemli olan: sentinel
            # HİÇ konulmadıysa işçi boş kuyrukta .get() ile bloke kalır ve
            # kesinlikle çıkamaz.
            self.assertTrue(
                assistant._transcribe_thread.is_alive(),
                "İşçi hâlâ canlı olmalı — sentinel konulmadıysa çıkamaz",
            )

            # İşçiyi temizle: test kendisi sentinel'i koyup bitmesini bekler
            # (main.py bunu YAPMADI — bu testin doğruladığı şey tam olarak bu).
            assistant._audio_queue.put(main_module._TRANSCRIBE_SENTINEL)
            assistant._transcribe_thread.join(timeout=10)
            self.assertFalse(assistant._transcribe_thread.is_alive())

    def test_stop_blocks_completion_on_persistence_gap(self):
        with patch("main.AudioCapture", FakeAudioCapture), \
             patch("main.Transcriber", FakeTranscriber), \
             patch("main.Summarizer", FakeSummarizer):
            import main as main_module

            assistant = main_module.WebinarAssistant(repository=self.repo)
            assistant.start()
            session_id = assistant.session_id

            original_append = self.repo.append_segment
            call_count = {"n": 0}

            def flaky_append(*args, **kwargs):
                call_count["n"] += 1
                if call_count["n"] == 2:
                    raise RuntimeError("simulated disk hiccup")
                return original_append(*args, **kwargs)

            assistant.repository.append_segment = flaky_append

            assistant._audio_queue.put("birinci")
            assistant._audio_queue.put("ikinci")  # bu segment kaydedilemeyecek
            assistant._audio_queue.put("ucuncu")

            final_summary = assistant.stop()

            self.assertTrue(assistant.last_stop_drained, "transkripsiyonun kendisi yine de bitmeliydi")
            self.assertFalse(assistant.last_stop_finalized, "eksik kayıtla TAMAMLANDI işaretlenmemeli")
            self.assertEqual(self.repo.get_session(session_id).status, STATUS_ACTIVE)
            self.assertIsNone(self.repo.get_latest_summary(session_id, SUMMARY_FINAL))
            self.assertEqual(final_summary, assistant.live_summary)

    def test_stop_blocks_completion_on_summarizer_failure(self):
        with patch("main.AudioCapture", FakeAudioCapture), \
             patch("main.Transcriber", FakeTranscriber), \
             patch("main.Summarizer", FailingFakeSummarizer):
            import main as main_module

            assistant = main_module.WebinarAssistant(repository=self.repo)
            assistant.start()
            session_id = assistant.session_id
            assistant._audio_queue.put("bir-parca")

            final_summary = assistant.stop()

            self.assertTrue(assistant.last_stop_drained)
            self.assertFalse(assistant.last_stop_finalized)
            self.assertEqual(self.repo.get_session(session_id).status, STATUS_ACTIVE)
            self.assertEqual(final_summary, assistant.live_summary)

    def test_stop_does_not_claim_finalized_when_session_id_is_none(self):
        """create_session() başarısız olup session_id=None kaldığında (bkz.
        start()), bellekten özet/Markdown üretimi (geriye dönük uyumluluk)
        ÇALIŞMAYA devam etmeli, ama last_stop_finalized asla True olmamalı —
        UI bunu 'kalıcı oturum tamamlandı' gibi göstermemeli."""
        with patch("main.AudioCapture", FakeAudioCapture), \
             patch("main.Transcriber", FakeTranscriber), \
             patch("main.Summarizer", FakeSummarizer):
            import main as main_module

            assistant = main_module.WebinarAssistant(repository=self.repo)

            def always_fail(*a, **kw):
                raise RuntimeError("simulated SQLite unavailable at session start")
            assistant.repository.create_session = always_fail

            assistant.start()
            self.assertIsNone(assistant.session_id)

            assistant._audio_queue.put("hafizadan ozet")
            final_summary = assistant.stop()

            self.assertTrue(assistant.last_stop_drained)
            self.assertFalse(assistant.last_stop_finalized, "session_id=None asla finalized=True üretmemeli")
            self.assertIn("FINAL", final_summary)  # bellekten özet yine de üretildi

            outputs = list(Path(config.OUTPUT_DIR).glob("webinar_*.md"))
            self.assertEqual(len(outputs), 1, "Markdown dosyası yine de (yedek biçimde) yazılmalı")

    def test_start_raises_and_recovers_when_capture_start_fails(self):
        """capture.start() (ör. WASAPI cihaz hatası) patlarsa, instance
        KALICI OLARAK kilitlenmemeli — _running geri alınmalı ki AYNI
        instance üzerinde bir sonraki start() denemesi gerçekten çalışabilsin.

        NOT: self.capture __init__'te BİR KEZ oluşturulur ve hiç
        değiştirilmez, bu yüzden burada 'ikinci patch bloğu' değil, TEK bir
        capture instance'ının ilk çağrıda patlayıp ikinci çağrıda başarılı
        olduğu durum-tutan (stateful) bir sahte kullanılıyor."""

        class FlakyStartCapture(FakeAudioCapture):
            def __init__(self, output_queue):
                super().__init__(output_queue)
                self._start_attempts = 0

            def start(self):
                self._start_attempts += 1
                if self._start_attempts == 1:
                    raise OSError("simulated WASAPI device error")
                super().start()

        with patch("main.AudioCapture", FlakyStartCapture), \
             patch("main.Transcriber", FakeTranscriber), \
             patch("main.Summarizer", FakeSummarizer):
            import main as main_module

            assistant = main_module.WebinarAssistant(repository=self.repo)

            with self.assertRaises(OSError):
                assistant.start()
            self.assertFalse(assistant._running, "capture.start() hatasından sonra _running False'a dönmeli")

            # AYNI instance, AYNI capture nesnesi — ikinci deneme başarıyla başlamalı.
            assistant.start()
            self.assertTrue(assistant._running)
            self.assertTrue(assistant.capture.started)
            assistant.stop()


if __name__ == "__main__":
    unittest.main()