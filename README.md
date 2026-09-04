<p align="center">
  <img src="assets/Vesper.jpg" width="260" alt="Vesper">
</p>

<h1 align="center">Vesper</h1>

<p align="center">
  <i>She was in the room. She took notes.</i><br>
  <b>Listens to your webinars, hands you the minutes. Local Whisper + Gemma, no cloud.</b>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/platform-Windows%2010%2F11-0078D6?logo=windows&logoColor=white" alt="Windows">
  <img src="https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-3776AB?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/GPU-NVIDIA%20CUDA%2012-76B900?logo=nvidia&logoColor=white" alt="CUDA 12">
  <img src="https://img.shields.io/badge/license-MIT-lightgrey" alt="MIT">
</p>

---

Vesper, bilgisayarında oynayan webinarı, canlı dersi veya toplantıyı **hoparlör çıkışından** dinler; Türkçe transkript çıkarır, konuşma sürerken birkaç dakikada bir güncellenen bir **canlı özet** tutar ve oturum bitince **tutanak formatında bir final özeti** (`.md`) bırakır.

Her şey kendi makinende çalışır. Ses hiçbir sunucuya gitmez, API anahtarı gerekmez, modeller bir kez iner ve sonrasında internet olmadan da çalışır.

## Nasıl çalışır

```
 Hoparlör çıkışı            Konuşma parçaları           Metin                    Özet
 (WASAPI loopback)  ──►  webrtcvad ile bölme  ──►  faster-whisper       ──►  Gemma 4 (Ollama)
                         (sessizlikte kes,          large-v3-turbo           CPU, 32K bağlam
                          maks. 25 sn)              NVIDIA GPU, int8_fp16    4 dk'da bir + final
```

- **Ses yakalama:** Windows'un yerleşik WASAPI loopback özelliği. Sanal ses kablosu, ek sürücü, mikrofon yok — kulaklıktan ne duyuyorsan Vesper da onu duyar.
- **Konuşma tespiti:** `webrtcvad` sessizlik sınırlarında kesip Whisper'a yalnızca konuşma gönderir; boş VRAM döngüsü ve halüsinasyon azalır.
- **ASR:** `faster-whisper` (CTranslate2) üzerinde Whisper large-v3-turbo. 6 GB VRAM'de ~2 GB kaplar, gerçek zamanlının çok altında gecikmeyle çalışır.
- **Özet:** Ollama üzerinden Gemma 4. Her seferinde tüm transkript baştan özetlenir; önceki özete ekleme yapılmadığı için hatalar birikmez.
- **Arayüz:** NiceGUI ile tarayıcıda açılan tek sayfa. Aynı ağdaki telefondan da izlenebilir.

## Gereksinimler

| | Minimum | Test edilen |
|---|---|---|
| İşletim sistemi | Windows 10 / 11 (64-bit) | Windows 11 |
| GPU | NVIDIA, 4 GB VRAM, CUDA 12 destekli sürücü (≥ 527) | 6 GB, hibrit AMD + NVIDIA laptop |
| RAM | 12 GB | 16 GB |
| Python | 3.10 | 3.11 |
| Disk | ~12 GB (CUDA 3 GB + Whisper 1.6 GB + Gemma ~5 GB) | |

> GPU'suz makinede de çalışır (`WHISPER_DEVICE = "cpu"`), ancak large-v3-turbo CPU'da gerçek zamanlının gerisinde kalır; o durumda `small` veya `medium` modeline inmen gerekir.

## Kurulum

Kurulum dört katmandan oluşur ve **sırası önemlidir**: sürücü → CUDA/cuDNN → Ollama → Python.

### 1. NVIDIA sürücüsü

[nvidia.com/drivers](https://www.nvidia.com/drivers) üzerinden kartın için güncel sürücüyü indir. Kurulumda **Custom (Advanced) → Perform a clean installation** seç, sonra yeniden başlat.

PowerShell'de doğrula:

```powershell
nvidia-smi
```

Sağ üstte **CUDA Version: 12.8** veya üstünü görmelisin. (13.x görmen de sorun değil; sürücü geriye uyumludur.)

### 2. CUDA Toolkit 12.8 + cuDNN 9

Vesper, CUDA kütüphanelerini **pip ile değil, resmi NVIDIA kurulumuyla** bekler. pip'ten gelen `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` paketleri hibrit laptoplarda sürücüyle çakışıp sessiz donmalara yol açtığı için desteklenmez (bkz. [Sorun Giderme](#sorun-giderme)).

**CUDA Toolkit 12.8** — [CUDA Toolkit Archive](https://developer.nvidia.com/cuda-toolkit-archive) → 12.8.x → Windows → x86_64 → `exe (local)`.

- Kurulumda **Custom (Advanced)** seç.
- **Driver components** bölümünün tamamının işaretini kaldır — aksi halde toolkit içindeki eski sürücü, az önce kurduğunun üstüne yazılır.
- GeForce Experience, PhysX, Nsight, Visual Studio Integration gerekmez.
- **CUDA → Runtime → Libraries** ve **Development** işaretli kalsın.
- 13.x sürümünü **kurma**; CTranslate2 şu an CUDA 12 ile çalışır.

**cuDNN 9 (CUDA 12 için)** — [cuDNN Downloads](https://developer.nvidia.com/cudnn-downloads) → Windows → x86_64 → **Tarball (zip)** → **CUDA 12**.

Dosya adında `_cuda12` geçtiğinden emin ol; indirme sayfası varsayılan olarak CUDA 13 seçili gelebiliyor. Zip'i aç ve içeriğini CUDA klasörüne kopyala:

```
cudnn\bin\*.dll        →  C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8\bin
cudnn\include\*        →  C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8\include
cudnn\lib\x64\*.lib    →  C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8\lib\x64
```

**Yeni** bir PowerShell aç ve doğrula:

```powershell
echo $env:CUDA_PATH                 # ...\CUDA\v12.8
where.exe cublas64_12.dll           # tek satır, CUDA\v12.8\bin
where.exe cudnn64_9.dll             # tek satır, CUDA\v12.8\bin
```

`where.exe` iki komut için de **tam olarak bir satır** döndürmeli. Birden fazla kopya görüyorsan (örneğin Ollama veya başka bir uygulamanın klasörü) o klasörü PATH'ten çıkar ya da CUDA `bin`'i listenin en üstüne taşı. Bu, projede karşılaşılan tek gerçek "donma" sebebiydi.

### 3. Ollama + Gemma

[ollama.com](https://ollama.com) üzerinden Windows kurulumunu yap, sonra:

```powershell
ollama pull gemma4:e2b
```

Vesper, Gemma'yı **bilinçli olarak CPU'da** çalıştırır (nedeni için [Tasarım kararları](#tasarım-kararları)). Bunu kalıcı yapmak için Windows Ortam Değişkenleri'ne ekle:

```
OLLAMA_LLM_LIBRARY = cpu
```

Ekledikten sonra görev çubuğundan Ollama'yı kapatıp yeniden aç.

### 4. Vesper

```powershell
git clone https://github.com/<kullanıcı-adın>/vesper.git
cd vesper
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

> Anaconda kullanıyorsan `base` yerine yeni bir ortam aç (`conda create -n vesper python=3.11`); Anaconda'nın MKL'li numpy'ı pip paketleriyle karışınca DLL hataları çıkarabiliyor.

## İlk çalıştırmadan önce: iki test

**Ses yakalanıyor mu?** Bir video/müzik açıkken:

```powershell
python test_audio.py
```

Ekranda `#` çubukları akıyorsa loopback cihazı doğru bulunmuştur.

**GPU zinciri sağlam mı?** CUDA → cuDNN → CTranslate2 → küçük model → hedef model, her adım ayrı basılır:

```powershell
python gpu_check.py
```

İlk çalıştırma 1–3 dakika sürebilir: cuDNN'in ~1 GB'lık motor DLL'leri ilk kez yüklenir ve Windows Defender tarar. İkinci çalıştırma saniyeler sürer. Yedi adımın hepsi `OK` döndüyse hazırsın.

## Çalıştırma

```powershell
python ui.py
```

Terminalde `NiceGUI ready to go` görünce tarayıcıda **http://localhost:8080** aç. Aynı ağdaki telefondan bilgisayarın yerel IP'siyle de açabilirsin (`http://192.168.1.23:8080` gibi).

1. **Başlat**'a bas, webinarı oynat.
2. Sağ panelde transkript akar; sol panel 4 dakikada bir güncellenen canlı özeti gösterir. Üstteki rozet Whisper'ın hangi cihazda çalıştığını söyler (`cuda:0 / int8_float16`).
3. **Durdur ve Final Özet Çıkar**'a bas. `outputs/webinar_YYYYMMDD_HHMMSS.md` dosyasına final özet + tam transkript yazılır.

Arayüzsüz konsol modu: `python main.py` (Ctrl+C ile durdurunca final özeti üretir).

### Çıktı formatı

```markdown
# Webinar Özeti — 20260903_140512

## Genel Özet
...

## Ana Noktalar
- ...

## Aksiyon Maddeleri / Kararlar
- ...

---

## Tam Transkript
...
```

## Ayarlar

Tüm ayarlar açıklamalarıyla birlikte `config.py` içinde. En sık dokunulanlar:

| Ayar | Varsayılan | Ne zaman değiştirilir |
|---|---|---|
| `WHISPER_LANGUAGE` | `"tr"` | Karışık TR/EN içerik için `None` (otomatik algılama, biraz daha yavaş) |
| `WHISPER_COMPUTE_TYPE` | `"int8_float16"` | RTX 30/40 serisinde `"float16"` genelde daha hızlı; VRAM darsa `"int8"` |
| `WHISPER_ALLOW_CPU_FALLBACK` | `True` | GPU'nun kesin çalıştığını test ederken `False` yap, sorun varsa açık hata alırsın |
| `OLLAMA_MODEL` | `"gemma4:e2b"` | Daha kaliteli özet için `"gemma4:e4b"` (CPU'da belirgin yavaşlar) |
| `OLLAMA_NUM_CTX` | `32768` | ~2 saatten uzun oturumlar için artır; RAM kullanımı da artar |
| `LIVE_SUMMARY_INTERVAL_SEC` | `240` | Canlı özet sıklığı |
| `SILENCE_TIMEOUT_MS` | `700` | Konuşmacı çok duraksıyorsa artır, cümleler bölünmesin |
| `WHISPER_EXTRA_DLL_DIRS` | `[]` | cuDNN'i zip yerine exe ile kurduysan onun `bin\12.x` klasörü |

## Tasarım kararları

**Neden Whisper GPU'da, Gemma CPU'da?**
6 GB VRAM'e ikisi birlikte sığmıyor: large-v3-turbo ~2 GB, `gemma4:e2b` ise en sıkı kuantizasyonda bile 4–6 GB istiyor. Gerçek zamanlı olması gereken parça Whisper; Gemma ise 4 dakikada bir 30–60 saniye çalışıyor. GPU'nun tamamını Whisper'a vermek, iki modelin VRAM için yarışmasından doğacak takılmaları baştan ortadan kaldırıyor. Ayrıca Ollama'nın CUDA runner'ı hibrit AMD+NVIDIA laptoplarda `0xc0000409` ile çökebiliyor; Ollama'nın kendi dokümanı bu durumda CPU'ya zorlamayı öneriyor.

**Neden pip'ten CUDA DLL'i değil, resmi Toolkit?**
`nvidia-cublas-cu12` + `nvidia-cudnn-cu12` kombinasyonu sürüm sabitlenmediğinde birbirleriyle ve sürücüyle test edilmemiş bir set oluşturuyor; PATH'te başka bir cuBLAS kopyası varsa (Ollama kendi klasöründe taşır) CUDA hata vermek yerine **sessizce kilitleniyor**. Tek ve resmi bir set + PATH'te tek kopya, bu sınıf sorunu tamamen kapatıyor.

**Neden her seferinde baştan özetleme?**
Artımlı özetleme (önceki özet + yeni kısım) küçük hataları katlayarak taşır. Gemma 4'ün geniş bağlam penceresi 1,5–2 saatlik transkripti tek seferde alıyor; her özet temiz bir sayfadan başlıyor.

**Neden `condition_on_previous_text=False`?**
Whisper'da bir parçadaki hata, sonraki parçaya "bağlam" olarak sızıp tekrar döngülerine yol açabiliyor. Her parça bağımsız işlenince bir kötü segment tüm oturumu bozamıyor.

## Sorun Giderme

| Belirti | Sebep | Çözüm |
|---|---|---|
| Whisper GPU'da başlatılırken **hata vermeden donuyor**, arayüz açılmıyor | PATH'te birden fazla `cublas64_12.dll` / `cudnn64_9.dll`; pip `nvidia-*` kalıntısı; sürücü–kütüphane uyumsuzluğu | `pip uninstall -y nvidia-cublas-cu12 nvidia-cudnn-cu12` → `where.exe cublas64_12.dll` tek satır olana kadar PATH'i temizle → `python gpu_check.py`. Hâlâ takılıyorsa temiz sürücü kurulumu (DDU) |
| Log: `GPU başlatma DONDU` sonra `CPU'ya düşülüyor` | Aynı sebepler; Vesper'ın bekçisi devreye girmiş | Uygulama çalışır ama yavaştır. Yukarıdaki adımları uygula; `WHISPER_ALLOW_CPU_FALLBACK = False` ile gerçek hatayı gör |
| `Library cublas64_12.dll is not found` | CUDA Toolkit kurulmamış veya `CUDA_PATH` tanımsız | Kurulum §2. `echo $env:CUDA_PATH` boşsa yeniden başlat / toolkit'i onar |
| `cudnn64_9.dll not found` | cuDNN kopyalanmamış ya da CUDA 13 varyantı indirilmiş | Dosya adında `_cuda12` olan zip'i indir, `bin` içeriğini CUDA `bin`'e kopyala |
| İlk açılış 2–3 dakika sürüyor | cuDNN motor DLL'leri ilk yükleme + antivirüs taraması | Normal, bir kez olur. Bu sırada `nvidia-smi`'de `python.exe` VRAM'i artıyorsa çalışıyordur |
| `CUDA out of memory` | VRAM'de başka bir uygulama (tarayıcı GPU hızlandırma, oyun) | `WHISPER_COMPUTE_TYPE = "int8"`; tarayıcıda donanım hızlandırmayı kapat |
| Ollama: `shared object initialization failed (0xc0000409)` | Ollama CUDA runner'ının sürücüyle uyumsuzluğu | `OLLAMA_LLM_LIBRARY = cpu` ortam değişkeni (Kurulum §3) |
| `Ollama'ya bağlanılamadı` | Ollama çalışmıyor | Görev çubuğunda ikonunu kontrol et; `ollama list` ile modeli doğrula |
| `Loopback cihazı bulunamadı` | Varsayılan çıkış cihazı WASAPI loopback sunmuyor | Windows Ses Ayarları'nda çıkış cihazını değiştir; olmazsa VB-CABLE kur |
| `webrtcvad` kurulumu: `Microsoft Visual C++ 14.0 or greater is required` | Python sürümün için hazır wheel yok | [C++ Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/) → "Desktop development with C++" → `pip install` tekrar |
| Anaconda'da `_multiarray_umath` / `mkl-service` hatası | MKL'li numpy ile pip paketleri karışmış | Ayrı ortam: `conda create -n vesper python=3.11` |
| Özet transkriptin sonunu içermiyor | Bağlam penceresi dolmuş | `OLLAMA_NUM_CTX` artır |
| Transkriptte tekrar eden cümleler / uydurma metin | Çok kısa veya sessiz parçalar | `VAD_AGGRESSIVENESS = 3`; `WHISPER_MIN_CHUNK_SEC` artır |

Tabloda olmayan bir şeyle karşılaşırsan `python gpu_check.py` çıktısını ve terminal logunu bir issue'ya yapıştır; log her adımı zaman damgasıyla basar, nerede durduğu görülür.

## Proje yapısı

```
vesper/
├── ui.py               # NiceGUI arayüzü (giriş noktası)
├── main.py             # Yakalama → transkript → özet akışını yöneten WebinarAssistant
├── audio_capture.py    # WASAPI loopback + webrtcvad ile konuşma parçalama
├── transcriber.py      # faster-whisper; GPU başlatma bekçisi, ısınma, CPU fallback
├── summarizer.py       # Ollama/Gemma istemcisi, canlı ve final prompt'lar
├── config.py           # Tüm ayarlar
├── gpu_check.py        # CUDA zincirini adım adım doğrulayan tanı aracı
├── test_audio.py       # Loopback ses testi
├── requirements.txt
├── assets/
└── outputs/            # Üretilen tutanaklar (git'e girmez)
```

## Durum ve yol haritası

Vesper, tek bir Windows 11 makinede (hibrit AMD + NVIDIA 6 GB, 16 GB RAM) Türkçe webinarlarla test edildi. Başka donanım/sürücü kombinasyonlarından gelen geri bildirimler ve issue'lar memnuniyetle karşılanır.

Planlananlar:

- [ ] Konuşmacı ayrımı (diarization)
- [ ] Oturum sırasında transkriptin diske akıtılması (çökme durumunda kayıp olmaması)
- [ ] Ollama'yı otomatik başlatma / model yoksa indirme
- [ ] Linux desteği (PulseAudio/PipeWire monitor kaynağı)
- [ ] Tek dosya `.exe` paketleme

## Teşekkür

[faster-whisper](https://github.com/SYSTRAN/faster-whisper) · [CTranslate2](https://github.com/OpenNMT/CTranslate2) · [Whisper large-v3-turbo CT2 dönüşümü](https://huggingface.co/deepdml/faster-whisper-large-v3-turbo-ct2) · [Ollama](https://ollama.com) · [Gemma](https://ai.google.dev/gemma) · [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch) · [NiceGUI](https://nicegui.io)

## Lisans

MIT — bkz. [LICENSE](LICENSE).
