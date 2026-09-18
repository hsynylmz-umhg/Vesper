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

from vesper import config
from vesper.repository import SessionRepository
from vesper.session import STATUS_ACTIVE, STATUS_COMPLETED, STATUS_INTERRUPTED, SUMMARY_FINAL
from vesper.storage import Database


class FakeAudioCapture:
    """Gerçek WASAPI/pyaudiowpatch'in yerini alır.

    `confirm_stopped` (varsayılan True): stop() ve wait_until_stopped()'ın
    ne döndüreceğini kontrol eder. Testler bunu instance üzerinde
    (assistant.capture.confirm_stopped = False) değiştirerek 'üretici kesin
    olarak durduğunu kanıtlayamadı' senaryosunu deterministik şekilde
    tetikleyebilir.

    v0.3.0: set_device()/get_active_device_name()/list_available_devices()
    gerçek AudioCapture'ın cihaz seçimi yüzeyini taklit eder — WebinarAssistant
    seviyesindeki testler set_audio_device()/list_audio_devices() akışını
    bu sahte üzerinden doğrulayabilsin diye. Gerçek doğrulama/loopback
    eşleştirme mantığı burada YOKTUR — o, tests/test_audio_capture_device_
    selection.py'de gerçek AudioCapture'a karşı, mock'lanmış bir pyaudio
    ile ayrıca test edilir."""

    def __init__(self, output_queue):
        self.output_queue = output_queue
        self.started = False
        self.stop_called = False
        self.confirm_stopped = True
        self._device_index = None
        self.active_device_name = "Sahte Cihaz (test)"
        self._error = None

    def start(self):
        self.started = True

    def stop(self) -> bool:
        self.stop_called = True
        return self.confirm_stopped

    def wait_until_stopped(self, timeout: float) -> bool:
        return self.confirm_stopped

    def set_device(self, device_index):
        self._device_index = device_index

    def get_active_device_name(self):
        return self.active_device_name

    def get_error(self):
        return self._error

    @staticmethod
    def list_available_devices():
        return [{"index": 1, "name": "Sahte Loopback Cihazı"}]


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
        with patch("vesper.engine.AudioCapture", FakeAudioCapture), \
             patch("vesper.engine.Transcriber", FakeTranscriber), \
             patch("vesper.engine.Summarizer", FakeSummarizer):
            import vesper.engine as main_module

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
        with patch("vesper.engine.AudioCapture", FakeAudioCapture), \
             patch("vesper.engine.Transcriber", FakeTranscriber), \
             patch("vesper.engine.Summarizer", FakeSummarizer):
            import vesper.engine as main_module

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
        with patch("vesper.engine.AudioCapture", FakeAudioCapture), \
             patch("vesper.engine.Transcriber", FakeTranscriber), \
             patch("vesper.engine.Summarizer", FakeSummarizer):
            import vesper.engine as main_module

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
        with patch("vesper.engine.AudioCapture", FakeAudioCapture), \
             patch("vesper.engine.Transcriber", ControllableFakeTranscriber), \
             patch("vesper.engine.Summarizer", FakeSummarizer):
            import vesper.engine as main_module

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
        with patch("vesper.engine.AudioCapture", FakeAudioCapture), \
             patch("vesper.engine.Transcriber", ControllableFakeTranscriber), \
             patch("vesper.engine.Summarizer", FakeSummarizer), \
             patch("vesper.engine.TRANSCRIBE_JOIN_TIMEOUT_SEC", 0.2):
            import vesper.engine as main_module

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
        with patch("vesper.engine.AudioCapture", FakeAudioCapture), \
             patch("vesper.engine.Transcriber", FakeTranscriber), \
             patch("vesper.engine.Summarizer", FakeSummarizer), \
             patch("vesper.engine.CAPTURE_EXTRA_WAIT_SEC", 0.1):
            import vesper.engine as main_module

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
        with patch("vesper.engine.AudioCapture", FakeAudioCapture), \
             patch("vesper.engine.Transcriber", FakeTranscriber), \
             patch("vesper.engine.Summarizer", FakeSummarizer):
            import vesper.engine as main_module

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
        with patch("vesper.engine.AudioCapture", FakeAudioCapture), \
             patch("vesper.engine.Transcriber", FakeTranscriber), \
             patch("vesper.engine.Summarizer", FailingFakeSummarizer):
            import vesper.engine as main_module

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
        with patch("vesper.engine.AudioCapture", FakeAudioCapture), \
             patch("vesper.engine.Transcriber", FakeTranscriber), \
             patch("vesper.engine.Summarizer", FakeSummarizer):
            import vesper.engine as main_module

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

        with patch("vesper.engine.AudioCapture", FlakyStartCapture), \
             patch("vesper.engine.Transcriber", FakeTranscriber), \
             patch("vesper.engine.Summarizer", FakeSummarizer):
            import vesper.engine as main_module

            assistant = main_module.WebinarAssistant(repository=self.repo)

            with self.assertRaises(OSError):
                assistant.start()
            self.assertFalse(assistant._running, "capture.start() hatasından sonra _running False'a dönmeli")

            # AYNI instance, AYNI capture nesnesi — ikinci deneme başarıyla başlamalı.
            assistant.start()
            self.assertTrue(assistant._running)
            self.assertTrue(assistant.capture.started)
            assistant.stop()

    # ------------------------------------------------------------------ #
    # v0.3.0 — ses kaynağı seçimi
    # ------------------------------------------------------------------ #

    def test_list_audio_devices_delegates_to_capture(self):
        with patch("vesper.engine.AudioCapture", FakeAudioCapture), \
             patch("vesper.engine.Transcriber", FakeTranscriber), \
             patch("vesper.engine.Summarizer", FakeSummarizer):
            import vesper.engine as main_module

            assistant = main_module.WebinarAssistant(repository=self.repo)
            self.assertEqual(
                assistant.list_audio_devices(),
                [{"index": 1, "name": "Sahte Loopback Cihazı"}],
            )

    def test_set_audio_device_rejected_while_running(self):
        with patch("vesper.engine.AudioCapture", FakeAudioCapture), \
             patch("vesper.engine.Transcriber", FakeTranscriber), \
             patch("vesper.engine.Summarizer", FakeSummarizer):
            import vesper.engine as main_module

            assistant = main_module.WebinarAssistant(repository=self.repo)
            assistant.start()
            with self.assertRaises(RuntimeError):
                assistant.set_audio_device(1)
            assistant.stop()

    def test_set_audio_device_flows_into_session_source_name(self):
        """source_name UI'nin verdiği bir değer değil, ses katmanının
        get_active_device_name()'inden gelmelidir (bkz. engine.py'daki
        capture.start() -> create_session() sıralaması)."""
        with patch("vesper.engine.AudioCapture", FakeAudioCapture), \
             patch("vesper.engine.Transcriber", FakeTranscriber), \
             patch("vesper.engine.Summarizer", FakeSummarizer):
            import vesper.engine as main_module

            assistant = main_module.WebinarAssistant(repository=self.repo)
            assistant.set_audio_device(1)
            assistant.capture.active_device_name = "USB Kulaklık (Loopback)"

            assistant.start()
            session_id = assistant.session_id

            session = self.repo.get_session(session_id)
            self.assertEqual(session.source_name, "USB Kulaklık (Loopback)")
            assistant.stop()

    def test_capture_start_failure_leaves_no_orphan_session(self):
        """v0.3.0: create_session() artık capture.start()'tan SONRA çağrılıyor,
        bu yüzden capture.start() patladığında hiçbir oturum satırı yaratılmamalı
        (önceden yaratılıyordu; sonraki açılışta zararsızca 'interrupted' olarak
        kurtarılıyordu — bu test sıralamanın değiştiğini doğrudan kanıtlar)."""

        class FlakyStartCapture(FakeAudioCapture):
            def start(self):
                raise OSError("simulated device error")

        with patch("vesper.engine.AudioCapture", FlakyStartCapture), \
             patch("vesper.engine.Transcriber", FakeTranscriber), \
             patch("vesper.engine.Summarizer", FakeSummarizer):
            import vesper.engine as main_module

            assistant = main_module.WebinarAssistant(repository=self.repo)
            with self.assertRaises(OSError):
                assistant.start()
            self.assertIsNone(assistant.session_id)
            self.assertEqual(self.repo.list_sessions(), [])

    # ------------------------------------------------------------------ #
    # v0.3.0 — çalışma-zamanı (runtime) yakalama arızası yönetimi
    # ------------------------------------------------------------------ #

    def test_capture_failure_detected_while_backlog_still_processing(self):
        """Fark edilme, kuyruk BOŞALANA KADAR gecikmemeli — bir yığın (backlog)
        işlenirken bile hata zaten mevcutsa fark edilmelidir, sadece
        queue.Empty anında değil."""
        with patch("vesper.engine.AudioCapture", FakeAudioCapture), \
             patch("vesper.engine.Transcriber", FakeTranscriber), \
             patch("vesper.engine.Summarizer", FakeSummarizer), \
             patch("vesper.engine.CAPTURE_QUEUE_POLL_TIMEOUT_SEC", 0.05):
            import vesper.engine as main_module

            assistant = main_module.WebinarAssistant(repository=self.repo)
            assistant.start()

            for i in range(5):
                assistant._audio_queue.put(f"backlog-{i}")
            assistant.capture._error = OSError("simulated device loss mid-backlog")

            time.sleep(0.5)  # 5 parça (~0.1sn, FakeTranscriber başına 0.02sn) + en
                             # az bir sağlık kontrolü turu için yeterli

            self.assertEqual(
                len(self.repo.get_segments(assistant.session_id)), 5,
                "yığındaki tüm parçalar, arızaya rağmen işlenmiş olmalı",
            )
            self.assertTrue(assistant.capture_failed)
            self.assertEqual(assistant.status, "Ses kaynağı beklenmedik şekilde koptu")

            assistant.stop()

    def test_repeated_health_checks_transition_only_once(self):
        with patch("vesper.engine.AudioCapture", FakeAudioCapture), \
             patch("vesper.engine.Transcriber", FakeTranscriber), \
             patch("vesper.engine.Summarizer", FakeSummarizer), \
             patch("vesper.engine.CAPTURE_QUEUE_POLL_TIMEOUT_SEC", 0.02), \
             patch("vesper.engine.log") as mock_log:
            import vesper.engine as main_module

            assistant = main_module.WebinarAssistant(repository=self.repo)
            assistant.start()
            assistant.capture._error = OSError("simulated device loss")

            time.sleep(0.3)  # birden fazla sağlık kontrolü turu geçsin
            status_after_multiple_polls = assistant.status
            self.assertTrue(assistant.capture_failed)

            time.sleep(0.2)  # daha da fazla tur
            self.assertEqual(
                assistant.status, status_after_multiple_polls,
                "durum, ilk fark edilmeden sonra tekrar tekrar değişmemeli",
            )

            assistant.stop()

        capture_failure_log_calls = [
            c for c in mock_log.error.call_args_list
            if c.args and "çöktü" in str(c.args[0])
        ]
        self.assertEqual(
            len(capture_failure_log_calls), 1,
            "arıza sadece BİR KEZ loglanmalı, tekrar tekrar değil",
        )

    def test_stop_checks_capture_error_directly_even_if_not_yet_flagged(self):
        """stop() finalizasyon kararı için HER ZAMAN capture.get_error()'ı
        taze olarak okumalı — sadece en-iyi-çaba capture_failed bayrağına
        güvenmemeli. CAPTURE_QUEUE_POLL_TIMEOUT_SEC kasıtlı olarak UZUN
        tutulur ki arka plan sağlık kontrolü henüz hiç çalışmamış olsun."""
        with patch("vesper.engine.AudioCapture", FakeAudioCapture), \
             patch("vesper.engine.Transcriber", FakeTranscriber), \
             patch("vesper.engine.Summarizer", FakeSummarizer), \
             patch("vesper.engine.CAPTURE_QUEUE_POLL_TIMEOUT_SEC", 5.0):
            import vesper.engine as main_module

            assistant = main_module.WebinarAssistant(repository=self.repo)
            assistant.start()
            assistant._audio_queue.put("bir-parca")
            time.sleep(0.1)

            assistant.capture._error = OSError("simulated device loss")
            # Kasıtlı olarak HENÜZ beklemedik — sağlık kontrolü turu 5 saniyede
            # bir olduğu için capture_failed muhtemelen hala False.
            self.assertFalse(assistant.capture_failed)

            assistant.stop()

            session = self.repo.get_session(assistant.session_id)
            self.assertEqual(session.status, STATUS_INTERRUPTED)
            self.assertFalse(assistant.last_stop_finalized)

    def test_stop_after_capture_failure_marks_interrupted_not_completed(self):
        with patch("vesper.engine.AudioCapture", FakeAudioCapture), \
             patch("vesper.engine.Transcriber", FakeTranscriber), \
             patch("vesper.engine.Summarizer", FakeSummarizer):
            import vesper.engine as main_module

            assistant = main_module.WebinarAssistant(repository=self.repo)
            assistant.start()
            session_id = assistant.session_id

            for i in range(3):
                assistant._audio_queue.put(f"parca-{i}")
            time.sleep(0.2)  # 3 parçanın işlenmesi için yeterli

            assistant.capture._error = OSError("simulated device loss")

            final_summary = assistant.stop()

            self.assertTrue(assistant.last_stop_drained, "drain kendisi yine de tamamlanmalıydı")
            self.assertFalse(assistant.last_stop_finalized, "temiz olmayan bitim finalized=True üretmemeli")
            self.assertEqual(assistant.status, "Durduruldu (ses kaynağı koptu)")

            session = self.repo.get_session(session_id)
            self.assertEqual(session.status, STATUS_INTERRUPTED)
            self.assertIsNotNone(session.ended_at)

            segments = self.repo.get_segments(session_id)
            self.assertEqual(len(segments), 3, "arızadan ÖNCE yakalanan parçalar kalıcı kalmalı")

            final = self.repo.get_latest_summary(session_id, SUMMARY_FINAL)
            self.assertIsNotNone(final, "yakalanan kısmın final özeti yine de üretilmeli")
            self.assertIn("FINAL", final.content)
            self.assertIn("FINAL", final_summary)

            outputs = list(Path(config.OUTPUT_DIR).glob("webinar_*.md"))
            self.assertEqual(len(outputs), 1, "Markdown dışa aktarımı yine de yazılmalı")

    def test_normal_completion_leaves_no_capture_error(self):
        """Düzenli tamamlanma yolunda hiçbir regresyon olmamalı: capture
        hiç hata bildirmediyse oturum yine STATUS_COMPLETED olarak biter."""
        with patch("vesper.engine.AudioCapture", FakeAudioCapture), \
             patch("vesper.engine.Transcriber", FakeTranscriber), \
             patch("vesper.engine.Summarizer", FakeSummarizer):
            import vesper.engine as main_module

            assistant = main_module.WebinarAssistant(repository=self.repo)
            assistant.start()
            self.assertIsNone(assistant.capture.get_error())
            self.assertFalse(assistant.capture_failed)

            assistant._audio_queue.put("parca")
            assistant.stop()

            session = self.repo.get_session(assistant.session_id)
            self.assertEqual(session.status, STATUS_COMPLETED)
            self.assertTrue(assistant.last_stop_finalized)


if __name__ == "__main__":
    unittest.main()
