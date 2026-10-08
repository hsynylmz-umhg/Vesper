"""
Kaynak (konuşma) dili seçeneklerinin ve varsayılan seçimin normalleştirilmesi.

v0.4.0: Vesper'ın resmi olarak desteklediği konuşma dilleri TAM OLARAK üçtür:

    "tr"   -> Whisper "tr"
    "en"   -> Whisper "en"
    "auto" -> Whisper None (parça başına otomatik dil algılama)

Bu modül YALNIZCA bu üç seçeneğin config'ten okunmasını, doğrulanmasını ve
eski `WHISPER_LANGUAGE` ayarının güvenli bir seçime eşlenmesini yapar.
Serbest/keyfi dil kodu KABUL ETMEZ: Transcriber.transcribe() Whisper hatalarını
sessizce yutup "" döndürdüğü için geçersiz bir kod, hiçbir log olmadan tüm
transkriptin kaybolmasına yol açardı. Geçersiz her değer uyarıyla Türkçe'ye döner.

Thread modeli: yalnızca saf fonksiyonlar; paylaşılan değişken durum yok. Her
çağrıda config okunur. Fonksiyonlar istisna fırlatmaz (Transcriber'ın GPU
watchdog thread'inden de çağrılırlar) — tek istisna, çağıran tarafın hatası olan
whisper_language_for()'daki bilinmeyen anahtardır.

Bu modül çeviri veya çok dilli işleme YAPMAZ. Whisper/CUDA, NiceGUI veya Ollama
import etmez; yalnızca `config`'e bağlıdır.
"""

import copy
import logging
from typing import Any, Dict, Optional, Tuple

from . import config

log = logging.getLogger("language")

AUTO_KEY = "auto"
DEFAULT_KEY = "tr"

# Resmi seçenekler. config.SOURCE_LANGUAGES yoksa veya bozuksa BU kopya kullanılır.
# Kanonik kaynak config.py'dir; tests/test_language_selection.py ikisinin
# birbirinden sapmadığını sabitler.
BUILTIN_SOURCE_LANGUAGES: Dict[str, Dict[str, Any]] = {
    "tr": {"label": "Türkçe", "whisper_language": "tr"},
    "en": {"label": "English", "whisper_language": "en"},
    AUTO_KEY: {"label": "Otomatik", "whisper_language": None},
}

_MISSING = object()


def _builtin_options() -> Dict[str, Dict[str, Any]]:
    return copy.deepcopy(BUILTIN_SOURCE_LANGUAGES)


def _same_whisper_value(actual: Any, expected: Optional[str]) -> bool:
    if expected is None:
        return actual is None
    return isinstance(actual, str) and actual == expected


def _validate_table(raw: Any) -> Tuple[Optional[Dict[str, Dict[str, Any]]], str]:
    """(seçenekler, hata) döndürür; geçerliyse hata boştur.

    Tablo TAM OLARAK üç resmi anahtarı içermeli ve her anahtar kendi kanonik
    Whisper değerine eşlenmelidir. Yalnızca etiket metni ve sıra özelleştirilebilir.
    """
    if not isinstance(raw, dict) or not raw:
        return None, "boş olmayan bir sözlük olmalı"
    if set(raw.keys()) != set(BUILTIN_SOURCE_LANGUAGES.keys()):
        return None, "anahtarlar tam olarak 'tr', 'en' ve 'auto' olmalı"

    options: Dict[str, Dict[str, Any]] = {}
    for key, entry in raw.items():
        canonical = BUILTIN_SOURCE_LANGUAGES[key]
        if not isinstance(entry, dict):
            return None, f"'{key}' girdisi bir sözlük değil"
        label = entry.get("label")
        if not isinstance(label, str) or not label.strip():
            return None, f"'{key}' için 'label' boş olmayan bir metin olmalı"
        if not _same_whisper_value(entry.get("whisper_language"), canonical["whisper_language"]):
            return None, f"'{key}' için 'whisper_language' {canonical['whisper_language']!r} olmalı"
        options[key] = {"label": label.strip(), "whisper_language": canonical["whisper_language"]}
    return options, ""


def _load_options() -> Dict[str, Dict[str, Any]]:
    raw = getattr(config, "SOURCE_LANGUAGES", _MISSING)
    if raw is _MISSING:
        return _builtin_options()
    options, error = _validate_table(raw)
    if options is None:
        log.warning(
            "config.SOURCE_LANGUAGES geçersiz (%s); yerleşik Türkçe/English/Otomatik "
            "seçenekleri kullanılıyor.", error,
        )
        return _builtin_options()
    return options


def _resolve_default_key(options: Dict[str, Dict[str, Any]]) -> str:
    raw = getattr(config, "WHISPER_LANGUAGE", _MISSING)
    if raw is _MISSING:
        return DEFAULT_KEY
    if raw is None:
        return AUTO_KEY
    if isinstance(raw, str):
        for key, entry in options.items():
            if entry["whisper_language"] == raw:
                return key
    log.warning(
        "config.WHISPER_LANGUAGE=%r desteklenmiyor (yalnızca 'tr', 'en' veya None "
        "(Otomatik)); Türkçe ('tr') kullanılıyor.", raw,
    )
    return DEFAULT_KEY


def resolve_source_languages() -> Tuple[Dict[str, Dict[str, Any]], str]:
    """(seçenekler, varsayılan_anahtar) döndürür.

    seçenekler: sıralı {anahtar: {"label": str, "whisper_language": Optional[str]}}.
    varsayılan_anahtar: eski `WHISPER_LANGUAGE` ayarından türetilen güvenli seçim.
    """
    options = _load_options()
    return options, _resolve_default_key(options)


def whisper_language_for(options: Dict[str, Dict[str, Any]], key: Any) -> Optional[str]:
    """Seçici anahtarını Whisper değerine eşler ("tr"/"en"/None).

    Bilinmeyen veya metin olmayan anahtar ValueError fırlatır.
    """
    if not isinstance(key, str) or key not in options:
        raise ValueError(
            f"Bilinmeyen konuşma dili: {key!r}. Geçerli seçenekler: {', '.join(options)}"
        )
    return options[key]["whisper_language"]


def resolve_default_whisper_language() -> Optional[str]:
    """Yapılandırılmış varsayılanın GÜVENLİ Whisper değeri ("tr"/"en"/None).

    Transcriber'ın warm-up'ı ve dil argümanı verilmeyen transcribe() çağrıları
    kullanır; böylece elle bozulmuş bir WHISPER_LANGUAGE faster-whisper'a asla
    ham olarak ulaşmaz.
    """
    options, default_key = resolve_source_languages()
    return options[default_key]["whisper_language"]
