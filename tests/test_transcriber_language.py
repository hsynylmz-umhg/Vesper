"""
v0.4.0 Transcriber dil argümanı testleri — GERÇEK GPU/Whisper/CTranslate2 YOK.

Transcriber, sahte bir WhisperModel ile CPU yolunda kurulur (DEVICE="cpu",
model indirme yaması). Doğrulananlar:
  - açık "tr"/"en"/None doğrudan faster-whisper'a ulaşır,
  - argüman verilmezse GÜVENLİ normalize varsayılan kullanılır,
  - elle bozulmuş WHISPER_LANGUAGE ne warm-up'a ne transcribe'a ham ulaşır,
  - CUDA yeniden deneme yolu ORİJİNAL çözülmüş dili kullanır,
  - korunan çözümleme parametreleri (v0.3.0 ile) DEĞİŞMEMİŞTİR.
"""

import os
import sys
import threading
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _ROOT)

# transcriber.py modül seviyesinde `import ctranslate2` / `from faster_whisper
# import ...` yapar; burada gerçek paketler gerekmez (bkz. test_lifecycle_
# integration.py'deki aynı gerekçe).
if "webrtcvad" not in sys.modules:
    sys.modules["webrtcvad"] = types.ModuleType("webrtcvad")
if "ctranslate2" not in sys.modules:
    sys.modules["ctranslate2"] = types.ModuleType("ctranslate2")
if "faster_whisper" not in sys.modules:
    _fake_fw = types.ModuleType("faster_whisper")
    _fake_fw.WhisperModel = type("WhisperModel", (), {})
    _fake_fw.download_model = lambda *a, **k: None
    sys.modules["faster_whisper"] = _fake_fw

from vesper import transcriber as transcriber_module
from vesper.transcriber import Transcriber

AUDIO = np.full(16000, 0.1, dtype=np.float32)  # 1 sn, MIN_CHUNK_SEC'in üstünde


class RecordingModel:
    """Sahte WhisperModel: transcribe() çağrılarını (audio, kwargs) olarak kaydeder."""

    def __init__(self, *args, **kwargs):
        self.calls = []
        self.side_effect = None

    def transcribe(self, audio, **kwargs):
        self.calls.append((audio, dict(kwargs)))
        if self.side_effect is not None:
            self.side_effect()
        segment = SimpleNamespace(text=" merhaba dünya ")
        return iter([segment]), SimpleNamespace(language="xx", language_probability=0.5)


class TestTranscriberLanguage(unittest.TestCase):
    def _make(self, config_attrs=None):
        """CPU yolunda sahte modelle bir Transcriber kurar (warm-up dahil)."""
        cfg = SimpleNamespace(**(config_attrs if config_attrs is not None else {"WHISPER_LANGUAGE": "tr"}))
        with patch("vesper.transcriber.DEVICE", "cpu"), \
             patch("vesper.transcriber.WhisperModel", RecordingModel), \
             patch.object(Transcriber, "_ensure_model_available", return_value="sahte-model"), \
             patch("vesper.language.config", cfg):
            return Transcriber()

    def test_explicit_languages_reach_faster_whisper_exactly(self):
        t = self._make()
        for value in ("tr", "en"):
            with self.subTest(language=value):
                t.model.calls.clear()
                self.assertEqual(t.transcribe(AUDIO, language=value), "merhaba dünya")
                (_, kwargs), = t.model.calls
                self.assertEqual(kwargs["language"], value)

    def test_explicit_none_reaches_faster_whisper_as_automatic_detection(self):
        t = self._make()
        t.model.calls.clear()
        t.transcribe(AUDIO, language=None)
        (_, kwargs), = t.model.calls
        self.assertIn("language", kwargs)
        self.assertIsNone(kwargs["language"])

    def test_omitted_language_uses_safe_normalized_config_default(self):
        t = self._make()
        expectations = [("tr", "tr"), ("en", "en"), (None, None), ("TR", "tr"), ("de", "tr"), (5, "tr")]
        for raw, expected in expectations:
            with self.subTest(raw=raw):
                t.model.calls.clear()
                with patch("vesper.language.config", SimpleNamespace(WHISPER_LANGUAGE=raw)):
                    t.transcribe(AUDIO)
                (_, kwargs), = t.model.calls
                self.assertEqual(kwargs["language"], expected)

    def test_invalid_hand_edited_config_never_reaches_warmup_or_transcribe(self):
        for bad in ("xx", "de", "TR", "tr-TR", "", 5, ["tr"]):
            with self.subTest(raw=bad):
                with self.assertLogs("language", level="WARNING"):
                    t = self._make({"WHISPER_LANGUAGE": bad})
                    with patch("vesper.language.config", SimpleNamespace(WHISPER_LANGUAGE=bad)):
                        t.transcribe(AUDIO)
                languages = [kwargs["language"] for _, kwargs in t.model.calls]
                self.assertEqual(len(languages), 2)  # warm-up + transcribe
                self.assertEqual(languages, ["tr", "tr"])

    def test_warmup_uses_safe_language_and_keeps_its_other_parameters(self):
        with self.assertLogs("language", level="WARNING"):
            t = self._make({"WHISPER_LANGUAGE": "TR"})
        (audio, kwargs), = t.model.calls  # yalnızca warm-up çağrısı
        self.assertEqual(kwargs, {"language": "tr", "beam_size": 1, "without_timestamps": True})
        self.assertEqual(audio.shape, (transcriber_module.SAMPLE_RATE,))
        self.assertEqual(audio.dtype, np.float32)
        self.assertFalse(audio.any())

        auto = self._make({"WHISPER_LANGUAGE": None})
        (_, kwargs), = auto.model.calls
        self.assertIsNone(kwargs["language"])

    def test_cuda_retry_reuses_the_original_explicit_language(self):
        t = self._make()
        failing = RecordingModel()
        failing.side_effect = self._raise_cuda_error
        retry_model = RecordingModel()

        def fake_fallback(self_):
            self_.model = retry_model
            self_.device = "cpu"

        t.model = failing
        t.device = "cuda:0"
        t._cuda_error_count = transcriber_module.MAX_CUDA_RUNTIME_ERRORS - 1
        with patch.object(Transcriber, "_fallback_to_cpu_after_runtime_error", fake_fallback), \
             patch("vesper.transcriber.ALLOW_CPU_FALLBACK", True):
            self.assertEqual(t.transcribe(AUDIO, language="en"), "merhaba dünya")
        self.assertEqual([kw["language"] for _, kw in failing.calls], ["en"])
        self.assertEqual([kw["language"] for _, kw in retry_model.calls], ["en"])

    def test_cuda_retry_resolves_language_once_even_if_config_changes_between_attempts(self):
        t = self._make()
        cfg = SimpleNamespace(WHISPER_LANGUAGE="tr")
        failing = RecordingModel()
        retry_model = RecordingModel()

        def change_config_then_fail():
            cfg.WHISPER_LANGUAGE = "en"  # ilk deneme sırasında config değişiyor
            self._raise_cuda_error()

        failing.side_effect = change_config_then_fail

        def fake_fallback(self_):
            self_.model = retry_model
            self_.device = "cpu"

        t.model = failing
        t.device = "cuda:0"
        t._cuda_error_count = transcriber_module.MAX_CUDA_RUNTIME_ERRORS - 1
        with patch("vesper.language.config", cfg), \
             patch.object(Transcriber, "_fallback_to_cpu_after_runtime_error", fake_fallback), \
             patch("vesper.transcriber.ALLOW_CPU_FALLBACK", True):
            t.transcribe(AUDIO)  # dil argümanı YOK -> varsayılan ("tr") çağrı başına bir kez çözülür
        self.assertEqual([kw["language"] for _, kw in failing.calls], ["tr"])
        self.assertEqual([kw["language"] for _, kw in retry_model.calls], ["tr"])

    def test_protected_decoding_parameters_are_unchanged(self):
        t = self._make()
        t.model.calls.clear()
        t.transcribe(AUDIO, language="en")
        (_, kwargs), = t.model.calls
        self.assertEqual(
            kwargs,
            {
                "language": "en",
                "task": "transcribe",
                "beam_size": transcriber_module.BEAM_SIZE,
                "condition_on_previous_text": False,
                "vad_filter": False,
                "without_timestamps": True,
                "temperature": [0.0, 0.2, 0.4],
                "compression_ratio_threshold": 2.4,
                "log_prob_threshold": -1.0,
                "no_speech_threshold": 0.6,
            },
        )
        self.assertIs(kwargs["condition_on_previous_text"], False)
        self.assertIs(kwargs["vad_filter"], False)

    def test_return_type_stays_str_and_short_audio_is_still_skipped(self):
        t = self._make()
        t.model.calls.clear()
        self.assertIsInstance(t.transcribe(AUDIO, language="tr"), str)
        self.assertEqual(t.transcribe(np.zeros(0, dtype=np.float32), language="tr"), "")
        self.assertEqual(t.transcribe(np.zeros(100, dtype=np.float32), language="tr"), "")
        self.assertEqual(len(t.model.calls), 1)  # kısa/boş ses modele HİÇ gitmedi

    def test_no_raw_config_whisper_language_read_remains_in_transcriber(self):
        source = Path(_ROOT, "vesper", "transcriber.py").read_text(encoding="utf-8")
        self.assertNotIn("config.WHISPER_LANGUAGE", source)

    @staticmethod
    def _raise_cuda_error():
        raise RuntimeError("CUDA error: device-side assert triggered")


if __name__ == "__main__":
    unittest.main()
