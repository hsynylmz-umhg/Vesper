"""
Ollama'da çalışan yerel bir LLM'e transkripti gönderip özet alır.
Hem "canlı" (kısa, akan) hem "final" (webinar bitince, düzenli) formatı destekler.

v0.3.0 — LLM profilleri: config.LLM_PROFILES içindeki küratörlü hız/kalite
profillerinden biri aktiftir ve aktif profilin modeli /api/generate'e
gönderilir. Bir profil SADECE "label" ve "model" taşır; num_ctx ile
num_gpu/force_cpu global kalır (config.OLLAMA_NUM_CTX, config.OLLAMA_FORCE_CPU).
Dolayısıyla hiçbir profil LLM'i GPU'ya taşıyamaz: Whisper -> GPU, Ollama LLM ->
CPU (CLAUDE.md §3, §5.1).

Model varlığı YALNIZCA yerel `GET {OLLAMA_URL}/api/tags` ile ve TAM etiket
eşleşmesiyle doğrulanır: bulanık / alt dize / büyük-küçük harf duyarsız eşleşme
ve örtük ":latest" genişletmesi YOKTUR. Bulut çağrısı ve otomatik `ollama pull`
YOKTUR. Doğrulama yalnızca set_profile() içinde yapılır; her summarize()
çağrısından önce ön kontrol YAPILMAZ (çalışma-zamanı istek hataları engine'deki
canlı özet işçisinin dayanıklılığıyla ele alınır).

Thread modeli: aynı Summarizer örneğini canlı özet işçisi ve stop() içindeki
final özet çağırır. Profil tablosu oluşturulduktan sonra değişmez; değişen tek
durum, tek bir referans ataması olan aktif profil anahtarıdır. Oturum sürerken
profil değişimi engine katmanında reddedilir (bkz. WebinarAssistant.set_llm_profile).
"""

import logging
from typing import Dict, List, Set, Tuple

import requests

from . import config

log = logging.getLogger("summarizer")

# LLM_PROFILES tanımsız/bozuksa OLLAMA_MODEL'den kurulan sentetik profilin anahtarı.
_LEGACY_PROFILE_KEY = "legacy"

# /api/tags yerel ve hızlı bir çağrıdır; Ollama takılırsa arayüz uzun süre bloke olmasın.
_TAGS_TIMEOUT_SEC = 3

_MISSING = object()

LIVE_PROMPT = """Aşağıda bir webinarın o ana kadarki transkripti var. Türkçe olarak özetle.

Kurallar:
- Madde madde en önemli noktaları yaz.
- Varsa "Aksiyon Maddeleri / Kararlar" başlığı altında somut kararları veya yapılacakları ayrıca listele.
- Sadece transkriptte gerçekten geçenleri yaz, tahmin veya uydurma yapma.
- Kısa ve öz ol, bu senin ilk taslağın değil, canlı takip eden bir özet.

TRANSKRİPT:
{transcript}

ÖZET:"""

FINAL_PROMPT = """Aşağıda tamamlanmış bir webinarın tüm transkripti var. Kapsamlı ve düzenli bir final özeti hazırla.

Şu formatı kullan:

## Genel Özet
(webinarın ana konusu ve akışı, 3-5 cümle)

## Ana Noktalar
(madde madde en önemli içerik noktaları)

## Aksiyon Maddeleri / Kararlar
(varsa somut kararlar, yapılacaklar, deadline'lar; yoksa "Belirtilmedi" yaz)

Sadece transkriptte gerçekten geçenleri yaz, tahmin veya uydurma yapma.

TRANSKRİPT:
{transcript}

FİNAL ÖZET:"""


def _legacy_profiles() -> Tuple[Dict[str, Dict[str, str]], str]:
    """OLLAMA_MODEL'den tek profilli, sentetik (eski uyumlu) bir tablo kurar."""
    model = getattr(config, "OLLAMA_MODEL", "gemma4:e2b")
    profiles = {_LEGACY_PROFILE_KEY: {"label": "Varsayılan", "model": model}}
    return profiles, _LEGACY_PROFILE_KEY


def _has_explicit_tag(model) -> bool:
    """Model adının açık bir etiket taşıyıp taşımadığını denetler ('ad:etiket').

    Örtük ':latest' YOKTUR: etiketsiz bir ad geçersizdir. Yol önekleri
    ('hf.co/kullanici/model:etiket') ve kayıt defteri portu ('host:5000/model:etiket')
    bozulmasın diye yalnızca SON yol parçasına bakılır."""
    if not isinstance(model, str) or any(ch.isspace() for ch in model):
        return False
    parts = model.rsplit("/", 1)[-1].split(":")
    return len(parts) == 2 and all(parts)


def _validate_profiles(raw, default_key) -> Dict[str, Dict[str, str]]:
    """Ham LLM_PROFILES + DEFAULT_LLM_PROFILE'ı doğrular.

    Geçerliyse {anahtar: {"label", "model"}} döner; herhangi bir sorun varsa
    okunabilir bir mesajla ValueError fırlatır (çağıran uyarı loglayıp yedeğe düşer).
    Profillerdeki fazladan alanlar (ör. num_gpu) YOK SAYILIR ve uyarı loglanır —
    hiçbir profil global CPU-only kuralını (OLLAMA_FORCE_CPU) ezemez."""
    if not isinstance(raw, dict) or not raw:
        raise ValueError("LLM_PROFILES boş olmayan bir sözlük olmalı")

    profiles: Dict[str, Dict[str, str]] = {}
    for key, entry in raw.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"geçersiz profil anahtarı: {key!r}")
        if not isinstance(entry, dict):
            raise ValueError(f"'{key}' profili bir sözlük olmalı")

        label = entry.get("label")
        if not isinstance(label, str) or not label.strip():
            raise ValueError(f"'{key}' profilinde geçerli bir 'label' yok")

        model = entry.get("model")
        if not _has_explicit_tag(model):
            raise ValueError(
                f"'{key}' profilinin modeli açık etiketli olmalı (ör. 'qwen2.5:3b'): {model!r}"
            )

        extras = sorted(str(name) for name in entry if name not in ("label", "model"))
        if extras:
            log.warning(
                "'%s' profilindeki desteklenmeyen alanlar yok sayıldı: %s "
                "(num_ctx / num_gpu / force_cpu gibi ayarlar global kalır)",
                key, ", ".join(extras),
            )

        profiles[key] = {"label": label.strip(), "model": model}

    if not isinstance(default_key, str) or default_key not in profiles:
        raise ValueError(
            f"DEFAULT_LLM_PROFILE geçersiz: {default_key!r} "
            f"(geçerli anahtarlar: {', '.join(profiles)})"
        )
    return profiles


def _resolve_profiles() -> Tuple[Dict[str, Dict[str, str]], str]:
    """Aktif yapılandırmadan (profil tablosu, varsayılan anahtar) çifti üretir.

    - LLM_PROFILES TANIMSIZ  -> sessizce OLLAMA_MODEL'den sentetik 'legacy' profil.
    - LLM_PROFILES bozuk ya da DEFAULT_LLM_PROFILE geçersiz -> açık bir UYARI
      loglayıp aynı sentetik profile düşer. Import/başlangıçta ÇÖKMEZ."""
    raw = getattr(config, "LLM_PROFILES", _MISSING)
    if raw is _MISSING:
        return _legacy_profiles()

    default_key = getattr(config, "DEFAULT_LLM_PROFILE", None)
    try:
        return _validate_profiles(raw, default_key), default_key
    except ValueError as exc:
        log.warning(
            "Geçersiz LLM profil yapılandırması (%s); OLLAMA_MODEL tabanlı "
            "'%s' profiline dönülüyor.", exc, _LEGACY_PROFILE_KEY,
        )
        return _legacy_profiles()


class Summarizer:
    def __init__(self):
        self._profiles, self._active_key = _resolve_profiles()
        self._check_ollama()

    # ------------------------------------------------------------------ #
    # v0.3.0 — LLM profilleri. UI bunlara doğrudan DEĞİL, engine üzerinden
    # ulaşır (bkz. WebinarAssistant.list_llm_profiles / set_llm_profile).
    def list_profiles(self) -> List[Dict[str, str]]:
        """[{"key", "label", "model"}, ...] — yapılandırmadaki sırayla, düz veri."""
        return [
            {"key": key, "label": profile["label"], "model": profile["model"]}
            for key, profile in self._profiles.items()
        ]

    def get_active_profile_key(self) -> str:
        return self._active_key

    def get_active_model(self) -> str:
        return self._profiles[self._active_key]["model"]

    def set_profile(self, key: str) -> None:
        """Aktif profili değiştirir. Bilinmeyen anahtar ValueError; hedef model
        Ollama'da yoksa (ya da Ollama'ya ulaşılamıyorsa) RuntimeError fırlatır.

        Hedef modelin varlığı durum DEĞİŞMEDEN ÖNCE doğrulanır: doğrulama
        başarısız olursa önceki aktif profil/model aynen kalır."""
        if not isinstance(key, str) or key not in self._profiles:
            raise ValueError(
                f"Bilinmeyen LLM profili: {key!r}. "
                f"Geçerli profiller: {', '.join(self._profiles)}"
            )

        model = self._profiles[key]["model"]
        installed = self._fetch_installed_model_names()
        if model not in installed:
            raise RuntimeError(
                f"'{model}' modeli Ollama'da yüklü değil, profil değiştirilemedi. "
                f"Gerekirse kendin indir: 'ollama pull {model}'"
            )

        self._active_key = key
        log.info("LLM profili değiştirildi: %s (%s)", key, model)

    @staticmethod
    def _fetch_installed_model_names() -> Set[str]:
        """Yerel Ollama'dan (GET /api/tags) kurulu model adlarını döner.

        Her giriş `entry.get("name") or entry.get("model")` ile tanımlanır. Bulut
        çağrısı YOK. Bağlantı/zaman aşımı/HTTP/geçersiz-yanıt hataları arayüzde
        gösterilebilecek açık RuntimeError'lara çevrilir."""
        url = f"{config.OLLAMA_URL}/api/tags"
        try:
            response = requests.get(url, timeout=_TAGS_TIMEOUT_SEC)
            response.raise_for_status()
        except requests.exceptions.Timeout as exc:
            raise RuntimeError(
                f"Ollama {_TAGS_TIMEOUT_SEC} sn içinde yanıt vermedi ({config.OLLAMA_URL})."
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise RuntimeError(
                "Ollama'ya bağlanılamadı. Ollama'nın kurulu ve çalışır durumda "
                f"olduğundan emin ol (varsayılan adres: {config.OLLAMA_URL})."
            ) from exc
        except requests.exceptions.HTTPError as exc:
            status = getattr(exc.response, "status_code", "bilinmiyor")
            raise RuntimeError(
                f"Ollama model listesi isteğine HTTP {status} ile yanıt verdi."
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(f"Ollama model listesi alınamadı: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("Ollama geçersiz (JSON olmayan) bir model listesi döndürdü.") from exc

        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            raise RuntimeError("Ollama'nın model listesi beklenen biçimde değil ('models' listesi yok).")

        names: Set[str] = set()
        for entry in models:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name") or entry.get("model")
            if isinstance(name, str) and name:
                names.add(name)
        return names

    def _check_ollama(self):
        try:
            requests.get(f"{config.OLLAMA_URL}/api/tags", timeout=3)
        except requests.exceptions.ConnectionError:
            raise RuntimeError(
                "Ollama'ya bağlanılamadı. Ollama'nın kurulu ve çalışır durumda "
                f"olduğundan emin ol (varsayılan adres: {config.OLLAMA_URL}). "
                f"Model henüz indirilmediyse: 'ollama pull {self.get_active_model()}'"
            )

    def summarize(self, transcript: str, is_final: bool = False) -> str:
        if not transcript.strip():
            return "Henüz yeterli transkript yok."

        prompt_template = FINAL_PROMPT if is_final else LIVE_PROMPT
        prompt = prompt_template.format(transcript=transcript)
        model = self.get_active_model()

        response = requests.post(
            f"{config.OLLAMA_URL}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    # Ollama'nın 4096'lık varsayılan bağlamını ezmek şart, aksi halde
                    # uzun transkriptler sessizce kesilir (bkz. config.py'deki not).
                    "num_ctx": config.OLLAMA_NUM_CTX,
                    "temperature": 0.3,  # özetlemede tutarlılık, uydurma riskini azaltmak için düşük
                    # CPU-only: yalnızca GLOBAL OLLAMA_FORCE_CPU belirler; profiller GPU ayarı taşımaz.
                    "num_gpu": 0 if config.OLLAMA_FORCE_CPU else -1,
                },
            },
            timeout=600,  # uzun transkript + CPU üretimi zaman alabilir
        )
        response.raise_for_status()
        return response.json().get("response", "").strip()
