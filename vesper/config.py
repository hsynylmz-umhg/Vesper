"""
Merkezi ayarlar. Bir şeyi değiştirmek istersen (model boyutu, dil, süre vb.)
sadece burayı düzenlemen yeterli.
"""

# ============================================================
# SES YAKALAMA
# ============================================================
# Whisper 16kHz mono bekliyor; ne olursa olsun buna resample ediyoruz.
TARGET_SAMPLE_RATE = 16000

# webrtcvad frame boyutu (ms). Sadece 10, 20 veya 30 olabilir.
VAD_FRAME_MS = 30

# VAD agresifliği: 0 (en gevşek) - 3 (en agresif, sadece net konuşmayı alır)
VAD_AGGRESSIVENESS = 2

# Bir konuşma parçasını "bitti" saymak için gereken sessizlik süresi (ms)
SILENCE_TIMEOUT_MS = 700

# Konuşma kesintisiz uzarsa (soru-cevap, hızlı konuşmacı vb.) bu süreden
# sonra parçayı zorla Whisper'a gönder — hem gecikmeyi sınırlar hem de
# tek seferde çok uzun ses verip belleği şişirmeyi önler.
MAX_CHUNK_SECONDS = 25

# ============================================================
# ASR — faster-whisper
# ============================================================
# "large-v3-turbo" faster-whisper'ın standart kısaltmaları arasında değil,
# bu yüzden CTranslate2'ye dönüştürülmüş HuggingFace deposunu doğrudan veriyoruz.
WHISPER_MODEL = "deepdml/faster-whisper-large-v3-turbo-ct2"
WHISPER_DEVICE = "cuda"                # Whisper'ı her zaman GPU'da tut (gerçek zamanlı olmalı)
WHISPER_LANGUAGE = "tr"                # v0.4.0: VARSAYILAN konuşma dili seçimi ("tr" | "en" | None = Otomatik)
                                        # Arayüz seçicisi bu değerle başlar. Başka bir değer uyarıyla "tr"ye döner (bkz. vesper/language.py).

# v0.4.0 — Konuşma (kaynak) dili seçenekleri. Resmi seçenekler TAM OLARAK bu üçüdür;
# yalnızca "label" metni ve sıra değiştirilebilir. Anahtar/değer eşlemesi (tr->"tr",
# en->"en", auto->None) sabittir; geçersiz bir tablo uyarıyla yerleşik kopyaya döner.
# Bu seçici YALNIZCA Whisper konuşma dilini belirler; özet dili Türkçe kalır.
SOURCE_LANGUAGES = {
    "tr": {"label": "Türkçe", "whisper_language": "tr"},
    "en": {"label": "English", "whisper_language": "en"},
    "auto": {"label": "Otomatik", "whisper_language": None},   # parça başına otomatik algılama
}

# ============================================================
# ÖZETLEME — Gemma 4 (Ollama üzerinden)
# ============================================================
OLLAMA_URL = "http://localhost:11434"

# ÖNEMLİ (VRAM düzeltmesi): Planlama sırasında Gemma 4 E4B için ~2.5-3GB VRAM
# tahmin etmiştim. Gerçek rakamlar öyle değil: Ollama'da gemma4:e4b (varsayılan)
# ~9.6GB, en agresif "QAT" hali bile ~6.1GB — yani Whisper'la aynı anda 6GB'lık
# karta SIĞMIYOR. Bu yüzden burada daha küçük gemma4:e2b ile başlıyoruz ve
# Gemma'yı CPU'da çalıştırıyoruz (aşağıya bak), GPU'yu tamamen Whisper'a bırakıyoruz.
OLLAMA_MODEL = "gemma4:e2b"

# v0.3.0 — LLM hız/kalite profilleri (yalnızca ÖZETLEME için).
# Her profil SADECE iki alan taşır: "label" (arayüzde görünen ad) ve "model"
# (Ollama'daki AÇIK etiketli model adı, ör. "qwen2.5:3b"; örtük ":latest"
# YOKTUR). OLLAMA_URL, OLLAMA_NUM_CTX ve OLLAMA_FORCE_CPU bilinçli olarak
# profillerin DIŞINDA, global kalır: hiçbir profil LLM'i GPU'ya taşıyamaz
# (Whisper -> GPU, Ollama LLM -> CPU; bkz. aşağıdaki OLLAMA_FORCE_CPU notu).
# Varsayılan "quality" = v0.2.x'te kullanılan gemma4:e2b; yükseltme yapan bir
# kurulum sessizce başka bir modele GEÇMEZ.
# LLM_PROFILES tanımsızsa summarizer.py yukarıdaki OLLAMA_MODEL'den sessizce
# sentetik bir "legacy" profil kurar; tanımlı ama bozuksa (ya da
# DEFAULT_LLM_PROFILE geçersizse) uyarı loglayıp aynı yedeğe düşer.
LLM_PROFILES = {
    "fast": {
        "label": "Hızlı",
        "model": "qwen2.5:3b",
    },
    "quality": {
        "label": "Kaliteli",
        "model": "gemma4:e2b",
    },
}
DEFAULT_LLM_PROFILE = "quality"

# Ollama'nın PC BAŞINA varsayılan bağlam penceresi 4096 token — Gemma 4'ün
# gerçek kapasitesinin (128K) çok altında ve bunu SESSİZCE (hata vermeden) kesiyor.
# Bunu açıkça büyük tutmazsak uzun bir webinarın özeti yarıda kesilmiş transkriptten
# çıkar ve fark etmeyiz. 32K token ~ 1.5-2 saatlik konuşmaya yetiyor.
OLLAMA_NUM_CTX = 32768

# True: Gemma'yı zorla CPU'da çalıştır (16GB RAM'de rahat çalışır, biraz yavaş
#       üretir ama Whisper'ın VRAM'ine hiç dokunmaz — webinar ortasında
#       VRAM çakışmasından donma riski olmaz).
# False: Ollama'nın otomatik GPU/CPU paylaşımına bırak (daha hızlı olabilir
#        ama Whisper ile aynı anda VRAM yarışına girip performans dalgalanmasına
#        veya nadir durumda belleğe sığmama hatasına yol açabilir).
# Önerimiz: önce True ile başla, sorunsuz çalıştığını gördükten sonra
# istersen False deneyip hız farkını kıyasla.
OLLAMA_FORCE_CPU = True

# ============================================================
# ÖZETLEME MANTIĞI
# ============================================================
# Canlı özet kaç saniyede bir güncellensin. Her seferinde BAŞTAN İTİBAREN
# tüm transkript yeniden özetlenir (önceki özete ekleme yapılmaz) — böylece
# küçük hatalar/kaymalar zamanla birikip büyümez. Gemma 4'ün geniş bağlam
# penceresi bunu tek seferde işlemeye yetiyor.
LIVE_SUMMARY_INTERVAL_SEC = 240  # 4 dakika

# ============================================================
# ÇIKTI
# ============================================================
OUTPUT_DIR = "outputs"

# ============================================================
# KALICI OTURUM DEPOSU (SQLite) — v0.2.0
# ============================================================
# outputs/*.md dosyaları artık bir DIŞA AKTARIM; kaynak veri burada.
DB_PATH = "data/vesper.db"

WHISPER_GPU_INDEX = 0
WHISPER_GPU_INIT_TIMEOUT_SEC = 180
WHISPER_ALLOW_CPU_FALLBACK = True
WHISPER_BEAM_SIZE = 5
WHISPER_MIN_CHUNK_SEC = 0.3
WHISPER_MAX_CUDA_RUNTIME_ERRORS = 2
WHISPER_EXTRA_DLL_DIRS = []
# RTX 4050 için float16, int8_float16'dan daha hızlı ve verimlidir
WHISPER_COMPUTE_TYPE = "float16"
