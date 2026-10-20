# CLAUDE.md — Vesper Proje Rehberi

Bu dosya, bu repo üzerinde çalışan her Claude oturumu için bağlayıcı kuralları
içerir. Kod yazmadan, dosya taşımadan veya mimari önerilerde bulunmadan önce
bu dosyanın tamamını oku. Burada yazan bir kuralla kullanıcının anlık isteği
çelişirse, kullanıcıya çelişkiyi açıkça söyle ve netleşene kadar kod yazma.

---

## 1. Vesper Nedir

Vesper, Windows'ta çalışan, tamamen yerel bir "meeting intelligence" (toplantı
zekâsı) sistemidir. Kısa tanım:

> Vesper, toplantıya katılan/yayın kaynağını izleyen, oturumu anlayan,
> konuşmayı ve paylaşılan bilgileri yakalayan ve sonrasında kullanılabilir
> kurumsal hafızaya dönüştüren local-first bir sistemdir.

Vesper'ın bugün hangi özelliklere sahip olduğu, mevcut geliştirme sürümü ve
aktif kapsamı bu dosyada tutulmaz. Güncel kod/dosya durumu için kök dizindeki
`PROJECT_STRUCTURE.md`, sürüm durumu ve aktif geliştirme kapsamı için
`ROADMAP.md` okunmalıdır. Bu ayrım bilinçlidir: CLAUDE.md sürümler arasında
asla değişmeyen kuralları ve tasarım kararlarını taşır.

**Ürün kimliği modele bağlı değildir.** Whisper başka bir ASR motoruyla,
Gemma başka bir LLM ile değişebilir; "yerel, gizlilik odaklı, kullanıcının
donanımına saygılı toplantı zekâsı" kimliği değişmez. Bir özellik önerirken
bu kimliğe aykırıysa (örn. bulut API'sine veri gönderme, kullanıcı onayı
olmadan bir platforma otomatik giriş yapma) uygulamadan önce mutlaka sor.

### Marka dili

- Tagline: *"She was in the room. She took notes."*
- Ürün cümlesi: *"Listens to your webinars, hands you the minutes."*
- Kapanış cümlesi: *"Listen less. Remember more."*
- Vesper her zaman üçüncü tekil şahıs "she/her" ile anılır (İngilizce metinlerde).
- Gelecekteki alt sistemler için isim ayrılmıştır, **değiştirilemez**:
  - **Sotto** → dil/çeviri/çok dilli katman (v0.4+)
  - **Sloane** → görsel/OCR/ekran analizi katmanı (v0.7+)
  - Bu isimler sadece gelecekteki modül adlandırmasında referans olarak
    tutulacaktır (örn. `vesper/sloane/ocr.py`).

---

## 2. Mevcut Kod Durumu — Bkz. PROJECT_STRUCTURE.md ve ROADMAP.md

CLAUDE.md bir durum veya sürüm günlüğü değildir. Kod tabanının fiziksel yapısı,
tamamlanmış özellikler ve aktif geliştirme kapsamı sürümden sürüme değiştiği
için burada kopyalanmaz.

Her çalışma oturumunun başında, kod yazmadan veya mimari öneri yapmadan önce
şu üç dosya bu sırayla okunmalıdır:

1. `CLAUDE.md` — değişmeyen kurallar, donanım kısıtları ve tasarım kararları.
2. `PROJECT_STRUCTURE.md` — repoda şu anda gerçekten bulunan fiziksel yapı
   ve uygulanmış davranışlar.
3. `ROADMAP.md` — sürümlerin geçmiş/gelecek durumu, aktif sürüm ve kapsamı.

"Şu anda ne var?" sorusunda `PROJECT_STRUCTURE.md`; "şu anda ne üzerinde
çalışıyoruz / hangi özellik hangi sürüme ait?" sorusunda `ROADMAP.md`
yetkilidir. Bu dosyalar ile kod arasında çelişki görülürse varsayım yapma;
çelişkiyi kullanıcıya bildir.

Dosya/paket yapısını değiştiren her görevde `PROJECT_STRUCTURE.md` güncellenir.
CLAUDE.md ancak burada tanımlanan kalıcı bir anayasal kural, donanım gerçeği
veya çalışma protokolü gerçekten değiştiğinde düzenlenir.

---

## 3. Donanım ve Ortam — Değiştirilemez Gerçekler

- İşletim sistemi: **Windows 10/11**. Linux/macOS için ayrı destek YOK
  (roadmap'te v1.0 sonrası bir hedef, şu an değil).
- GPU: NVIDIA GeForce RTX 4050 Laptop GPU, **6 GB VRAM**, AMD iGPU ile
  hibrit grafik sistemi.
- RAM: 16 GB.
- CUDA: **CUDA Toolkit 12.8 + cuDNN 9 (CUDA 12 varyantı)**, resmi NVIDIA
  kurulumu üzerinden. **pip'ten `nvidia-cublas-cu12` / `nvidia-cudnn-cu12`
  paketleri KULLANILMAZ.** Bu paketler daha önce sessiz donma (deadlock)
  sorununa yol açmıştı; `vesper/transcriber.py`'deki DLL kayıt mantığı bunu
  önlemek için resmi Toolkit yoluna güvenir. Bu kısıtı kaldırmayı veya
  pip CUDA paketlerini geri getirmeyi ASLA önerme.
- Ollama: LLM modelleri **bilinçli olarak CPU'da** çalışır
  (`OLLAMA_FORCE_CPU = True`). Sebep: 6 GB VRAM'e Whisper + LLM birlikte
  güvenle sığmıyor; Whisper gerçek zamanlı olduğu için GPU'yu o alır.
  Ayrıca bu sistemde Ollama'nın CUDA runner'ı `0xc0000409` hatası veriyordu.
  Bu ayrımı "optimizasyon" gerekçesiyle bozma.
- Destek hedefi: Windows x64, CPython 3.13. Kesin doğrulanmış yalıtılmış
  referans Python **3.13.14 / SQLite 3.53.4**'tür; ortam kanıtı ve dış araç
  bağımlılıkları `release-tooling.json` içindedir. Önceki Python 3.10–3.12 /
  3.11 bilgisi tarihsel referanstır; başka Python/SQLite derlemelerinin güncel
  uyumluluğu varsayılmaz. `requirements.txt` tek başına bu ortamı yeniden üretmez.

---

## 4. Kod Stili Kuralları

- **Docstring ve yorumlar Türkçe yazılır.** Kod (değişken/fonksiyon adları)
  İngilizce kalır. Bu tutarlılığı koru.
- **Loglama:** `logging.getLogger("modül_adı")` deseni kullanılır.
  `print()` yalnızca `main.py`'nin konsol modunda ve bağımsız tanı scriptlerinde
  (`gpu_check.py`, `test_audio.py`) kullanılır.
- **Ayarlar için geriye dönük uyumlu okuma:**
  ```python
  SAMPLE_RATE = getattr(config, "TARGET_SAMPLE_RATE", 16000)
  ```
  Yeni bir ayar alanı eklerken bu deseni kullan; böylece eski yapılandırmayla
  da kod çökmez.
- **Tip ipuçları:** Tip belirteçleri (`Optional`, `List`, `Tuple`, `Dict`)
  kod tabanında tutarlı şekilde kullanılmalıdır; var olan tip ipuçlarını kaldırma.
- **Hata yönetimi:** Sessiz `except: pass` YAZMA. Hataları loglayan,
  türüne göre ayrıştıran ve güvenli kurtarma sağlayan açık bloklar kur.
- **Docstring formatı:** Her dosyanın en üstünde üç tırnaklı modül docstring'i,
  dosyanın amacını, thread modelini ve varsa donanım kısıtlarını açıklamalıdır.

---

## 5. Kritik Tasarım Kararları — SORGULAMADAN KABUL ET

Aşağıdaki kararlar bilinçlidir ve gerekçesi vardır. Bunları "iyileştirme"
adı altında değiştirme; değiştirmek istersen önce kullanıcıya sor:

1. **Whisper GPU'da, LLM CPU'da.** Sebep: VRAM paylaşımı ve gerçek
   zamanlılık önceliği (Bölüm 3).
2. **Her canlı özette tüm transkript baştan özetlenir, artımlı değil.**
   Sebep: Artımlı özetleme küçük hataları katlayarak taşır; her özetin
   kaynağı her zaman ham transkript olmalıdır.
3. **`condition_on_previous_text=False`** (Whisper transkripsiyon parametresi).
   Sebep: Parçalar arası halüsinasyon sızıntısını ve tekrar döngülerini önler.
4. **GPU init bir timeout/watchdog thread altında çalışır.**
   Sebep: CUDA/cuDNN çakışmalarında ana thread'in sonsuza kadar kilitlenmesini engeller.
5. **pip CUDA paketleri yerine resmi NVIDIA Toolkit kurulumu** (Bölüm 3).
6. **Final Markdown çıktısı (`outputs/*.md`) veritabanından üretilir, kaynak değildir.**
   Gerçek veri kaynağı SQLite'tır; `.md` dosyası geriye dönük uyumluluk için
   üretilen bir dışa aktarımdır. Durdur sırasında otomatik oluşturulan dosya bir
   anlık görüntüdür; sonraki Geçmiş düzenleme/silmeleri mevcut dosyayı değiştirmez.
7. **Motor (capture→transcribe→summarize akışı) UI'dan bağımsız çalışır.**
   `vesper/engine.py` NiceGUI'ye kesinlikle import bağımlılığı içermez. UI
   yalnızca motoru gözlemler ve tetikler.
8. **`vesper/__init__.py` dosyası her zaman BOŞ kalır.**
   Böylece hafif modüller (örn. repository/storage) import edilirken native
   donanım bağımlılıkları (WASAPI/CUDA) gereksiz yere erken yüklenmez.
9. **Şema göçü varsayılan olarak reddedilir.** `Database(..., allow_migration=False)`
   korunur; mevcut v1 için açık ve ayrı yükseltme onayı gerekir. Salt-okunur
   inceleme WAL yan dosyalarında sıfır etki garantisi değildir. Üretim göçünün
   güncel onay durumu ROADMAP.md'de tutulur; geliştirme/test izni göç izni değildir.

---

## 6. Dokunulmaz Alanlar

Aşağıdaki bileşenlerin **davranışını** değiştirmeden önce mutlaka kullanıcıdan
açık onay iste:

- `vesper/transcriber.py` içindeki GPU watchdog, DLL kayıt sırası, CPU fallback
  eşiği (`WHISPER_MAX_CUDA_RUNTIME_ERRORS`), compute type fallback sırası.
- `vesper/audio_capture.py` içindeki VAD parametreleri, resample mantığı,
  `_force_exact_length` fonksiyonu.
- `ui.py` içindeki CSS/HTML (renk paleti, animasyonlar, cam panel efekti).
  Mevcut tasarım diline (`.vesper-glass`, `.vesper-btn-primary`) uyum sağla;
  onaysız yeni tasarım dili icat etme.
- `gpu_check.py` ve `test_audio.py` — bağımsız tanı araçlarıdır; ana uygulama
  tarafından import edilmemeli, içeriği bozulmamalıdır.

---

## 7. Sürüm Yol Haritası ve Aktif Kapsam — Bkz. ROADMAP.md

Sürüm numaraları, hangi sürümün yayında olduğu ve aktif sürümün sınırları bu
dosyada sabitlenmez; tek kaynak kök dizindeki `ROADMAP.md` dosyasıdır.

Bir görev verildiğinde önce `ROADMAP.md` üzerinden aktif sürümü ve kapsamını
belirle. Bir özelliğin aktif sürümün dışında olduğu anlaşılırsa onu otomatik
olarak uygulama; hangi sürüme ait olduğunu belirt ve kullanıcıdan onay al.

### Sürümler arası değişmeyen planlama kısıtları

- "Ses cihazı seçimi" ile "uygulama bazlı (process-specific) loopback" aynı
  özellik değildir. Basit cihaz seçimi Python/WASAPI katmanıyla çözülür;
  uygulama bazlı loopback ise harici native program (C#/Rust) gerektiren
  ayrı bir konudur. İkisini kullanıcı onayı olmadan aynı PR'da birleştirme.
- Otomatik toplantı katılımı (v0.6) yalnızca resmî URL şeması/SDK'sı olan
  platformlar için düşünülür (Zoom `zoommtg://`, Teams link). Google Meet ve
  Discord için otomatik katılım planlanmaz.
- Kapanış tespitinde görsel sinyal ancak v0.7 görsel/OCR katmanı geldiğinde
  kullanılabilir; öncesinde ses + zaman sinyaliyle sınırlıdır.
- `artifacts` tablosunun yapısı (v0.5) v0.8'deki provenance özelliklerini
  doğrudan besleyeceği için v0.8 öncesinde geriye dönük uyumsuz bozulmamalıdır.

---

## 8. Çalışma Protokolü

1. **Önce plan, sonra kod.** Değişecek/oluşacak dosyaları, amaçlarını ve veri
   şeması etkilerini belirt. Kullanıcı onaylamadan kod yazma.
2. **Emin olmadığın noktada varsayım yapma.** Durumu açıkça sor.
3. **Kapsam dışına çıkma.** `ROADMAP.md`'deki aktif sürümün ötesindeki
   özellikleri "madem buradayım" diyerek eklemek YASAK.
4. **Regresyon riski taşıyan her değişiklikte açıkça uyar.**
5. **Dosya/dizin yapısını değiştiren her görevde `PROJECT_STRUCTURE.md`'yi
   güncelle.** Bu, görevin tamamlanması için zorunlu adımdır.

---

## 9. Test Edilemeyen Şeyler

Linux veya sandbox ortamlarında **gerçek Windows, gerçek NVIDIA GPU, gerçek ses
cihazı yoktur.** Bu nedenle:

- `audio_capture.py` ve `transcriber.py` donanım yolları sandbox'ta çalıştırılamaz;
  yalnızca söz dizimi (`py_compile`) ve mock testleri yapılabilir.
- Nihai donanım kabulü fiziksel Windows'ta ayrıca yetkilendirilir; mock testleri
  donanım kanıtı yerine geçmez. Mevcut üretim DB'sini varsayılan girişle açma.
  Yalıtılmış kabulde önce DB/çıktı yollarını değiştiren dış başlatıcı kullanılır;
  `gpu_check.py` gibi bağımsız tanılar da ayrı izin gerektirir. Doğrulanmamış
  senaryoyu geçti sayma.

---

## 10. Git ve Commit Kuralları

- Commit mesajları **Conventional Commits** formatında ve İngilizce yazılır:
  `feat:`, `fix:`, `docs:`, `chore:`, `refactor:`.
- Sürüm numaraları **SemVer** standardındadır (`0.x` aralığında).
- Her sürüm için ayrı branch açılır (`v0.3-listens` gibi). Doğrudan `main`'de
  çalışılmaz.
- `.gitignore`: `__pycache__/`, `.venv/`, `outputs/`, `data/` içerir.

---

## 11. Terimler Sözlüğü

| Terim | Anlamı | Not |
|---|---|---|
| Oturum (Session) | Bir kayıt başlangıcından bitişine kadar olan kullanım | SQLite `sessions` tablosunda saklanır |
| Parça (Segment) | VAD ile ayrılmış, Whisper'a giden tek ses/metin birimi | `segments` tablosunda saklanır |
| Canlı özet | Oturum sürerken periyodik üretilen özet | Ham transkriptten üretilir |
| Final özet | Oturum bitince üretilen kapsamlı kapanış tutanağı | SQLite'a yazılır, Markdown çıktısı verilir |
| Motor (Engine) | UI'dan bağımsız çalışan ana iş mantığı | `vesper/engine.py` (NiceGUI bağımlılığı yoktur) |
| CPU fallback | GPU arızasında Whisper'ın CPU moduna geçmesi | Loglanarak yapılır |

---

## 12. Özellik Önerisi Kontrol Listesi

Kullanıcıdan yeni bir fikir geldiğinde şu soruları değerlendir:
1. Vesper'ın "local-first" kimliğini bozuyor mu?
2. Bölüm 5'teki bilinçli kararlardan birini çiğniyor mu?
3. Mevcut sürümün (ROADMAP.md) kapsamında mı, yoksa ertelenmeli mi?
4. Donanıma dokunuyor mu (Bölüm 9)? Kullanıcının neyi test etmesi gerekir?
5. Dokunulmaz bir alana (Bölüm 6) giriyor mu?
