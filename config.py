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
WHISPER_COMPUTE_TYPE = "int8_float16"  # 6GB VRAM'da ~2-3GB civarı yer kaplar
WHISPER_LANGUAGE = "tr"                # Ağırlıklı Türkçe içerik: sabitlemek hız + doğruluk kazandırır
                                        # Webinar bazen İngilizce de içeriyorsa None yap (otomatik algılama, biraz daha yavaş)

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

WHISPER_GPU_INDEX = 0
WHISPER_GPU_INIT_TIMEOUT_SEC = 180
WHISPER_ALLOW_CPU_FALLBACK = True
WHISPER_BEAM_SIZE = 5
WHISPER_MIN_CHUNK_SEC = 0.3
WHISPER_MAX_CUDA_RUNTIME_ERRORS = 2
WHISPER_EXTRA_DLL_DIRS = []
# RTX 4050 için float16, int8_float16'dan daha hızlı ve verimlidir
WHISPER_COMPUTE_TYPE = "float16"
