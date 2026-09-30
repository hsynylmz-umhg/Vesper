"""
v0.3.0 Checkpoint B — vesper.summarizer LLM profil mantığı için MOCK'lu testler.

GERÇEK Ollama HTTP'si YOK: requests.get / requests.post yamalanır ve summarizer
modülünün `config`'i, testin istediği ayarları taşıyan bir SimpleNamespace ile
değiştirilir (bu sayede "LLM_PROFILES tanımsız" gibi durumlar da temsil edilebilir).
Yalnızca `test_real_config_*` testleri gerçek vesper/config.py'yi okur —
onaylanmış iki profilin ve CPU-only kuralının gerçekten yapılandırmada
durduğunu kanıtlamak için.

Çalıştırma:
    python -m unittest discover -s tests -t .
"""

import copy
import os
import sys
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import requests

from vesper import config as real_config
from vesper import summarizer as summarizer_module
from vesper.summarizer import Summarizer

_ABSENT = object()  # bir config alanının HİÇ tanımlanmamasını temsil eder

APPROVED_PROFILES = {
    "fast": {"label": "Hızlı", "model": "qwen2.5:3b"},
    "quality": {"label": "Kaliteli", "model": "gemma4:e2b"},
}

LEGACY_ONLY = [{"key": "legacy", "label": "Varsayılan", "model": "legacy-model:1b"}]


def _config(**overrides):
    """Summarizer'ın okuduğu config'i taklit eder. Değeri _ABSENT olan alan
    namespace'e HİÇ konmaz (getattr varsayılanı devreye girer)."""
    values = {
        "OLLAMA_URL": "http://ollama.test:11434",
        "OLLAMA_NUM_CTX": 12345,  # gerçek 32768'den farklı: 'global config okundu' kanıtı
        "OLLAMA_FORCE_CPU": True,
        "OLLAMA_MODEL": "legacy-model:1b",
        "LLM_PROFILES": copy.deepcopy(APPROVED_PROFILES),
        "DEFAULT_LLM_PROFILE": "quality",
    }
    values.update(overrides)
    return SimpleNamespace(**{k: v for k, v in values.items() if v is not _ABSENT})


class _FakeResponse:
    """requests.Response'un bu testlerin ihtiyaç duyduğu küçük bir taklidi."""

    def __init__(self, status_code=200, payload=None, json_error=None):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


def _tags(*names, key="name"):
    """Ollama'nın GET /api/tags yanıtını taklit eder; `key` girişin alan adıdır."""
    return _FakeResponse(payload={"models": [{key: name} for name in names]})


class ProfileTestCase(unittest.TestCase):
    def setUp(self):
        self._stack = ExitStack()
        self.addCleanup(self._stack.close)

    def build(self, tags=("gemma4:e2b", "qwen2.5:3b"), get_error=None, **overrides):
        """Yamalı config + yamalı requests.get/post ile bir Summarizer kurar.
        Yamalar test bitene kadar açık kalır (Summarizer config'i çağrı anında okur)."""
        self.cfg = _config(**overrides)
        self._stack.enter_context(patch.object(summarizer_module, "config", self.cfg))
        self.get = self._stack.enter_context(patch.object(summarizer_module.requests, "get"))
        self.post = self._stack.enter_context(patch.object(summarizer_module.requests, "post"))
        self.post.return_value = _FakeResponse(payload={"response": "  özet  "})
        if get_error is not None:
            self.get.side_effect = get_error
        else:
            self.get.return_value = _tags(*tags)
        summarizer = Summarizer()
        self.get.reset_mock()  # kurucunun bağlantı denetimini sayaçtan çıkar
        return summarizer

    def build_with_real_config(self):
        """GERÇEK vesper/config.py ile; yalnızca HTTP yamalı."""
        self.get = self._stack.enter_context(patch.object(summarizer_module.requests, "get"))
        self.post = self._stack.enter_context(patch.object(summarizer_module.requests, "post"))
        self.get.return_value = _tags("gemma4:e2b", "qwen2.5:3b")
        self.post.return_value = _FakeResponse(payload={"response": "özet"})
        summarizer = Summarizer()
        self.get.reset_mock()
        return summarizer


class TestProfileResolution(ProfileTestCase):
    def test_real_config_lists_the_two_approved_profiles(self):
        summarizer = self.build_with_real_config()
        self.assertEqual(
            summarizer.list_profiles(),
            [
                {"key": "fast", "label": "Hızlı", "model": "qwen2.5:3b"},
                {"key": "quality", "label": "Kaliteli", "model": "gemma4:e2b"},
            ],
        )

    def test_real_config_quality_is_default(self):
        summarizer = self.build_with_real_config()
        self.assertEqual(summarizer.get_active_profile_key(), "quality")
        self.assertEqual(summarizer.get_active_model(), "gemma4:e2b")
        self.assertEqual(real_config.DEFAULT_LLM_PROFILE, "quality")

    def test_real_config_profiles_carry_only_label_and_model(self):
        """Profiller yalnızca label+model taşır; num_ctx/num_gpu/force_cpu global kalır."""
        for key, profile in real_config.LLM_PROFILES.items():
            with self.subTest(profile=key):
                self.assertEqual(set(profile), {"label", "model"})

    def test_legacy_profile_when_llm_profiles_absent(self):
        with self.assertNoLogs("summarizer", level="WARNING"):
            summarizer = self.build(LLM_PROFILES=_ABSENT, DEFAULT_LLM_PROFILE=_ABSENT)
        self.assertEqual(summarizer.list_profiles(), LEGACY_ONLY)
        self.assertEqual(summarizer.get_active_profile_key(), "legacy")
        self.assertEqual(summarizer.get_active_model(), "legacy-model:1b")

    def test_legacy_profile_uses_builtin_default_when_ollama_model_also_absent(self):
        summarizer = self.build(
            LLM_PROFILES=_ABSENT, DEFAULT_LLM_PROFILE=_ABSENT, OLLAMA_MODEL=_ABSENT
        )
        self.assertEqual(summarizer.get_active_model(), "gemma4:e2b")

    def test_malformed_profiles_warn_and_fall_back_to_legacy(self):
        good = {"label": "Kaliteli", "model": "gemma4:e2b"}
        malformed = {
            "not a dict": "hizli,kaliteli",
            "empty dict": {},
            "explicit None": None,
            "entry is not a dict": {"fast": "qwen2.5:3b", "quality": dict(good)},
            "missing label": {"quality": {"model": "gemma4:e2b"}},
            "blank label": {"quality": {"label": "   ", "model": "gemma4:e2b"}},
            "missing model": {"quality": {"label": "Kaliteli"}},
            "model without tag": {"quality": {"label": "Kaliteli", "model": "gemma4"}},
            "model with empty tag": {"quality": {"label": "Kaliteli", "model": "gemma4:"}},
            "non-string key": {3: {"label": "x", "model": "a:b"}},
        }
        for name, raw in malformed.items():
            with self.subTest(case=name):
                with self.assertLogs("summarizer", level="WARNING") as captured:
                    summarizer = self.build(LLM_PROFILES=raw, DEFAULT_LLM_PROFILE="quality")
                self.assertTrue(
                    any("Geçersiz LLM profil yapılandırması" in line for line in captured.output),
                    captured.output,
                )
                self.assertEqual(summarizer.list_profiles(), LEGACY_ONLY)
                self.assertEqual(summarizer.get_active_profile_key(), "legacy")
                self.assertEqual(summarizer.get_active_model(), "legacy-model:1b")

    def test_invalid_default_profile_warns_and_falls_back_to_legacy(self):
        for name, default in (
            ("unknown key", "ghost"),
            ("absent", _ABSENT),
            ("wrong type", 123),
        ):
            with self.subTest(case=name):
                with self.assertLogs("summarizer", level="WARNING") as captured:
                    summarizer = self.build(DEFAULT_LLM_PROFILE=default)
                self.assertTrue(
                    any("DEFAULT_LLM_PROFILE geçersiz" in line for line in captured.output),
                    captured.output,
                )
                self.assertEqual(summarizer.list_profiles(), LEGACY_ONLY)
                self.assertEqual(summarizer.get_active_model(), "legacy-model:1b")

    def test_configured_model_without_explicit_tag_is_rejected_never_expanded_to_latest(self):
        with self.assertLogs("summarizer", level="WARNING") as captured:
            summarizer = self.build(
                LLM_PROFILES={"quality": {"label": "Kaliteli", "model": "gemma4"}},
                DEFAULT_LLM_PROFILE="quality",
            )
        self.assertTrue(any("açık etiketli" in line for line in captured.output), captured.output)
        # ':latest' TÜRETİLMEZ: geçersiz profil tablosu yerine legacy kurulur.
        self.assertEqual(summarizer.get_active_profile_key(), "legacy")
        self.assertNotIn("gemma4:latest", [p["model"] for p in summarizer.list_profiles()])

    def test_explicit_tag_rules(self):
        has_tag = summarizer_module._has_explicit_tag
        for model in (
            "qwen2.5:3b",
            "gemma4:e2b",
            "gemma4:latest",
            "hf.co/user/model:Q4_K_M",
            "localhost:5000/team/model:1b",
        ):
            with self.subTest(valid=model):
                self.assertTrue(has_tag(model))
        for model in (
            "qwen2.5",
            "qwen2.5:",
            ":3b",
            "a:b:c",
            "localhost:5000/model",
            "qwen 2.5:3b",
            "",
            None,
            3,
        ):
            with self.subTest(invalid=model):
                self.assertFalse(has_tag(model))

    def test_unsupported_profile_fields_are_ignored_with_a_warning(self):
        hostile = copy.deepcopy(APPROVED_PROFILES)
        hostile["fast"].update({"num_gpu": 99, "num_ctx": 1, "force_cpu": False, "temperature": 2.0})
        with self.assertLogs("summarizer", level="WARNING") as captured:
            summarizer = self.build(LLM_PROFILES=hostile)
        self.assertTrue(
            any("desteklenmeyen alanlar" in line for line in captured.output), captured.output
        )
        # Profil tablosu geçerli kalır; fazladan alanlar hiçbir yerde açığa çıkmaz.
        self.assertEqual(
            summarizer.list_profiles(),
            [
                {"key": "fast", "label": "Hızlı", "model": "qwen2.5:3b"},
                {"key": "quality", "label": "Kaliteli", "model": "gemma4:e2b"},
            ],
        )
        summarizer.set_profile("fast")
        summarizer.summarize("merhaba")
        sent = self.post.call_args.kwargs["json"]
        self.assertEqual(sent["options"]["num_gpu"], 0, "profil CPU-only kuralını ezemez")
        self.assertEqual(sent["options"]["num_ctx"], 12345, "profil global num_ctx'i ezemez")
        self.assertEqual(sent["options"]["temperature"], 0.3)


class TestSetProfile(ProfileTestCase):
    def test_successful_switch_updates_key_and_model(self):
        summarizer = self.build()
        summarizer.set_profile("fast")
        self.assertEqual(summarizer.get_active_profile_key(), "fast")
        self.assertEqual(summarizer.get_active_model(), "qwen2.5:3b")

        summarizer.set_profile("quality")
        self.assertEqual(summarizer.get_active_profile_key(), "quality")
        self.assertEqual(summarizer.get_active_model(), "gemma4:e2b")

    def test_switch_validates_against_local_api_tags_only(self):
        summarizer = self.build()
        summarizer.set_profile("fast")
        self.get.assert_called_once()
        self.assertEqual(self.get.call_args.args[0], "http://ollama.test:11434/api/tags")
        self.post.assert_not_called()  # hiçbir üretim/indirme çağrısı yok

    def test_unknown_key_is_rejected_without_any_network_call(self):
        summarizer = self.build()
        for key in ("ghost", "", None, 7):
            with self.subTest(key=key):
                with self.assertRaises(ValueError) as ctx:
                    summarizer.set_profile(key)
                self.assertIn("Bilinmeyen LLM profili", str(ctx.exception))
        self.get.assert_not_called()
        self.assertEqual(summarizer.get_active_profile_key(), "quality")

    def test_unavailable_model_is_rejected_and_state_unchanged(self):
        summarizer = self.build(tags=("gemma4:e2b",))  # qwen2.5:3b kurulu DEĞİL
        with self.assertRaises(RuntimeError) as ctx:
            summarizer.set_profile("fast")
        self.assertIn("qwen2.5:3b", str(ctx.exception))
        self.assertEqual(summarizer.get_active_profile_key(), "quality")
        self.assertEqual(summarizer.get_active_model(), "gemma4:e2b")

    def test_failed_switch_leaves_previous_profile_and_model_unchanged(self):
        summarizer = self.build()
        summarizer.set_profile("fast")

        # 1) Bağlantı hatası
        self.get.side_effect = requests.exceptions.ConnectionError("refused")
        with self.assertRaises(RuntimeError):
            summarizer.set_profile("quality")
        self.assertEqual(summarizer.get_active_profile_key(), "fast")
        self.assertEqual(summarizer.get_active_model(), "qwen2.5:3b")

        # 2) Hedef model listede yok
        self.get.side_effect = None
        self.get.return_value = _tags("qwen2.5:3b")
        with self.assertRaises(RuntimeError):
            summarizer.set_profile("quality")
        self.assertEqual(summarizer.get_active_profile_key(), "fast")
        self.assertEqual(summarizer.get_active_model(), "qwen2.5:3b")

        # 3) Bilinmeyen anahtar
        with self.assertRaises(ValueError):
            summarizer.set_profile("ghost")
        self.assertEqual(summarizer.get_active_profile_key(), "fast")

    def test_tags_connection_failure_becomes_clear_local_error(self):
        summarizer = self.build()
        self.get.side_effect = requests.exceptions.ConnectionError("refused")
        with self.assertRaises(RuntimeError) as ctx:
            summarizer.set_profile("fast")
        self.assertIn("bağlanılamadı", str(ctx.exception))
        self.assertEqual(summarizer.get_active_profile_key(), "quality")

    def test_tags_timeout_becomes_clear_local_error(self):
        summarizer = self.build()
        self.get.side_effect = requests.exceptions.Timeout("slow")
        with self.assertRaises(RuntimeError) as ctx:
            summarizer.set_profile("fast")
        self.assertIn("yanıt vermedi", str(ctx.exception))
        self.assertEqual(summarizer.get_active_profile_key(), "quality")

    def test_tags_http_failure_becomes_clear_local_error(self):
        summarizer = self.build()
        self.get.return_value = _FakeResponse(status_code=500)
        with self.assertRaises(RuntimeError) as ctx:
            summarizer.set_profile("fast")
        self.assertIn("HTTP 500", str(ctx.exception))
        self.assertEqual(summarizer.get_active_profile_key(), "quality")

    def test_tags_invalid_response_becomes_clear_local_error(self):
        bad_responses = {
            "not json": _FakeResponse(json_error=ValueError("no json")),
            "missing models key": _FakeResponse(payload={"unexpected": []}),
            "top level is a list": _FakeResponse(payload=["qwen2.5:3b"]),
            "models is null": _FakeResponse(payload={"models": None}),
        }
        summarizer = self.build()
        for name, response in bad_responses.items():
            with self.subTest(case=name):
                self.get.return_value = response
                with self.assertRaises(RuntimeError) as ctx:
                    summarizer.set_profile("fast")
                self.assertIn("Ollama", str(ctx.exception))
                self.assertEqual(summarizer.get_active_profile_key(), "quality")

    def test_tags_entries_identified_by_name_field(self):
        summarizer = self.build(tags=("gemma4:e2b", "qwen2.5:3b"))  # {"name": ...}
        summarizer.set_profile("fast")
        self.assertEqual(summarizer.get_active_model(), "qwen2.5:3b")

    def test_tags_entries_identified_by_model_field(self):
        summarizer = self.build()
        self.get.return_value = _tags("gemma4:e2b", "qwen2.5:3b", key="model")  # {"model": ...}
        summarizer.set_profile("fast")
        self.assertEqual(summarizer.get_active_model(), "qwen2.5:3b")

    def test_name_field_takes_precedence_over_model_field(self):
        """Tanımlama: entry.get("name") or entry.get("model")."""
        summarizer = self.build()
        self.get.return_value = _FakeResponse(
            payload={"models": [{"name": "other:1b", "model": "qwen2.5:3b"}]}
        )
        with self.assertRaises(RuntimeError):
            summarizer.set_profile("fast")
        self.assertEqual(summarizer.get_active_profile_key(), "quality")

    def test_model_match_is_exact_no_fuzzy_substring_case_or_implicit_latest(self):
        near_misses = (
            "qwen2.5:3b-instruct",
            "Qwen2.5:3B",
            "qwen2.5:3B",
            "qwen2.5",
            "qwen2.5:latest",
            "xqwen2.5:3b",
            "library/qwen2.5:3b",
            "qwen2.5:3b ",
            " qwen2.5:3b",
        )
        summarizer = self.build(tags=("gemma4:e2b",) + near_misses)
        with self.assertRaises(RuntimeError):
            summarizer.set_profile("fast")
        self.assertEqual(summarizer.get_active_profile_key(), "quality")

        # Aynı yakın-ıskalamaların YANINDA tam etiket varsa kabul edilir.
        self.get.return_value = _tags(*near_misses, "qwen2.5:3b")
        summarizer.set_profile("fast")
        self.assertEqual(summarizer.get_active_model(), "qwen2.5:3b")


class TestSummarizeRequest(ProfileTestCase):
    def test_generate_request_uses_the_active_profile_model(self):
        summarizer = self.build()
        result = summarizer.summarize("merhaba", is_final=False)
        self.assertEqual(result, "özet", "yanıt kırpılarak dönmeli")
        self.assertEqual(self.post.call_args.args[0], "http://ollama.test:11434/api/generate")
        self.assertEqual(self.post.call_args.kwargs["json"]["model"], "gemma4:e2b")

        summarizer.set_profile("fast")
        summarizer.summarize("merhaba", is_final=True)
        self.assertEqual(self.post.call_args.kwargs["json"]["model"], "qwen2.5:3b")

    def test_global_num_ctx_is_used_for_every_profile(self):
        summarizer = self.build()
        for key in ("quality", "fast"):
            with self.subTest(profile=key):
                summarizer.set_profile(key)
                summarizer.summarize("merhaba")
                options = self.post.call_args.kwargs["json"]["options"]
                self.assertEqual(options["num_ctx"], self.cfg.OLLAMA_NUM_CTX)

    def test_cpu_only_num_gpu_is_zero_with_force_cpu(self):
        summarizer = self.build(OLLAMA_FORCE_CPU=True)
        for key in ("quality", "fast"):
            with self.subTest(profile=key):
                summarizer.set_profile(key)
                summarizer.summarize("merhaba")
                self.assertEqual(self.post.call_args.kwargs["json"]["options"]["num_gpu"], 0)

    def test_num_gpu_derives_only_from_the_global_flag(self):
        summarizer = self.build(OLLAMA_FORCE_CPU=False)
        for key in ("quality", "fast"):
            with self.subTest(profile=key):
                summarizer.set_profile(key)
                summarizer.summarize("merhaba")
                self.assertEqual(self.post.call_args.kwargs["json"]["options"]["num_gpu"], -1)

    def test_real_config_is_cpu_only_and_num_gpu_zero_for_both_profiles(self):
        self.assertIs(real_config.OLLAMA_FORCE_CPU, True)
        summarizer = self.build_with_real_config()
        for key in ("quality", "fast"):
            with self.subTest(profile=key):
                summarizer.set_profile(key)
                summarizer.summarize("merhaba")
                options = self.post.call_args.kwargs["json"]["options"]
                self.assertEqual(options["num_gpu"], 0)
                self.assertEqual(options["num_ctx"], real_config.OLLAMA_NUM_CTX)

    def test_summarize_does_not_preflight_api_tags(self):
        summarizer = self.build()
        summarizer.summarize("merhaba", is_final=False)
        summarizer.summarize("merhaba", is_final=True)
        self.get.assert_not_called()

    def test_empty_transcript_skips_http_entirely(self):
        summarizer = self.build()
        self.assertEqual(summarizer.summarize("   "), "Henüz yeterli transkript yok.")
        self.post.assert_not_called()
        self.get.assert_not_called()

    def test_constructor_reports_unreachable_ollama_with_active_model_hint(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.build(get_error=requests.exceptions.ConnectionError("refused"))
        self.assertIn("ollama pull gemma4:e2b", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
