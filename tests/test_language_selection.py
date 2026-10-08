"""
v0.4.0 konuşma (kaynak) dili seçimi testleri — GERÇEK GPU/Ollama/ses cihazı YOK.

İki katman:
  1. TestLanguagePolicy: vesper/language.py saf fonksiyonları (config okuma,
     doğrulama, eski WHISPER_LANGUAGE eşlemesi). Donanım/Whisper gerekmez.
  2. TestSourceLanguageEngine: WebinarAssistant'ın dil yüzeyi ve oturum-yerel
     dondurma davranışı; sahte yakalama/transkripsiyon/özetleme ile.

Sahteler test_lifecycle_integration.py'den yeniden kullanılır (aynı sys.modules
stub'larını da o modül kurar).
"""

import ast
import copy
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _ROOT)

try:
    from tests.test_lifecycle_integration import (
        ControllableFakeTranscriber,
        FakeAudioCapture,
        FakeSummarizer,
    )
except ImportError:  # `discover -s tests` (tests/ en üst dizin olarak)
    from test_lifecycle_integration import (
        ControllableFakeTranscriber,
        FakeAudioCapture,
        FakeSummarizer,
    )

from vesper import config, language
from vesper.repository import SessionRepository
from vesper.storage import Database

_OMITTED = object()


class RecordingTranscriber:
    """Her transcribe() çağrısını (chunk, language) olarak kaydeder. `language`
    argümanı HİÇ verilmezse _OMITTED kaydedilir — motorun HER ZAMAN açıkça
    (keyword) dil verdiğini doğrulamak için."""

    def __init__(self):
        self.calls = []

    def transcribe(self, chunk, language=_OMITTED) -> str:
        self.calls.append((chunk, language))
        return str(chunk)


class RecordingControllableTranscriber(ControllableFakeTranscriber):
    def __init__(self):
        super().__init__()
        self.calls = []

    def transcribe(self, chunk, language=_OMITTED) -> str:
        self.calls.append((chunk, language))
        return super().transcribe(chunk, language)


class RecordingSummarizer(FakeSummarizer):
    """summarize() çağrılarını AYNEN kaydeder (dil argümanı sızmasın diye)."""

    def __init__(self):
        super().__init__()
        self.summarize_calls = []

    def summarize(self, *args, **kwargs):
        self.summarize_calls.append((args, dict(kwargs)))
        return super().summarize(*args, **kwargs)


def _table(**overrides):
    """Yerleşik tablonun kopyası; `overrides` girdi anahtarını (tr/en/auto) değiştirir."""
    table = copy.deepcopy(language.BUILTIN_SOURCE_LANGUAGES)
    table.update(overrides)
    return table


class TestLanguagePolicy(unittest.TestCase):
    def _resolve(self, **attrs):
        with patch("vesper.language.config", SimpleNamespace(**attrs)):
            return language.resolve_source_languages()

    def test_builtin_options_are_exactly_three_in_order(self):
        self.assertEqual(list(language.BUILTIN_SOURCE_LANGUAGES), ["tr", "en", "auto"])
        self.assertEqual(
            {k: v["whisper_language"] for k, v in language.BUILTIN_SOURCE_LANGUAGES.items()},
            {"tr": "tr", "en": "en", "auto": None},
        )
        self.assertEqual(
            [v["label"] for v in language.BUILTIN_SOURCE_LANGUAGES.values()],
            ["Türkçe", "English", "Otomatik"],
        )

    def test_shipped_config_table_equals_builtin_fallback(self):
        self.assertEqual(config.SOURCE_LANGUAGES, language.BUILTIN_SOURCE_LANGUAGES)

    def test_shipped_config_resolves_to_turkish(self):
        self.assertEqual(config.WHISPER_LANGUAGE, "tr")
        with self.assertNoLogs("language", level="WARNING"):
            options, default_key = language.resolve_source_languages()
        self.assertEqual(default_key, "tr")
        self.assertEqual(list(options), ["tr", "en", "auto"])
        self.assertEqual(language.whisper_language_for(options, default_key), "tr")

    def test_none_resolves_to_auto(self):
        with self.assertNoLogs("language", level="WARNING"):
            options, default_key = self._resolve(WHISPER_LANGUAGE=None, SOURCE_LANGUAGES=_table())
        self.assertEqual(default_key, "auto")
        self.assertIsNone(language.whisper_language_for(options, default_key))

    def test_en_resolves_to_en(self):
        with self.assertNoLogs("language", level="WARNING"):
            _, default_key = self._resolve(WHISPER_LANGUAGE="en", SOURCE_LANGUAGES=_table())
        self.assertEqual(default_key, "en")

    def test_missing_whisper_language_defaults_to_turkish_silently(self):
        with self.assertNoLogs("language", level="WARNING"):
            _, default_key = self._resolve(SOURCE_LANGUAGES=_table())
        self.assertEqual(default_key, "tr")

    def test_invalid_legacy_values_warn_and_resolve_to_turkish(self):
        for bad in ("de", "xx", "TR", "tr-TR", "", " tr", "auto", 5, True, ["tr"], {"a": 1}, b"tr", 1.5):
            with self.subTest(value=bad):
                with self.assertLogs("language", level="WARNING"):
                    options, default_key = self._resolve(
                        WHISPER_LANGUAGE=bad, SOURCE_LANGUAGES=_table()
                    )
                self.assertEqual(default_key, "tr")
                self.assertEqual(language.whisper_language_for(options, default_key), "tr")
                # Keyfi seçenek SENTEZLENMEZ: tablo hâlâ tam olarak üç resmi seçenek.
                self.assertEqual(list(options), ["tr", "en", "auto"])

    def test_missing_source_languages_uses_builtin_without_warning(self):
        with self.assertNoLogs("language", level="WARNING"):
            options, default_key = self._resolve(WHISPER_LANGUAGE="en")
        self.assertEqual(options, language.BUILTIN_SOURCE_LANGUAGES)
        self.assertEqual(default_key, "en")

    def test_malformed_source_languages_warns_and_uses_builtin(self):
        def with_entry(key, **entry_overrides):
            table = _table()
            entry = dict(table[key])
            entry.update(entry_overrides)
            table[key] = entry
            return table

        no_whisper_key = _table()
        no_whisper_key["tr"] = {"label": "Türkçe"}
        no_label = _table()
        no_label["tr"] = {"whisper_language": "tr"}
        without_auto = {k: v for k, v in _table().items() if k != "auto"}

        cases = {
            "None": None,
            "list": [],
            "str": "tr",
            "int": 5,
            "empty dict": {},
            "missing auto": without_auto,
            "extra 'de' option": {**_table(), "de": {"label": "Deutsch", "whisper_language": "de"}},
            "entry not a dict": _table(tr="Türkçe"),
            "blank label": with_entry("tr", label="   "),
            "non-str label": with_entry("tr", label=5),
            "missing label": no_label,
            "missing whisper_language": no_whisper_key,
            "tr maps to de": with_entry("tr", whisper_language="de"),
            "tr maps to TR": with_entry("tr", whisper_language="TR"),
            "en maps to None": with_entry("en", whisper_language=None),
            "auto maps to en": with_entry("auto", whisper_language="en"),
            "auto maps to 'auto'": with_entry("auto", whisper_language="auto"),
        }
        for name, bad_table in cases.items():
            with self.subTest(case=name):
                with self.assertLogs("language", level="WARNING"):
                    options, default_key = self._resolve(
                        WHISPER_LANGUAGE="tr", SOURCE_LANGUAGES=bad_table
                    )
                self.assertEqual(options, language.BUILTIN_SOURCE_LANGUAGES)
                self.assertEqual(default_key, "tr")

    def test_valid_table_allows_label_and_order_customization_only(self):
        custom = {
            "auto": {"label": "  Auto  ", "whisper_language": None},
            "en": {"label": "Eng", "whisper_language": "en"},
            "tr": {"label": "TR", "whisper_language": "tr"},
        }
        with self.assertNoLogs("language", level="WARNING"):
            options, _ = self._resolve(WHISPER_LANGUAGE="tr", SOURCE_LANGUAGES=custom)
        self.assertEqual(list(options), ["auto", "en", "tr"])
        self.assertEqual(options["auto"], {"label": "Auto", "whisper_language": None})
        self.assertEqual(options["tr"]["whisper_language"], "tr")

    def test_whisper_language_for_maps_and_rejects(self):
        options = copy.deepcopy(language.BUILTIN_SOURCE_LANGUAGES)
        self.assertEqual(language.whisper_language_for(options, "tr"), "tr")
        self.assertEqual(language.whisper_language_for(options, "en"), "en")
        self.assertIsNone(language.whisper_language_for(options, "auto"))
        for bad in ("de", "xx", "TR", "", None, 5, ["tr"], b"tr"):
            with self.subTest(key=bad):
                with self.assertRaises(ValueError):
                    language.whisper_language_for(options, bad)

    def test_default_whisper_language_is_always_safe(self):
        expectations = [("tr", "tr"), ("en", "en"), (None, None), ("TR", "tr"), ("de", "tr"), (5, "tr")]
        for raw, expected in expectations:
            with self.subTest(raw=raw):
                with patch("vesper.language.config", SimpleNamespace(WHISPER_LANGUAGE=raw)):
                    self.assertEqual(language.resolve_default_whisper_language(), expected)

    def test_results_are_copies_of_builtin(self):
        options, _ = self._resolve()
        options["tr"]["label"] = "DEĞİŞTİ"
        options.pop("en")
        self.assertEqual(language.BUILTIN_SOURCE_LANGUAGES["tr"]["label"], "Türkçe")
        self.assertIn("en", language.BUILTIN_SOURCE_LANGUAGES)

    def test_language_module_is_dependency_light(self):
        source = Path(_ROOT, "vesper", "language.py").read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add("." * node.level + (node.module or ""))
        self.assertEqual(imported, {"copy", "logging", "typing", ".", })

    def test_sotto_name_is_not_used_in_code(self):
        files = list(Path(_ROOT, "vesper").glob("*.py")) + [Path(_ROOT, "ui.py")]
        self.assertFalse(Path(_ROOT, "vesper", "sotto").exists())
        for path in files:
            with self.subTest(file=path.name):
                self.assertNotIn("sotto", path.read_text(encoding="utf-8").lower())


class TestSourceLanguageEngine(unittest.TestCase):
    def setUp(self):
        self._db_tmpdir = tempfile.TemporaryDirectory()
        self.repo = SessionRepository(Database(Path(self._db_tmpdir.name) / "language_test.db"))
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

    def _assistant(self, transcriber_cls=RecordingTranscriber, summarizer_cls=FakeSummarizer):
        import vesper.engine as engine_module
        with patch("vesper.engine.AudioCapture", FakeAudioCapture), \
             patch("vesper.engine.Transcriber", transcriber_cls), \
             patch("vesper.engine.Summarizer", summarizer_cls):
            return engine_module.WebinarAssistant(repository=self.repo)

    def _run_session(self, assistant, chunks):
        assistant.start()
        session_id = assistant.session_id
        for chunk in chunks:
            assistant._audio_queue.put(chunk)
        assistant.stop()
        return session_id

    def _assert_session_language(self, session_id, expected, texts):
        session = self.repo.get_session(session_id)
        self.assertEqual(session.source_language, expected)
        self.assertEqual(session.summary_language, "tr")
        segments = self.repo.get_segments(session_id)
        self.assertEqual([s.text for s in segments], texts)
        self.assertEqual([s.language for s in segments], [expected] * len(texts))

    def test_default_is_turkish_and_persisted(self):
        assistant = self._assistant()
        self.assertEqual(assistant.get_source_language_key(), "tr")
        self.assertEqual(
            assistant.list_source_languages(),
            [
                {"key": "tr", "label": "Türkçe"},
                {"key": "en", "label": "English"},
                {"key": "auto", "label": "Otomatik"},
            ],
        )
        session_id = self._run_session(assistant, ["a", "b"])
        self._assert_session_language(session_id, "tr", ["a", "b"])
        self.assertEqual(assistant.transcriber.calls, [("a", "tr"), ("b", "tr")])

    def test_english_selection(self):
        assistant = self._assistant()
        assistant.set_source_language("en")
        self.assertEqual(assistant.get_source_language_key(), "en")
        session_id = self._run_session(assistant, ["hello"])
        self._assert_session_language(session_id, "en", ["hello"])
        self.assertEqual(assistant.transcriber.calls, [("hello", "en")])

    def test_auto_selection_passes_explicit_none_and_persists_null(self):
        assistant = self._assistant()
        assistant.set_source_language("auto")
        session_id = self._run_session(assistant, ["x"])
        self._assert_session_language(session_id, None, ["x"])
        (chunk, passed_language), = assistant.transcriber.calls
        self.assertEqual(chunk, "x")
        self.assertIsNone(passed_language)  # açıkça None; _OMITTED DEĞİL

    def test_initial_selection_follows_legacy_config(self):
        with patch.object(config, "WHISPER_LANGUAGE", None):
            assistant = self._assistant()
        self.assertEqual(assistant.get_source_language_key(), "auto")
        session_id = self._run_session(assistant, ["x"])
        self._assert_session_language(session_id, None, ["x"])

        with patch.object(config, "WHISPER_LANGUAGE", "en"):
            assistant = self._assistant()
        self.assertEqual(assistant.get_source_language_key(), "en")

    def test_invalid_legacy_config_falls_back_to_turkish_with_warning(self):
        for bad in ("de", "TR", "", 5):
            with self.subTest(value=bad):
                with patch.object(config, "WHISPER_LANGUAGE", bad):
                    with self.assertLogs("language", level="WARNING"):
                        assistant = self._assistant()
                self.assertEqual(assistant.get_source_language_key(), "tr")
                self.assertEqual(
                    [o["key"] for o in assistant.list_source_languages()], ["tr", "en", "auto"]
                )

    def test_unknown_key_rejected_and_state_unchanged(self):
        assistant = self._assistant()
        assistant.set_source_language("en")
        for bad in ("de", "xx", "TR", "", "custom", None, 5, ["tr"]):
            with self.subTest(key=bad):
                with self.assertRaises(ValueError):
                    assistant.set_source_language(bad)
                self.assertEqual(assistant.get_source_language_key(), "en")

    def test_change_rejected_while_running_then_allowed_after_stop(self):
        assistant = self._assistant()
        assistant.start()
        try:
            with self.assertRaises(RuntimeError):
                assistant.set_source_language("en")
            self.assertEqual(assistant.get_source_language_key(), "tr")
        finally:
            assistant.stop()
        assistant.set_source_language("en")
        self.assertEqual(assistant.get_source_language_key(), "en")

    def test_language_is_frozen_for_the_session(self):
        assistant = self._assistant()
        assistant.set_source_language("en")
        assistant.start()
        session_id = assistant.session_id
        # Seçim durumu (hata/yarış simülasyonu) oturum sürerken DOĞRUDAN bozulsa bile
        # oturum başında dondurulan değer kullanılmalı.
        assistant._source_language_key = "tr"
        assistant._audio_queue.put("frozen")
        assistant.stop()
        self._assert_session_language(session_id, "en", ["frozen"])
        self.assertEqual(assistant.transcriber.calls, [("frozen", "en")])

    def test_stale_transcribe_worker_keeps_its_original_language(self):
        assistant = self._assistant(transcriber_cls=RecordingControllableTranscriber)
        release_event = threading.Event()
        assistant.transcriber.block_on = "STUCK"
        assistant.transcriber.release_event = release_event
        assistant.set_source_language("en")

        assistant.start()
        session_id = assistant.session_id
        assistant._audio_queue.put("STUCK")
        assistant._audio_queue.put("AFTER")
        self.assertTrue(assistant.transcriber.reached_block.wait(timeout=5))

        with patch("vesper.engine.TRANSCRIBE_JOIN_TIMEOUT_SEC", 0.2):
            assistant.stop()
        self.assertFalse(assistant.last_stop_drained)

        # stop() zaman aşımına uğradı; seçici yeniden etkin ve değişebilir —
        # ama eski işçi hâlâ canlı ve yeni oturum başlatılamaz.
        assistant.set_source_language("tr")
        self.assertEqual(assistant.get_source_language_key(), "tr")
        with self.assertRaises(RuntimeError):
            assistant.start()

        release_event.set()
        assistant._transcribe_thread.join(timeout=10)
        self.assertFalse(assistant._transcribe_thread.is_alive())

        self.assertEqual(
            assistant.transcriber.calls, [("STUCK", "en"), ("AFTER", "en")]
        )
        segments = self.repo.get_segments(session_id)
        self.assertEqual([(s.text, s.language) for s in segments], [("STUCK", "en"), ("AFTER", "en")])
        self.assertEqual(self.repo.get_session(session_id).source_language, "en")

    def test_no_cross_session_language_contamination(self):
        assistant = self._assistant()
        assistant.set_source_language("en")
        first_id = self._run_session(assistant, ["one"])

        assistant.set_source_language("auto")
        second_id = self._run_session(assistant, ["two"])

        assistant.set_source_language("tr")
        third_id = self._run_session(assistant, ["three"])

        self._assert_session_language(first_id, "en", ["one"])
        self._assert_session_language(second_id, None, ["two"])
        self._assert_session_language(third_id, "tr", ["three"])
        self.assertEqual(
            assistant.transcriber.calls, [("one", "en"), ("two", None), ("three", "tr")]
        )

    def test_config_whisper_language_is_never_mutated(self):
        def snapshot():
            return {k: v for k, v in vars(config).items() if k.isupper()}

        before = snapshot()
        assistant = self._assistant()
        assistant.set_source_language("en")
        self._run_session(assistant, ["a"])
        assistant.set_source_language("auto")
        self._run_session(assistant, ["b"])
        self.assertEqual(snapshot(), before)
        self.assertEqual(config.WHISPER_LANGUAGE, "tr")

    def test_summary_language_is_always_turkish_and_summarizer_calls_unchanged(self):
        assistant = self._assistant(summarizer_cls=RecordingSummarizer)
        for key in ("tr", "en", "auto"):
            assistant.set_source_language(key)
            session_id = self._run_session(assistant, [f"metin-{key}"])
            self.assertEqual(self.repo.get_session(session_id).summary_language, "tr")
        self.assertTrue(assistant.summarizer.summarize_calls)
        for args, kwargs in assistant.summarizer.summarize_calls:
            self.assertEqual(len(args), 1)  # yalnızca transkript metni
            self.assertLessEqual(set(kwargs), {"is_final"})  # dil argümanı SIZMAMALI

    def test_audio_and_llm_facades_are_unaffected(self):
        assistant = self._assistant()
        assistant.set_audio_device(7)
        assistant.set_llm_profile("fast")
        assistant.set_source_language("en")
        assistant.set_source_language("auto")
        self.assertEqual(assistant.capture._device_index, 7)
        self.assertEqual(assistant.get_active_llm_profile_key(), "fast")
        # Ve tersi: ses/LLM değişimi dil seçimini etkilemez.
        assistant.set_audio_device(None)
        assistant.set_llm_profile("quality")
        self.assertEqual(assistant.get_source_language_key(), "auto")
        self.assertEqual(
            assistant.list_audio_devices(), [{"index": 1, "name": "Sahte Loopback Cihazı"}]
        )

    def test_list_source_languages_is_plain_data_copy(self):
        assistant = self._assistant()
        options = assistant.list_source_languages()
        for option in options:
            self.assertEqual(set(option), {"key", "label"})
            self.assertTrue(all(isinstance(v, str) for v in option.values()))
        options.clear()
        self.assertEqual(len(assistant.list_source_languages()), 3)


if __name__ == "__main__":
    unittest.main()
