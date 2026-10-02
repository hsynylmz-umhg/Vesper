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

import ast
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
# Modül import zamanında (testler os.chdir ile geçici dizine geçmeden ÖNCE) sabitlenir.
_VESPER_PACKAGE_DIR = os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")), "vesper")

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
from vesper.session import STATUS_ACTIVE, STATUS_COMPLETED, STATUS_INTERRUPTED, SUMMARY_FINAL, SUMMARY_LIVE
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
    """v0.3.0: gerçek Summarizer'ın profil yüzeyini (list_profiles /
    get_active_profile_key / get_active_model / set_profile) taklit eder —
    HTTP/Ollama YOK. Motor bu yüzeyi (1) LLM profil facade'ı ve (2)
    summaries.model kaydı için kullanır; gerçek doğrulama mantığı
    tests/test_summarizer_profiles.py'de gerçek Summarizer'a karşı test edilir."""

    def __init__(self):
        self._profiles = [
            {"key": "fast", "label": "Hızlı", "model": "fake-fast:1b"},
            {"key": "quality", "label": "Kaliteli", "model": "fake-quality:1b"},
        ]
        self._active_key = "quality"

    def list_profiles(self):
        return [dict(profile) for profile in self._profiles]

    def get_active_profile_key(self) -> str:
        return self._active_key

    def get_active_model(self) -> str:
        return next(p["model"] for p in self._profiles if p["key"] == self._active_key)

    def set_profile(self, key: str) -> None:
        if key not in {p["key"] for p in self._profiles}:
            raise ValueError(f"Bilinmeyen profil: {key!r}")
        self._active_key = key

    def summarize(self, transcript: str, is_final: bool = False) -> str:
        kind = "FINAL" if is_final else "LIVE"
        return f"## Genel Özet\n\n[{kind}] {transcript}"


class FailingFakeSummarizer(FakeSummarizer):

    def summarize(self, transcript: str, is_final: bool = False) -> str:
        raise RuntimeError("simulated Ollama failure")


class RejectingFakeSummarizer(FakeSummarizer):
    """set_profile() çağrısını her zaman reddeden özetleyici (ör. hedef model
    Ollama'da yüklü değil). Durumu DEĞİŞTİRMEDEN RuntimeError fırlatır."""

    def set_profile(self, key: str) -> None:
        raise RuntimeError("model Ollama'da yüklü değil (simulated)")


class ScriptedLiveSummarizer(FakeSummarizer):
    """Canlı özet (is_final=False) çağrılarının sonucunu betikler; final özet
    çağrıları HER ZAMAN başarılıdır.

    live_outcomes: her canlı çağrı için sırayla 'ok' | 'fail' | 'block' |
    'block_fail' (liste tükenirse 'ok'). 'block', çağrıyı release_event set
    edilene kadar bekletir, SONRA normal döner — böylece thread testleri,
    işçinin önceki turları TAM işleyip sonraki turda takıldığı bir anı
    Event ile deterministik yakalayabilir. 'block_fail' AYNI şekilde bekler
    ama SONRA FIRLATIR — 'geciken bir tur, serbest bırakıldığında ARIZA ile
    döner' senaryosunu deterministik üretmek için (bkz. jenerasyon-sahipliği
    testleri). live_call_events[i], (i+1). canlı çağrının BAŞINDA set edilir.
    on_live_call: her canlı çağrının başında (arıza/bloklama öncesi)
    çağrılan isteğe bağlı yan etki (ör. 'stop() bu sırada başladı')"""

    def __init__(self):
        super().__init__()
        self.live_outcomes = []
        self.live_calls = 0
        self.live_call_events = [threading.Event() for _ in range(8)]
        self.release_event = threading.Event()
        self.on_live_call = None

    def summarize(self, transcript: str, is_final: bool = False) -> str:
        if is_final:
            return super().summarize(transcript, is_final=True)

        self.live_calls += 1
        call_no = self.live_calls
        if call_no <= len(self.live_call_events):
            self.live_call_events[call_no - 1].set()
        if self.on_live_call is not None:
            self.on_live_call()

        outcome = self.live_outcomes.pop(0) if self.live_outcomes else "ok"
        if outcome == "fail":
            raise RuntimeError("simulated live Ollama failure")
        if outcome == "block":
            self.release_event.wait(timeout=30)
        if outcome == "block_fail":
            self.release_event.wait(timeout=30)
            raise RuntimeError("simulated live Ollama failure (after unblocking)")
        return f"[LIVE#{call_no}] {transcript}"


class _PausableLock:
    """threading.Lock etrafında ince bir sarmalayıcı: yalnızca
    test_ownership_boundary_is_atomic_never_partially_applied için,
    assistant._session_generation_lock'ın yerine GEÇİCİ olarak takılır.

    Amaç: 'A tam olarak yayınlama sınırında, kilit ZATEN alınmış, kritik
    bölgenin gövdesi henüz çalışmaya BAŞLAMAMIŞKEN' anını yakalayıp testin
    kontrollü olarak duraklatmasını sağlamak — böylece test, BAŞKA bir iş
    parçacığının (ör. B'nin start()'ı) AYNI GERÇEK kilide GERÇEKTEN bloke
    olduğunu (sadece 'henüz çalışmamış' değil) kanıtlayabilir. `arm_pause()`
    yalnızca BİR SONRAKİ __enter__ çağrısını duraklatır (tek atımlık);
    sonraki çağrılar (ör. start()'ın kendi geçişi) normal, duraklamasız
    ilerler."""

    def __init__(self, real_lock: threading.Lock):
        self._real_lock = real_lock
        self._pause_next = False
        self.holder_ready = threading.Event()
        self.release_holder = threading.Event()

    def arm_pause(self):
        self._pause_next = True

    def __enter__(self):
        acquired = self._real_lock.acquire(timeout=30)
        if not acquired:
            raise AssertionError("gerçek kilit 30 sn içinde alınamadı (olası kilitlenme)")
        if self._pause_next:
            self._pause_next = False
            self.holder_ready.set()
            self.release_holder.wait(timeout=10)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._real_lock.release()
        return False


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

    # ------------------------------------------------------------------ #
    # v0.3.0 Checkpoint B — LLM profil facade'ı, summaries.model, canlı özet
    # işçisi dayanıklılığı. Gerçek Ollama HTTP'si YOK; tüm özetleyiciler sahte.
    # ------------------------------------------------------------------ #

    def _new_assistant(self, summarizer_cls=FakeSummarizer):
        """Sahte donanım/özetleyici ile bir WebinarAssistant kurar. Yamalar
        test bitince otomatik kaldırılır (addCleanup)."""
        for target, replacement in (
            ("vesper.engine.AudioCapture", FakeAudioCapture),
            ("vesper.engine.Transcriber", FakeTranscriber),
            ("vesper.engine.Summarizer", summarizer_cls),
        ):
            patcher = patch(target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        import vesper.engine as main_module

        return main_module.WebinarAssistant(repository=self.repo)

    def _start_with_scripted(self, *outcomes):
        """ScriptedLiveSummarizer ile oturumu başlatır ve bellekteki transkripti
        doldurur (canlı özet turu boş transkriptte hiçbir şey yapmaz)."""
        assistant = self._new_assistant(ScriptedLiveSummarizer)
        assistant.summarizer.live_outcomes.extend(outcomes)
        assistant.start()
        assistant.full_transcript = ["merhaba dunya"]
        return assistant

    def _patch_engine_log(self):
        """Beklenen arıza testlerinde traceback gürültüsünü bastırır ve loglamayı doğrulanabilir kılar."""
        patcher = patch("vesper.engine.log")
        mock_log = patcher.start()
        self.addCleanup(patcher.stop)
        return mock_log

    def _wait_for_summary_row(self, session_id, kind, timeout=5):
        """self.repository.save_summary() bir işçi thread'inde, kilit
        SERBEST BIRAKILDIKTAN SONRA çalışır — bunu ana test thread'inin
        yaptığı HERHANGİ BİR şeye (aynı işçinin thread'ine join() dahil,
        çünkü FARKLI bir thread'in yazımı hâlâ sürmekte olabilir) bağlayan
        bir happens-before kenarı YOKTUR. Bellek-içi bir alanı veya başka
        bir thread'in join()'unu değil, SATIRIN KENDİSİNİ yokla."""
        deadline = time.time() + timeout
        row = None
        while row is None and time.time() < deadline:
            row = self.repo.get_latest_summary(session_id, kind)
            if row is None:
                time.sleep(0.05)
        return row

    @staticmethod
    def _stop_workers_without_stop(assistant):
        """Testin kendisi _running'i False'a çektiyse stop() bir şey yapmaz;
        canlı kalan transkripsiyon işçisini sentinel ile temizler."""
        import vesper.engine as main_module

        assistant._audio_queue.put(main_module._TRANSCRIBE_SENTINEL)
        assistant._transcribe_thread.join(timeout=10)

    CAPTURE_LOST_STATUS = "Ses kaynağı beklenmedik şekilde koptu"

    def _observe_capture_failure(self, assistant):
        """Yakalama işçisinin çöktüğünü ve motorun bunu GÖZLEMLEDİĞİNİ
        (capture_failed=True, durum = arıza metni) gerçek
        _sync_capture_failure_state() ile deterministik olarak üretir."""
        assistant.capture._error = OSError("simulated device loss")
        assistant._sync_capture_failure_state()

    def test_llm_profile_facade_lists_profiles_and_reports_active_key(self):
        assistant = self._new_assistant()
        self.assertEqual(
            [profile["key"] for profile in assistant.list_llm_profiles()], ["fast", "quality"]
        )
        self.assertEqual(assistant.get_active_llm_profile_key(), "quality")

    def test_set_llm_profile_changes_active_profile_while_idle(self):
        assistant = self._new_assistant()
        assistant.set_llm_profile("fast")
        self.assertEqual(assistant.get_active_llm_profile_key(), "fast")
        assistant.set_llm_profile("quality")
        self.assertEqual(assistant.get_active_llm_profile_key(), "quality")

    def test_set_llm_profile_rejected_while_running_then_allowed_after_stop(self):
        assistant = self._new_assistant()
        assistant.start()
        with self.assertRaises(RuntimeError):
            assistant.set_llm_profile("fast")
        self.assertEqual(assistant.get_active_llm_profile_key(), "quality")

        assistant.stop()
        assistant.set_llm_profile("fast")
        self.assertEqual(assistant.get_active_llm_profile_key(), "fast")

    def test_set_llm_profile_failure_is_propagated_and_leaves_profile_unchanged(self):
        assistant = self._new_assistant(RejectingFakeSummarizer)
        with self.assertRaises(RuntimeError):
            assistant.set_llm_profile("fast")
        self.assertEqual(assistant.get_active_llm_profile_key(), "quality")

    def test_final_summary_records_default_profile_model(self):
        assistant = self._new_assistant()
        assistant.start()
        session_id = assistant.session_id
        assistant._audio_queue.put("parca")
        assistant.stop()

        final = self.repo.get_latest_summary(session_id, SUMMARY_FINAL)
        self.assertIsNotNone(final)
        self.assertEqual(final.model, "fake-quality:1b")

    def test_final_summary_records_actual_selected_model(self):
        assistant = self._new_assistant()
        assistant.set_llm_profile("fast")
        assistant.start()
        session_id = assistant.session_id
        assistant._audio_queue.put("parca")
        assistant.stop()

        final = self.repo.get_latest_summary(session_id, SUMMARY_FINAL)
        self.assertIsNotNone(final)
        self.assertEqual(final.model, "fake-fast:1b")
        self.assertTrue(assistant.last_stop_finalized)
        self.assertEqual(self.repo.get_session(session_id).status, STATUS_COMPLETED)

    def test_live_summary_records_actual_selected_model(self):
        assistant = self._new_assistant(ScriptedLiveSummarizer)
        assistant.set_llm_profile("fast")
        assistant.start()
        session_id = assistant.session_id
        assistant.full_transcript = ["merhaba dunya"]

        assistant._run_live_summary_once()

        live = self.repo.get_latest_summary(session_id, SUMMARY_LIVE)
        self.assertIsNotNone(live)
        self.assertEqual(live.model, "fake-fast:1b")
        self.assertIn("merhaba dunya", live.content)
        assistant.stop()

    def test_live_summary_failure_keeps_previous_summary_and_status_recovers(self):
        mock_log = self._patch_engine_log()
        assistant = self._start_with_scripted("ok", "fail")
        session_id = assistant.session_id

        assistant._run_live_summary_once()
        first_summary = assistant.live_summary
        self.assertIn("LIVE#1", first_summary)
        self.assertEqual(assistant.status, "Dinleniyor")

        assistant._run_live_summary_once()  # simüle Ollama arızası — istisna SIZMAMALI

        self.assertTrue(mock_log.exception.called, "arıza loglanmalı")
        self.assertEqual(
            assistant.live_summary, first_summary, "önceki BAŞARILI canlı özet korunmalı"
        )
        self.assertEqual(assistant.status, "Dinleniyor", "oturum sürüyorsa durum geri dönmeli")
        self.assertEqual(
            self.repo.get_latest_summary(session_id, SUMMARY_LIVE).content, first_summary,
            "arızalı tur kalıcı depoya hiçbir şey yazmamalı",
        )
        self.assertEqual(assistant.live_summary_failure_episode, 1)
        assistant.stop()

    def test_first_failure_increments_episode_once_and_repeated_failures_do_not(self):
        self._patch_engine_log()
        assistant = self._start_with_scripted("fail", "fail", "fail")
        self.assertEqual(assistant.live_summary_failure_episode, 0)

        assistant._run_live_summary_once()
        self.assertEqual(assistant.live_summary_failure_episode, 1)
        self.assertTrue(assistant._live_summary_failing)

        assistant._run_live_summary_once()
        assistant._run_live_summary_once()
        self.assertEqual(
            assistant.live_summary_failure_episode, 1,
            "aynı arıza döneminde tekrar eden arızalar sayacı artırmamalı",
        )
        assistant.stop()

    def test_success_closes_failure_episode_and_later_failure_opens_a_new_one(self):
        self._patch_engine_log()
        assistant = self._start_with_scripted("fail", "ok", "fail", "fail")

        assistant._run_live_summary_once()  # arıza -> dönem 1
        self.assertEqual(assistant.live_summary_failure_episode, 1)

        assistant._run_live_summary_once()  # başarı -> dönem kapanır
        self.assertFalse(assistant._live_summary_failing)
        self.assertEqual(
            assistant.live_summary_failure_episode, 1, "sayaç monoton: başarı onu SIFIRLAMAZ"
        )

        assistant._run_live_summary_once()  # bağımsız yeni arıza -> dönem 2
        self.assertEqual(assistant.live_summary_failure_episode, 2)

        assistant._run_live_summary_once()  # aynı yeni dönem
        self.assertEqual(assistant.live_summary_failure_episode, 2)
        assistant.stop()

    def test_new_session_keeps_monotonic_counter_and_clears_only_failing_flag(self):
        self._patch_engine_log()
        assistant = self._start_with_scripted("fail")
        assistant._run_live_summary_once()
        self.assertEqual(assistant.live_summary_failure_episode, 1)
        self.assertTrue(assistant._live_summary_failing)
        assistant.stop()

        assistant.summarizer.live_outcomes.append("fail")
        assistant.start()
        self.assertEqual(
            assistant.live_summary_failure_episode, 1, "start() sayacı SIFIRLAMAMALI"
        )
        self.assertFalse(
            assistant._live_summary_failing,
            "start() yalnızca 'şu an arızalı mı' bayrağını temizlemeli",
        )

        assistant.full_transcript = ["ikinci oturum"]
        assistant._run_live_summary_once()
        self.assertEqual(
            assistant.live_summary_failure_episode, 2,
            "yeni oturumdaki bağımsız arıza sayacı yeniden artırmalı",
        )
        assistant.stop()

    def test_live_summary_status_not_restored_after_stop_began_on_success_or_failure(self):
        self._patch_engine_log()
        for outcome in ("ok", "fail"):
            with self.subTest(outcome=outcome):
                assistant = self._start_with_scripted(outcome)

                def stop_begins_during_summarize():
                    assistant._running = False
                    assistant.status = "Final özet hazırlanıyor..."

                assistant.summarizer.on_live_call = stop_begins_during_summarize
                assistant._run_live_summary_once()

                self.assertEqual(
                    assistant.status, "Final özet hazırlanıyor...",
                    "stop() başladıktan sonra biten canlı özet durumu EZMEMELİ",
                )
                if outcome == "ok":
                    self.assertIn("LIVE#1", assistant.live_summary)
                else:
                    self.assertEqual(assistant.live_summary_failure_episode, 1)
                self._stop_workers_without_stop(assistant)
                self.assertFalse(assistant._transcribe_thread.is_alive())

    def test_live_summary_progress_status_is_announced_when_capture_is_healthy(self):
        """Kontrol testi: capture_failed=False iken davranış DEĞİŞMEDİ — tur
        başında 'Canlı özet güncelleniyor...', sonunda 'Dinleniyor'."""
        assistant = self._start_with_scripted("ok")
        seen_during_call = []
        assistant.summarizer.on_live_call = lambda: seen_during_call.append(assistant.status)

        assistant._run_live_summary_once()

        self.assertEqual(seen_during_call, ["Canlı özet güncelleniyor..."])
        self.assertEqual(assistant.status, "Dinleniyor")
        assistant.stop()

    def test_live_summary_success_does_not_overwrite_capture_failure_status(self):
        """Ses kaynağı koptuktan sonra biten canlı özet (kullanıcı henüz Durdur'a
        basmadı: _running hâlâ True) arıza durumunu 'Dinleniyor' ile EZMEMELİ.
        İki zamanlama: arıza tur BAŞLAMADAN önce / tur SÜRERKEN gözlemlenir."""
        self._patch_engine_log()
        for timing in ("before_attempt", "during_attempt"):
            with self.subTest(timing=timing):
                assistant = self._start_with_scripted("ok")
                session_id = assistant.session_id
                seen_during_call = []

                if timing == "before_attempt":
                    self._observe_capture_failure(assistant)
                    assistant.summarizer.on_live_call = (
                        lambda: seen_during_call.append(assistant.status)
                    )
                else:
                    def capture_lost_mid_summarize():
                        seen_during_call.append(assistant.status)
                        self._observe_capture_failure(assistant)
                    assistant.summarizer.on_live_call = capture_lost_mid_summarize

                assistant._run_live_summary_once()

                self.assertTrue(assistant.capture_failed)
                self.assertTrue(assistant._running, "kullanıcı henüz Durdur'a basmadı")
                self.assertEqual(assistant.status, self.CAPTURE_LOST_STATUS)
                if timing == "before_attempt":
                    self.assertEqual(
                        seen_during_call, [self.CAPTURE_LOST_STATUS],
                        "özeti duyurmak için arıza durumu ezilmemeli",
                    )
                else:
                    self.assertEqual(seen_during_call, ["Canlı özet güncelleniyor..."])

                # Özetin kendisi zaten yakalanmış transkriptten üretildi: kabul + kalıcı yazım.
                self.assertIn("LIVE#1", assistant.live_summary)
                self.assertEqual(
                    self.repo.get_latest_summary(session_id, SUMMARY_LIVE).content,
                    assistant.live_summary,
                )
                self.assertFalse(assistant._live_summary_failing)
                self.assertEqual(assistant.live_summary_failure_episode, 0)
                assistant.stop()

    def test_live_summary_failure_does_not_overwrite_capture_failure_status(self):
        """Aynı değişmez, ARIZA yolu için: önceki başarılı canlı özet korunur,
        arıza-dönemi mantığı DEĞİŞMEZ, ama arıza durumu 'Dinleniyor' ile ezilmez."""
        self._patch_engine_log()
        for timing in ("before_attempt", "during_attempt"):
            with self.subTest(timing=timing):
                assistant = self._start_with_scripted("ok", "fail")

                assistant._run_live_summary_once()  # 1. tur: başarılı
                first_summary = assistant.live_summary
                self.assertIn("LIVE#1", first_summary)
                self.assertEqual(assistant.status, "Dinleniyor")
                self.assertEqual(assistant.live_summary_failure_episode, 0)

                seen_during_call = []
                if timing == "before_attempt":
                    self._observe_capture_failure(assistant)
                    assistant.summarizer.on_live_call = (
                        lambda: seen_during_call.append(assistant.status)
                    )
                else:
                    def capture_lost_mid_summarize():
                        seen_during_call.append(assistant.status)
                        self._observe_capture_failure(assistant)
                    assistant.summarizer.on_live_call = capture_lost_mid_summarize

                assistant._run_live_summary_once()  # 2. tur: ARIZA

                self.assertTrue(assistant.capture_failed)
                self.assertTrue(assistant._running, "kullanıcı henüz Durdur'a basmadı")
                self.assertEqual(assistant.status, self.CAPTURE_LOST_STATUS)
                if timing == "before_attempt":
                    self.assertEqual(seen_during_call, [self.CAPTURE_LOST_STATUS])
                else:
                    self.assertEqual(seen_during_call, ["Canlı özet güncelleniyor..."])

                self.assertEqual(
                    assistant.live_summary, first_summary,
                    "önceki BAŞARILI canlı özet korunmalı",
                )
                self.assertEqual(
                    assistant.live_summary_failure_episode, 1,
                    "arıza-dönemi mantığı değişmemeli",
                )
                self.assertTrue(assistant._live_summary_failing)
                assistant.stop()

    def test_live_summary_round_does_nothing_once_stop_has_begun(self):
        assistant = self._start_with_scripted("ok")
        assistant._running = False
        assistant.status = "Final özet hazırlanıyor..."

        assistant._run_live_summary_once()

        self.assertEqual(assistant.summarizer.live_calls, 0)
        self.assertEqual(assistant.status, "Final özet hazırlanıyor...")
        self.assertEqual(assistant.live_summary, "")
        self._stop_workers_without_stop(assistant)
        self.assertFalse(assistant._transcribe_thread.is_alive())

    def test_live_summary_worker_survives_failure_and_retries_next_interval(self):
        """Gerçek _summarize_loop iş parçacığı: ilk canlı özet ARIZA verse de
        işçi ölmemeli, sonraki aralıkta yeniden denemeli. Zamanlamaya değil
        Event'lere güvenilir: 3. tur 'block' ile bloke edilir, böylece 1. (arıza)
        ve 2. (başarı) turların TAM işlendiği an deterministik yakalanır.
        Aralık TAM SAYI 1 sn'ye çekilir (kesirli/sıfır DEĞİL)."""
        self._patch_engine_log()
        assistant = self._new_assistant(ScriptedLiveSummarizer)
        summarizer = assistant.summarizer
        summarizer.live_outcomes.extend(["fail", "ok", "block"])

        with patch.object(config, "LIVE_SUMMARY_INTERVAL_SEC", 1):
            assistant.start()
            session_id = assistant.session_id
            assistant.full_transcript = ["merhaba dunya"]

            self.assertTrue(
                summarizer.live_call_events[2].wait(timeout=20),
                "işçi 3. canlı özet turuna ulaşmadı — ilk arızada ölmüş olabilir",
            )
            self.assertTrue(assistant._summarize_thread.is_alive())
            self.assertIn("LIVE#2", assistant.live_summary)
            self.assertEqual(assistant.live_summary_failure_episode, 1)
            self.assertIn(
                "LIVE#2", self.repo.get_latest_summary(session_id, SUMMARY_LIVE).content
            )

            summarizer.release_event.set()
            live_summarize_thread = assistant._summarize_thread
            assistant.stop()
            live_summarize_thread.join(timeout=10)
            self.assertFalse(
                live_summarize_thread.is_alive(),
                "canlı özet işçisi stop()'tan sonra sonlanmalı",
            )

    def test_final_summary_unaffected_by_earlier_live_summary_failure(self):
        self._patch_engine_log()
        assistant = self._start_with_scripted("fail")
        session_id = assistant.session_id
        assistant._run_live_summary_once()
        self.assertEqual(assistant.live_summary_failure_episode, 1)

        assistant._audio_queue.put("son parca")
        final_summary = assistant.stop()

        self.assertTrue(assistant.last_stop_drained)
        self.assertTrue(assistant.last_stop_finalized)
        self.assertEqual(assistant.status, "Durduruldu")
        self.assertEqual(self.repo.get_session(session_id).status, STATUS_COMPLETED)
        self.assertIn("FINAL", final_summary)
        final = self.repo.get_latest_summary(session_id, SUMMARY_FINAL)
        self.assertIsNotNone(final)
        self.assertEqual(final.model, "fake-quality:1b")

    def test_stale_summarize_worker_does_not_contaminate_a_later_session(self):
        """Overlapping-worker regresyon testi: A'nın canlı özet turu hâlâ
        summarize() içinde bloke olmuşken kullanıcı A'yı durdurup HEMEN
        B'yi başlatırsa (AYNI WebinarAssistant örneği), A'nın GECİKMİŞ
        turu -döndüğünde- (1) B'nin live_summary/SQLite/arıza
        sayaçları/durumuna SIZMAMALI, (2) A'nın KENDİ oturumuna da
        'gecikmiş' bir satır olarak YAZILMAMALI (çünkü döndüğünde
        jenerasyon ZATEN değişmişti — A tamamen atılmalı, A'ya bile
        yönlendirilmemeli), ve (3) 'yetim' iş parçacığı B'nin jenerasyonunu
        fark ettiğinde sonlanmalı (B ile birlikte sonsuza dek dönmemeli)."""
        self._patch_engine_log()
        assistant = self._new_assistant(ScriptedLiveSummarizer)
        summarizer = assistant.summarizer
        summarizer.live_outcomes.append("block")

        with patch.object(config, "LIVE_SUMMARY_INTERVAL_SEC", 1):
            assistant.start()
            session_a_id = assistant.session_id
            assistant.full_transcript = ["A oturumu transkripti"]
            self.assertTrue(
                summarizer.live_call_events[0].wait(timeout=20),
                "A'nın canlı özet turu başlamadı",
            )
            stale_thread = assistant._summarize_thread  # start() B için bunu değiştirecek

            assistant.stop()  # stop() _summarize_thread'i BEKLEMEZ; A normal biter
            self.assertTrue(stale_thread.is_alive(), "eski iş parçacığı hâlâ bloke/canlı olmalı")

            assistant.start()  # Session B, AYNI instance
            session_b_id = assistant.session_id
            self.assertNotEqual(session_a_id, session_b_id)
            assistant.full_transcript = ["B oturumu transkripti"]

            # B'nin KENDİ turu ("ok", liste tükendiği için varsayılan) kendi
            # başına tamamlanır; A'nın gecikmiş turunu serbest bırakmadan
            # ÖNCE B'nin gerçek/güncel özetinin zaten yerinde olduğunu kanıtla.
            self.assertTrue(
                summarizer.live_call_events[1].wait(timeout=20),
                "B'nin kendi canlı özet turu başlamadı",
            )
            deadline = time.time() + 5
            while assistant.live_summary == "" and time.time() < deadline:
                time.sleep(0.05)
            b_summary_before_release = assistant.live_summary
            b_status_before_release = assistant.status
            self.assertIn("B oturumu transkripti", b_summary_before_release)
            self.assertNotIn("A oturumu transkripti", b_summary_before_release)

            live_row_b_early = self._wait_for_summary_row(session_b_id, SUMMARY_LIVE)
            self.assertIsNotNone(live_row_b_early, "B'nin kendi canlı özeti SQLite'a zamanında yazılmadı")

            summarizer.release_event.set()  # ŞİMDİ A'nın gecikmiş turunu serbest bırak

            stale_thread.join(timeout=10)
            self.assertFalse(
                stale_thread.is_alive(),
                "yetim iş parçacığı jenerasyon değiştiğini fark ettikten sonra sonlanmalı",
            )

            # (1) B'nin durumu A'nın bayat sonucuyla KİRLENMEMİŞ.
            self.assertEqual(assistant.live_summary, b_summary_before_release)
            self.assertEqual(assistant.status, b_status_before_release)
            live_row_b = self._wait_for_summary_row(session_b_id, SUMMARY_LIVE)
            self.assertIsNotNone(live_row_b)
            self.assertNotIn("A oturumu transkripti", live_row_b.content)
            self.assertEqual(assistant.live_summary_failure_episode, 0)
            self.assertFalse(assistant._live_summary_failing)

            # (2) A'nın KENDİ oturumuna da 'gecikmiş' bir satır olarak
            # YAZILMADI — döndüğünde jenerasyon zaten değişmişti, bu yüzden
            # tamamen atıldı (A'ya bile yönlendirilmedi).
            self.assertIsNone(
                self.repo.get_latest_summary(session_a_id, SUMMARY_LIVE),
                "jenerasyon zaten değişmişken A'ya bile gecikmiş bir satır yazılmamalı",
            )

            # B'nin KENDİ canlı özet işçisi gerçek bir arka plan thread'idir
            # (yamalı 1 sn'lik aralık); stop() bunu KASITLI olarak beklemez
            # (bkz. jenerasyon-sahipliği tasarımı — sınırsız bekleme açıkça
            # reddedildi). Bunu BURADA, test tarafında bekle ki kalıcı bir
            # thread-local SQLite bağlantısı testten sonra da açık kalmasın
            # (Windows, o bağlantı açıkken geçici veritabanını silmeyi reddeder).
            b_summarize_thread = assistant._summarize_thread
            assistant.stop()
            b_summarize_thread.join(timeout=10)
            self.assertFalse(
                b_summarize_thread.is_alive(),
                "B'nin canlı özet işçisi stop()'tan sonra sonlanmalı",
            )

    def test_stale_summarize_worker_failure_does_not_pollute_new_session_episode(self):
        """Aynı geçiş, ama A'nın GECİKMİŞ turu B başladıktan SONRA BAŞARISIZ
        olur. B'nin live_summary_failure_episode/_live_summary_failing/
        durumu, artık var olmayan bir oturuma ait bu arızadan ETKİLENMEMELİ."""
        self._patch_engine_log()
        assistant = self._new_assistant(ScriptedLiveSummarizer)
        summarizer = assistant.summarizer
        summarizer.live_outcomes.append("block_fail")

        with patch.object(config, "LIVE_SUMMARY_INTERVAL_SEC", 1):
            assistant.start()
            assistant.full_transcript = ["A oturumu transkripti"]
            self.assertTrue(summarizer.live_call_events[0].wait(timeout=20))
            stale_thread = assistant._summarize_thread

            assistant.stop()
            assistant.start()
            assistant.full_transcript = ["B oturumu transkripti"]

            self.assertTrue(summarizer.live_call_events[1].wait(timeout=20))
            deadline = time.time() + 5
            while assistant.live_summary == "" and time.time() < deadline:
                time.sleep(0.05)
            self.assertEqual(assistant.live_summary_failure_episode, 0)
            b_status_before_release = assistant.status

            summarizer.release_event.set()  # A'nın gecikmiş turu ŞİMDİ fırlatır

            stale_thread.join(timeout=10)
            self.assertFalse(stale_thread.is_alive())

            self.assertEqual(
                assistant.live_summary_failure_episode, 0,
                "B'nin arıza sayacı, artık geçerli olmayan A'nın arızasından etkilenmemeli",
            )
            self.assertFalse(assistant._live_summary_failing)
            self.assertEqual(
                assistant.status, b_status_before_release,
                "B'nin durumu, A'nın bayat arızasından etkilenmemeli",
            )

            b_summarize_thread = assistant._summarize_thread
            assistant.stop()
            b_summarize_thread.join(timeout=10)
            self.assertFalse(
                b_summarize_thread.is_alive(),
                "B'nin canlı özet işçisi stop()'tan sonra sonlanmalı",
            )

    def test_ownership_boundary_is_atomic_never_partially_applied(self):
        """TOCTOU sınır testi: A'nın canlı turu, tam olarak `generation ==
        self._session_generation` kontrolünü GEÇTİĞİ ve yayınlamaya
        BAŞLADIĞI ANDA (kilit ZATEN alınmış, gövde henüz çalışmamış), B'nin
        start()'ı AYNI GERÇEK kilide bilerek TIKANDIRILIR. Kilit tutulduğu
        sürece B'nin start()'ı kesinlikle İLERLEYEMEMELİ (böylece 'başka bir
        iş parçacığı aslında bloke oldu' GERÇEKTEN kanıtlanır, sadece
        'henüz çalışmadı' varsayılmaz); serbest bırakıldığında ise A'nın
        yayını TAMAMEN kendi (henüz geçerli olan) oturumuna uygulanmış, B'nin
        geçişi ise ONDAN SONRA TAMAMEN uygulanmış olmalı — asla ikisinin
        yarım kalmış bir karışımı gözlenmemeli (A'nın içeriği B'ye SIZMAZ,
        B'nin sıfırlaması A'nın kalıcı satırını SİLMEZ)."""
        self._patch_engine_log()
        assistant = self._new_assistant(ScriptedLiveSummarizer)
        summarizer = assistant.summarizer
        summarizer.live_outcomes.append("ok")  # A'nın turu normal döner (Ollama'da bloke DEĞİL)

        with patch.object(config, "LIVE_SUMMARY_INTERVAL_SEC", 1):
            assistant.start()  # Session A (gerçek kilit kullanılır)
            session_a_id = assistant.session_id
            stale_thread_a = assistant._summarize_thread  # start() B için bunu değiştirecek
            assistant.full_transcript = ["A oturumu transkripti"]

            # A'nın turu summarize()'dan DÖNDÜ ve şimdi yayınlamak için
            # kilide girmek üzere; TAM O ANDA (kilit alındıktan hemen sonra,
            # gövde çalışmadan ÖNCE) duraklat.
            pausable = _PausableLock(assistant._session_generation_lock)
            pausable.arm_pause()
            assistant._session_generation_lock = pausable  # A'nın işçisi BUNU kullanacak

            self.assertTrue(
                pausable.holder_ready.wait(timeout=20),
                "A'nın turu yayınlama kilidine hiç girmedi",
            )
            # A'nın turu ŞİMDİ kilidi TUTUYOR, kritik bölgeyi henüz TAMAMLAMADI.

            # A'yı durdur ki YENİ bir start() gerçekten bir şey yapsın
            # (_running True iken start() no-op'tur). stop(),
            # _session_generation_lock'a Hiç dokunmaz, bu yüzden A'nın turu
            # kilidi tutarken bunu çağırmak güvenlidir.
            assistant.stop()

            start_b_done = threading.Event()

            def start_session_b():
                assistant.start()
                # start() (create_session() aracılığıyla) BU thread'de KENDİ
                # thread-local SQLite bağlantısını açar — thread çıkmadan
                # ÖNCE bunu açıkça kapat: _summarize_loop/_transcribe_loop'un
                # aksine, bu düz yardımcı thread'in finally-bloğu YOKTUR.
                assistant.repository.close_thread_connection()
                start_b_done.set()

            b_thread = threading.Thread(target=start_session_b)
            b_thread.start()

            # A hâlâ GERÇEK kilidi tutarken B'nin start()'ı KESİNLİKLE
            # tamamlanmamalı — bu, GERÇEK threading.Lock üzerinde gerçek bir
            # bloke olmaya dayanır, simülasyon değil.
            self.assertFalse(
                start_b_done.wait(timeout=0.5),
                "B'nin start()'ı, A hâlâ yayınlama kilidini tutarken ilerlememeliydi",
            )

            pausable.release_holder.set()  # A'nın yayınını (kendi jenerasyonu
                                            # HÂLÂ geçerliydi) tamamlamasına izin ver

            b_thread.join(timeout=10)
            self.assertTrue(
                start_b_done.is_set(), "B'nin start()'ı serbest bırakıldıktan sonra tamamlanmalıydı"
            )

            session_b_id = assistant.session_id
            self.assertNotEqual(session_a_id, session_b_id)

            # Sonuç: KISMİ bir durum YOK. A'nın yayını (kilidi ÖNCE aldığı
            # için) kendi oturumuna TAMAMEN uygulandı, B'nin geçişi ONDAN
            # SONRA (A kilidi bırakınca) TAMAMEN uygulandı — B kendi
            # start()'ının TAZE sıfırlamasını gösteriyor (A'nın içeriğini
            # DEĞİL), ve A'nın SQLite satırı kendi oturumunda SAKLI kalıyor.
            self.assertEqual(
                assistant.live_summary, "",
                "B'nin start()'ı kendi sıfırlamasıyla sonuçlanmalı (A'nın geçici yazımı değil)",
            )
            a_live_row = self._wait_for_summary_row(session_a_id, SUMMARY_LIVE)
            self.assertIsNotNone(
                a_live_row, "A'nın yayını KENDİ oturumuna TAM olarak uygulanmış olmalı"
            )
            self.assertIn("A oturumu transkripti", a_live_row.content)
            self.assertIsNone(
                self.repo.get_latest_summary(session_b_id, SUMMARY_LIVE),
                "B henüz kendi turunu üretmedi; A'nınki B'ye SIZMAMALI",
            )

            # Bu testin oluşturduğu HİÇBİR canlı-özet işçisi geride
            # bırakılmasın: A'nın orijinali (şimdiye dek hiç join()
            # edilmedi) VE B'nin kendi yeni işçisi (start_session_b()
            # tarafından oluşturuldu). stop() ikisini de BEKLEMEZ.
            b_summarize_thread = assistant._summarize_thread
            assistant.stop()
            stale_thread_a.join(timeout=10)
            self.assertFalse(stale_thread_a.is_alive(), "A'nın canlı özet işçisi sonlanmalı")
            b_summarize_thread.join(timeout=10)
            self.assertFalse(b_summarize_thread.is_alive(), "B'nin canlı özet işçisi sonlanmalı")

    def test_publish_uses_the_frozen_session_id_not_a_later_reassignment(self):
        """A'nın sahiplik kontrolü GEÇER ve target_session_id kilit ALTINDA
        dondurulur; B'nin TÜM start()'ı (kendi self.session_id ataması DAHİL)
        A'nın GERÇEK SQLite yazımı çalışmadan ÖNCE TAMAMEN bitmesine izin
        verilir (kilit yerine sarılmış bir save_summary üzerinden duraklatılır
        — bu, yukarıdaki kilit testinin özellikle zorlamadığı, 'kilit
        serbest bırakıldı' ile 'yazım fiilen çalışır' arasındaki daha dar
        pencereyi hedefler). Yazım yine de A'nın KENDİ kimliğine düşmeli:
        save_summary()'ye geçilen argümanın kontrol anında DONDURULDUĞUNU,
        yazım anında self.session_id'den YENİDEN OKUNMADIĞINI kanıtlar."""
        self._patch_engine_log()
        assistant = self._new_assistant(ScriptedLiveSummarizer)
        summarizer = assistant.summarizer
        summarizer.live_outcomes.append("ok")

        with patch.object(config, "LIVE_SUMMARY_INTERVAL_SEC", 1):
            assistant.start()
            session_a_id = assistant.session_id
            stale_thread_a = assistant._summarize_thread  # start() B için bunu değiştirecek
            assistant.full_transcript = ["A oturumu transkripti"]

            real_save_summary = assistant.repository.save_summary
            save_called_with = []
            pause_armed = [True]
            save_about_to_run = threading.Event()
            proceed_with_save = threading.Event()

            def pausing_save_summary(session_id, *args, **kwargs):
                save_called_with.append(session_id)
                if pause_armed[0]:
                    pause_armed[0] = False
                    save_about_to_run.set()
                    proceed_with_save.wait(timeout=10)
                return real_save_summary(session_id, *args, **kwargs)

            assistant.repository.save_summary = pausing_save_summary

            self.assertTrue(summarizer.live_call_events[0].wait(timeout=20))
            self.assertTrue(
                save_about_to_run.wait(timeout=20),
                "A'nın canlı turu save_summary()'ye hiç ulaşmadı",
            )
            self.assertEqual(save_called_with, [session_a_id])
            # A'nın yazımı şimdi, target_session_id kilit altında
            # dondurulduktan SONRA ama gerçek SQLite INSERT'i çalışmadan
            # ÖNCE duraklatılmış durumda.

            assistant.stop()  # A'nın KENDİ final-özet save_summary çağrısı,
                               # pause_armed tükendiği için DURAKLAMAZ.
            assistant.start()
            session_b_id = assistant.session_id
            self.assertNotEqual(session_a_id, session_b_id)

            proceed_with_save.set()  # A'nın duraklamış yazımını şimdi serbest bırak

            a_live_row = self._wait_for_summary_row(session_a_id, SUMMARY_LIVE)
            self.assertIsNotNone(
                a_live_row,
                "A'nın yayını, kilit altında dondurulan KENDİ session_id'sine yazılmalı",
            )
            self.assertIsNone(
                self.repo.get_latest_summary(session_b_id, SUMMARY_LIVE),
                "A'nın yayını B'nin SONRADAN atanan session_id'sine SIZMAMALI",
            )

            b_summarize_thread = assistant._summarize_thread
            assistant.stop()
            stale_thread_a.join(timeout=10)
            self.assertFalse(stale_thread_a.is_alive(), "A'nın canlı özet işçisi sonlanmalı")
            b_summarize_thread.join(timeout=10)
            self.assertFalse(b_summarize_thread.is_alive(), "B'nin canlı özet işçisi sonlanmalı")

    def test_vesper_package_has_no_nicegui_import(self):
        """CLAUDE.md §5.7: motor (vesper/) NiceGUI'ye HİÇBİR import bağımlılığı içermez."""
        offenders = []
        for name in sorted(os.listdir(_VESPER_PACKAGE_DIR)):
            if not name.endswith(".py"):
                continue
            path = os.path.join(_VESPER_PACKAGE_DIR, name)
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                else:
                    continue
                if any(module == "nicegui" or module.startswith("nicegui.") for module in modules):
                    offenders.append(name)
        self.assertEqual(offenders, [], f"NiceGUI import'u içeren motor dosyaları: {offenders}")


if __name__ == "__main__":
    unittest.main()
