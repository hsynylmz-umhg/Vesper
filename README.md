<p align="center">
  <img src="assets/Vesper.jpg" width="260" alt="Vesper">
</p>

<h1 align="center">Vesper</h1>

<p align="center">
  <i>She was in the room. She took notes.</i>
</p>

<p align="center">
  <b>Listens to your webinars, hands you the minutes.</b><br>
  Local Whisper + Gemma. No cloud APIs.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/platform-Windows%2010%2F11-0078D6?logo=windows&logoColor=white" alt="Windows">
  <img src="https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-3776AB?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/GPU-NVIDIA%20CUDA%2012-76B900?logo=nvidia&logoColor=white" alt="CUDA 12">
  <img src="https://img.shields.io/badge/local--first-no%20cloud%20API-181717" alt="Local first">
  <img src="https://img.shields.io/badge/license-MIT-lightgrey" alt="MIT">
</p>

---

Vesper, bilgisayarında oynatılan webinarı, canlı dersi veya toplantıyı doğrudan **hoparlör çıkışından** dinler; konuşmayı Türkçe metne dönüştürür, oturum sürerken düzenli olarak güncellenen bir **canlı özet** tutar ve oturum bittiğinde **tutanak formatında final özet + tam transkript** bırakır.

Transkripsiyon ve özetleme kendi makinen üzerinde gerçekleşir. Ses bir bulut AI servisine gönderilmez ve API anahtarı gerekmez. Modeller ilk kullanımda indirildikten sonra temel inference akışı yerel olarak çalışabilir.

<p align="center">
  <img src="assets/demo.jpg" width="900" alt="Vesper live transcription and summary interface">
</p>

<p align="center">
  <sub>Live transcription and AI-generated summaries running locally on Windows.</sub>
</p>

## Nasıl çalışır?

```text
┌──────────────────────┐
│ Windows System Audio │
│   WASAPI Loopback    │
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│      WebRTC VAD      │
│ konuşma / sessizlik  │
└──────────┬───────────┘
           │ 16 kHz mono
           ▼
┌──────────────────────┐
│    faster-whisper    │
│     CTranslate2      │
│     NVIDIA GPU       │
└──────────┬───────────┘
           │
           ▼
       Transcript
           │
           ▼
┌──────────────────────┐
│    Ollama + Gemma    │
│         CPU          │
└──────────┬───────────┘
           │
           ▼
 Live Summary + Final Minutes
```

- **Ses yakalama:** Windows'un WASAPI loopback özelliğini kullanır. Mikrofonu dinlemek yerine varsayılan çıkış cihazından oynatılan sesi yakalar.
- **Konuşma tespiti:** `webrtcvad`, sesi konuşma ve sessizlik sınırlarına göre parçalar; gereksiz sessiz bölümlerin Whisper'a gönderilmesini azaltır.
- **ASR:** `faster-whisper` + CTranslate2 üzerinde Whisper large-v3-turbo, NVIDIA GPU ile çalışır.
- **Özetleme:** Ollama üzerinden yerel Gemma modeli kullanılır. Varsayılan yapılandırmada Gemma CPU'da tutularak GPU belleği Whisper'a bırakılır.
- **Canlı özet:** Belirlenen aralıklarla o ana kadarki transkript yeniden özetlenir.
- **Arayüz:** NiceGUI tabanlı yerel web arayüzü tarayıcıdan veya aynı yerel ağdaki başka bir cihazdan görüntülenebilir.

## Gereksinimler

| Bileşen | Minimum / Beklenen | Test edilen ortam |
|---|---|---|
| İşletim sistemi | Windows 10 / 11 64-bit | Windows 11 |
| GPU | CUDA destekli NVIDIA GPU | RTX 4050 Laptop GPU, 6 GB VRAM |
| Hibrit grafik | Desteklenebilir | AMD iGPU + NVIDIA |
| RAM | 12 GB+ | 16 GB |
| Python | 3.10 – 3.12 | Python 3.11 |
| CUDA | CUDA 12 ailesi | CUDA Toolkit 12.8 |
| cuDNN | cuDNN 9 / CUDA 12 | cuDNN 9 |
| Disk | Modeller + CUDA için birkaç GB boş alan | — |

> Vesper'ın mevcut/test edilen GPU bağımlılık seti **CUDA 12 + cuDNN 9** kullanır. CTranslate2/faster-whisper'ın gelecekteki sürümlerinde desteklenen CUDA kombinasyonları değişebileceğinden bağımlılık güncellerken upstream dokümantasyonunu da kontrol et.

GPU'suz kullanım mümkündür:

```python
WHISPER_DEVICE = "cpu"
```

Ancak large-v3-turbo CPU üzerinde gerçek zamanlı kullanım için ağır olabilir. CPU-only sistemlerde daha küçük bir Whisper modeli tercih etmek daha uygundur.

---

## Kurulum

Kurulumu şu sırayla yapmak önerilir:

```text
NVIDIA Driver
     ↓
CUDA Toolkit + cuDNN
     ↓
Ollama + Gemma
     ↓
Python environment
     ↓
Vesper
```

### 1. NVIDIA sürücüsü

[NVIDIA Drivers](https://www.nvidia.com/drivers) üzerinden GPU'n için güncel Windows sürücüsünü indir.

Mevcut CUDA kurulumunda sorun yaşadıysan kurulum sırasında:

```text
Custom (Advanced)
→ Perform a clean installation
```

seçeneği yararlı olabilir.

Kurulumdan sonra yeniden başlat ve PowerShell'de:

```powershell
nvidia-smi
```

çalıştır.

GPU'nun listelendiğini ve sürücünün doğru yüklendiğini doğrula.

> `nvidia-smi` içindeki **CUDA Version**, bilgisayarda kurulu CUDA Toolkit sürümünü göstermez. NVIDIA sürücüsünün desteklediği CUDA seviyesini ifade eder.

### 2. CUDA Toolkit 12.8

Vesper için referans/test edilen kurulum **CUDA Toolkit 12.8 + cuDNN 9 (CUDA 12)** kombinasyonudur.

CUDA Toolkit:

https://developer.nvidia.com/cuda-toolkit-archive

Şunları seç:

```text
CUDA Toolkit 12.8.x
Windows
x86_64
exe (local)
```

Kurulumda:

```text
Custom (Advanced)
```

seçeneğini kullan.

Eğer güncel NVIDIA sürücüsünü zaten ayrı olarak kurduysan Toolkit paketindeki display-driver bileşenini yeniden kurman gerekmez.

CUDA tarafında temel olarak Runtime/Libraries ve Development bileşenleri yeterlidir. Nsight ve Visual Studio entegrasyonları Vesper'ın çalışması için gerekli değildir.

Kurulumdan sonra **yeni bir PowerShell** aç:

```powershell
echo $env:CUDA_PATH
nvcc --version
```

Örneğin:

```text
C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8
```

görmelisin.

### 3. cuDNN 9 — CUDA 12

NVIDIA'nın resmi indirme sayfasından cuDNN 9'un **CUDA 12** varyantını indir:

https://developer.nvidia.com/cudnn-downloads

Windows ZIP/Tarball paketini kullanıyorsan dosya adının CUDA 12 paketini gösterdiğinden emin ol.

Arşivdeki dosyaları CUDA Toolkit dizinine yerleştir:

```text
cuDNN bin\*.dll
    ↓
C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8\bin

cuDNN include\*
    ↓
C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8\include

cuDNN lib\x64\*.lib
    ↓
C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8\lib\x64
```

Ardından yeni bir PowerShell aç:

```powershell
where.exe cublas64_12.dll
where.exe cudnn64_9.dll
```

Beklenen kaynak CUDA Toolkit'in `bin` dizinidir:

```text
...\CUDA\v12.8\bin\cublas64_12.dll
...\CUDA\v12.8\bin\cudnn64_9.dll
```

Birden fazla sonuç görmek tek başına bir hata değildir. Ancak farklı uygulamalardan gelen birden fazla CUDA/cuDNN sürümünün PATH üzerinde bulunması, Windows DLL yükleme sırasını belirsizleştirerek teşhisi zorlaştırabilir.

Bu nedenle Vesper için CUDA DLL'lerinin tercihen **tek ve tutarlı bir NVIDIA CUDA kurulumundan** gelmesi önerilir.

> Vesper'ın referans kurulumu CUDA/cuDNN çalışma zamanı için pip'teki `nvidia-cublas-cu12` ve `nvidia-cudnn-cu12` paketlerini kullanmaz. Amaç olası DLL sürüm ve arama yolu çakışmalarını azaltmaktır.

### 4. Ollama + Gemma

[Ollama](https://ollama.com/) Windows sürümünü kur.

Ardından:

```powershell
ollama pull gemma4:e2b
```

Modeli doğrula:

```powershell
ollama list
```

API'nin çalıştığını kontrol etmek istersen:

```powershell
curl http://localhost:11434/api/tags
```

Varsayılan Vesper yapılandırması özetleme modelini CPU'da çalıştırır:

```python
OLLAMA_FORCE_CPU = True
```

Ollama CUDA backend'inde sistemine özgü bir sorun yaşıyorsan ayrıca kullanıcı ortam değişkeni olarak:

```text
OLLAMA_LLM_LIBRARY=cpu
```

tanımlayabilirsin.

PowerShell ile:

```powershell
[Environment]::SetEnvironmentVariable("OLLAMA_LLM_LIBRARY", "cpu", "User")
```

Sonrasında Ollama'yı tamamen kapatıp yeniden başlat.

### 5. Vesper'ı kur

Repoyu klonla:

```powershell
git clone https://github.com/hsynylmz-umhg/Vesper.git
cd Vesper
```

Ayrı bir Python ortamı oluşturmak önerilir:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

PowerShell sanal ortam script'ini engelliyorsa:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Ardından:

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

> Anaconda/Miniconda kullanıyorsan `base` ortamına kurmak yerine ayrı bir environment oluşturmak DLL/NumPy çakışmalarını önlemeye yardımcı olabilir.

Örneğin:

```powershell
conda create -n vesper python=3.11
conda activate vesper
pip install -r requirements.txt
```

---

## İlk çalıştırmadan önce

Vesper iki bağımsız tanı aracı içerir. Tam uygulamadan önce ikisini çalıştırmak sorunları çok daha kolay ayırır.

### 1. Sistem sesini test et

Bilgisayarda bir video veya müzik oynatırken:

```powershell
python test_audio.py
```

Şuna benzer hareketli çubuklar görüyorsan:

```text
0.0s  #######
0.5s  ##############
1.0s  ####################
```

WASAPI loopback ses yakalama katmanı çalışıyor demektir.

### 2. GPU zincirini test et

```powershell
python gpu_check.py
```

Tanı aracı sırasıyla:

```text
CUDA DLLs
   ↓
CTranslate2
   ↓
CUDA device detection
   ↓
supported compute types
   ↓
small Whisper inference
   ↓
Vesper target model
```

katmanlarını kontrol eder.

İlk CUDA/cuDNN yüklemesi sonraki çalıştırmalardan daha uzun sürebilir. Disk, antivirüs, shader/kernel cache ve sürücü durumuna bağlı olarak süre sistemden sisteme değişebilir.

Başka bir terminalde GPU kullanımını izlemek için:

```powershell
nvidia-smi -l 1
```

kullanabilirsin.

`gpu_check.py` başarıyla tamamlanıyorsa tam uygulamaya geç.

---

## Çalıştırma

Web arayüzünü başlat:

```powershell
python ui.py
```

NiceGUI hazır olduğunda:

http://localhost:8080

adresini aç.

Aynı yerel ağdaki başka bir cihazdan da bilgisayarının LAN IP adresini kullanabilirsin:

```text
http://192.168.1.23:8080
```

### Kullanım

1. **Başlat** düğmesine bas.
2. Webinarı, canlı dersi veya toplantıyı oynat.
3. Vesper konuşma parçalarını algılayıp Whisper'a gönderir.
4. Transkript arayüzde görünür.
5. Canlı özet belirlenen aralıklarla güncellenir.
6. Oturum sonunda **Durdur ve Final Özet Çıkar** düğmesine bas.

Final çıktı:

```text
outputs/webinar_YYYYMMDD_HHMMSS.md
```

altına kaydedilir.

Arayüzsüz kullanım için:

```powershell
python main.py
```

Ctrl+C ile durdurulduğunda final özet oluşturulur.

### Örnek çıktı

```markdown
# Webinar Özeti — 20260903_140512

## Genel Özet

...

## Ana Noktalar

- ...
- ...

## Aksiyon Maddeleri / Kararlar

- ...

---

## Tam Transkript

...
```

---

## Yapılandırma

Vesper'ın çalışma ayarları `config.py` içerisindedir.

| Ayar | Varsayılan | Açıklama |
|---|---|---|
| `WHISPER_LANGUAGE` | `"tr"` | Türkçe için dili sabitler. Karışık TR/EN içerikte `None` kullanılabilir |
| `WHISPER_COMPUTE_TYPE` | `"int8_float16"` | GPU inference hassasiyeti / bellek dengesi |
| `WHISPER_ALLOW_CPU_FALLBACK` | `True` | GPU başlatılamazsa CPU'ya geçilmesine izin verir |
| `WHISPER_GPU_INDEX` | `0` | Kullanılacak CUDA cihazı |
| `WHISPER_MIN_CHUNK_SEC` | `0.3` | Çok kısa ses parçalarını filtreler |
| `OLLAMA_MODEL` | `"gemma4:e2b"` | Özetlemede kullanılan yerel model |
| `OLLAMA_NUM_CTX` | `32768` | Ollama bağlam penceresi |
| `OLLAMA_FORCE_CPU` | `True` | Gemma'yı CPU üzerinde tutar |
| `LIVE_SUMMARY_INTERVAL_SEC` | `240` | Canlı özet yenileme aralığı |
| `SILENCE_TIMEOUT_MS` | `700` | Konuşma parçasını kapatmadan önce beklenecek sessizlik |
| `MAX_CHUNK_SECONDS` | `25` | Tek Whisper parçasının maksimum uzunluğu |

### Dil algılama

Ağırlıklı Türkçe içerik:

```python
WHISPER_LANGUAGE = "tr"
```

Türkçe ve İngilizcenin karıştığı oturumlar:

```python
WHISPER_LANGUAGE = None
```

İkinci seçenek otomatik dil algılama yaptığı için bir miktar ek işlem gerektirebilir.

### GPU compute type

Varsayılan:

```python
WHISPER_COMPUTE_TYPE = "int8_float16"
```

6 GB sınıfı GPU'lar için iyi bir başlangıç noktasıdır.

GPU mimarisine ve VRAM'e bağlı olarak:

```python
WHISPER_COMPUTE_TYPE = "float16"
```

da denenebilir.

En iyi seçim GPU modeline göre benchmark edilmelidir.

---

## Gizlilik

Vesper'ın temel tasarım hedeflerinden biri transkripsiyon ve özetleme verisini yerel makinede tutmaktır.

```text
System Audio
     ↓
Local Whisper
     ↓
Local Transcript
     ↓
Local Ollama / Gemma
     ↓
Local Markdown Output
```

Vesper'ın normal inference akışı sesi veya transkripti OpenAI, Google, Anthropic gibi harici bir AI API'sine göndermez.

İlk kurulum sırasında:

- Python bağımlılıklarının,
- Whisper modelinin,
- Ollama'nın,
- Gemma modelinin

indirilmesi için internet bağlantısı gerekir.

Modeller ve bağımlılıklar hazır olduğunda temel transkripsiyon ve özetleme akışı yerel olarak çalışabilir.

> **Kayıt ve gizlilik uyarısı:** Toplantıların, derslerin veya görüşmelerin kaydedilmesi/transkribe edilmesi bulunduğun ülkeye, organizasyona ve görüşmenin niteliğine göre izin veya bilgilendirme gerektirebilir. Vesper'ı yalnızca kaydetmeye veya transkribe etmeye yetkili olduğun içeriklerde kullan.

---

## Tasarım kararları

### Neden Whisper GPU'da, Gemma CPU'da?

Transkripsiyon gecikmeye duyarlıdır. Özetleme ise yalnızca belirli aralıklarla gerçekleştirilir.

Bu nedenle varsayılan mimari:

```text
NVIDIA GPU → Whisper
CPU        → Gemma
```

şeklindedir.

Test sisteminde large-v3-turbo çalışma koşullarına göre yaklaşık 2–3 GB VRAM kullanır. Gemma'yı aynı sınırlı VRAM alanına eklemek iki iş yükünün GPU belleği için yarışmasına neden olabilir.

Gemma'yı CPU'da tutmak GPU belleğini ASR'ye ayırır ve davranışı daha öngörülebilir hale getirir.

Daha yüksek VRAM'e sahip sistemlerde farklı dağılımlar denenebilir.

### Neden pip CUDA DLL'leri yerine resmi NVIDIA kurulumu?

Python üzerinden dağıtılan NVIDIA CUDA paketleri geçerli bir dağıtım yöntemidir; Vesper ise Windows referans kurulumunda farklı kaynaklardan gelen CUDA/cuDNN DLL'lerinin aynı süreçte karışması ihtimalini azaltmak için resmi NVIDIA Toolkit + cuDNN kurulumunu tercih eder.

Amaç:

```text
tek sürücü
   +
tek CUDA Toolkit
   +
uyumlu cuDNN
   +
öngörülebilir DLL arama yolu
```

oluşturmaktır.

Bu tercih, pip tabanlı CUDA dağıtımlarının genel olarak hatalı olduğu anlamına gelmez.

### Neden her canlı özette tüm transkript yeniden işleniyor?

Artımlı özetleme:

```text
eski özet + yeni bölüm → yeni özet
```

şeklinde yapıldığında önceki özette oluşan bir hata sonraki özetlere taşınabilir.

Vesper bunun yerine:

```text
o ana kadarki tam transkript → yeni özet
```

yaklaşımını kullanır.

Böylece her özet önceki AI çıktısı yerine kaynak transkripte dayanır.

`OLLAMA_NUM_CTX = 32768` birçok kullanım senaryosu için geniş bir bağlam sağlar; ancak bunun kaç dakikalık konuşmaya karşılık geldiği konuşma hızı, dil, tokenizasyon ve prompt boyutuna bağlıdır.

Çok uzun oturumlarda bağlam kullanımı ayrıca izlenmelidir.

### Neden `condition_on_previous_text=False`?

Whisper'ın önceki segment metnini sonraki parçaya bağlam olarak taşıması bazı seslerde tekrarların veya hataların segmentler arasında yayılmasına neden olabilir.

Vesper'da ses zaten VAD tarafından bağımsız parçalara ayrıldığı için:

```python
condition_on_previous_text=False
```

kullanılır.

Bu, parçalar arasındaki hata yayılımını azaltmayı amaçlar.

---

## Sorun giderme

### Whisper GPU başlatılırken takılıyor

Önce bağımsız testi çalıştır:

```powershell
python gpu_check.py
```

Ardından:

```powershell
where.exe cublas64_12.dll
where.exe cudnn64_9.dll
pip list | findstr /i nvidia
```

çıktılarını kontrol et.

Birden fazla CUDA/cuDNN dağıtımının PATH üzerinde bulunması tek başına hatayı kanıtlamaz; ancak farklı sürümlerin yüklenmesi olası DLL çakışmalarının teşhisini zorlaştırır.

Mümkün olduğunca tek ve tutarlı CUDA kaynağı kullan.

GPU'nun sürücü tarafından görüldüğünü doğrula:

```powershell
nvidia-smi
```

### `Library cublas64_12.dll is not found`

```powershell
where.exe cublas64_12.dll
echo $env:CUDA_PATH
```

çalıştır.

Beklenen konum:

```text
...\NVIDIA GPU Computing Toolkit\CUDA\v12.x\bin
```

CUDA Toolkit kurulumunu ve PATH'i kontrol et.

### `cudnn64_9.dll is not found`

```powershell
where.exe cudnn64_9.dll
```

ile kontrol et.

cuDNN'in CUDA 12 varyantını kurduğundan ve DLL'lerin doğru `bin` dizininde olduğundan emin ol.

### Vesper CPU fallback'e geçiyor

Logda:

```text
GPU başlatma DONDU
```

ve ardından CPU fallback görüyorsan `gpu_check.py` ile GPU zincirini izole test et.

GPU sorunlarını özellikle teşhis ederken:

```python
WHISPER_ALLOW_CPU_FALLBACK = False
```

yaparak başarısızlığın CPU fallback tarafından gizlenmesini önleyebilirsin.

### CUDA out of memory

Öncelikle GPU belleğini kontrol et:

```powershell
nvidia-smi
```

Diğer GPU uygulamalarını kapat ve:

```python
WHISPER_COMPUTE_TYPE = "int8_float16"
```

veya kartın desteklediği daha düşük bellekli compute type'ları dene.

Ollama'nın da GPU kullanmadığından emin olmak için:

```python
OLLAMA_FORCE_CPU = True
```

kullan.

### İlk GPU başlangıcı uzun sürüyor

İlk model/CUDA başlangıcı sonraki çalıştırmalardan daha uzun sürebilir.

Başka bir terminalde:

```powershell
nvidia-smi -l 1
```

ile `python.exe` GPU kullanımını gözlemleyebilirsin.

Başlangıç sürekli aynı noktada kalıyorsa `gpu_check.py` çıktısı hangi katmanın sorunlu olduğunu belirlemek için daha faydalıdır.

### Ollama `shared object initialization failed (0xc0000409)`

Bu hata sisteminde Ollama'nın GPU backend'i kullanılırken ortaya çıkıyorsa Ollama'yı CPU modunda çalıştırmayı deneyebilirsin:

```text
OLLAMA_LLM_LIBRARY=cpu
```

Vesper'ın kendisi için:

```python
OLLAMA_FORCE_CPU = True
```

kullanılır.

### Ollama'ya bağlanılamıyor

```powershell
ollama list
curl http://localhost:11434/api/tags
```

ile Ollama'nın çalıştığını ve modelin mevcut olduğunu doğrula.

### WASAPI loopback cihazı bulunamıyor

Önce:

```powershell
python test_audio.py
```

çalıştır.

Windows'ta:

```text
Ayarlar
→ Sistem
→ Ses
→ Çıkış
```

altındaki varsayılan cihazı kontrol et.

### `webrtcvad`: Microsoft Visual C++ 14.0 or greater is required

[Microsoft C++ Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/) yükle ve:

```text
Desktop development with C++
```

iş yükünü seç.

Ardından:

```powershell
pip install -r requirements.txt
```

komutunu yeniden çalıştır.

### Anaconda / NumPy DLL hatası

`_multiarray_umath`, MKL veya benzer DLL sorunlarında temiz bir ortam oluştur:

```powershell
conda create -n vesper python=3.11
conda activate vesper
pip install -r requirements.txt
```

### Çok uzun transkriptin sonu özete girmiyor

`OLLAMA_NUM_CTX` sabit bir süreyi değil token kapasitesini ifade eder.

Çok uzun oturumlarda:

```python
OLLAMA_NUM_CTX = 65536
```

gibi daha büyük bir değer denenebilir; fakat RAM kullanımı da artar.

Uzun vadede hiyerarşik/chunked özetleme daha ölçeklenebilir bir çözümdür.

---

## Proje yapısı

```text
Vesper/
├── ui.py               # NiceGUI web arayüzü
├── main.py             # Ana uygulama ve thread yönetimi
├── audio_capture.py    # WASAPI loopback + WebRTC VAD
├── transcriber.py      # faster-whisper / CUDA / fallback
├── summarizer.py       # Ollama + Gemma
├── config.py           # Merkezi yapılandırma
│
├── gpu_check.py        # CUDA / CTranslate2 tanı aracı
├── test_audio.py       # WASAPI ses testi
├── requirements.txt
├── LICENSE
│
├── assets/
│   └── Vesper.jpg
│
└── outputs/            # Üretilen tutanaklar; git'e dahil edilmez
```

---

## Durum

Vesper şu anda erken aşamada bir açık kaynak projesidir.

Mevcut sürüm gerçek bir Windows 11 makinede:

```text
Windows 11
AMD + NVIDIA hibrit grafik
NVIDIA RTX 4050 Laptop GPU / 6 GB VRAM
16 GB RAM
Python 3.11
CUDA Toolkit 12.8
cuDNN 9
```

ile Türkçe içerikler üzerinde test edilmiştir.

Bu, diğer donanım ve sürücü kombinasyonlarının doğrulandığı anlamına gelmez. Farklı NVIDIA GPU'lar, Windows sürümleri ve ses cihazlarından gelen hata raporları özellikle değerlidir.

---

## Yol haritası

- [ ] Arayüz yenilemesi + ekran görüntüleri (v2)
- [ ] Speaker diarization / konuşmacı ayrımı
- [ ] Zaman damgalı transkript
- [ ] Oturum sırasında transkripti diske sürekli yazma
- [ ] Çökme sonrası oturum kurtarma
- [ ] Daha iyi Türkçe / İngilizce dil geçişi
- [ ] Uzun oturumlar için hiyerarşik özetleme
- [ ] Aranabilir eski oturumlar
- [ ] Markdown / JSON / SRT export
- [ ] GPU / CPU telemetry paneli
- [ ] Ollama model kontrolü ve yardımcı kurulum akışı
- [ ] Windows portable / `.exe` build
- [ ] Linux PulseAudio / PipeWire desteği
- [ ] English README

---

## Katkıda bulunma

Issue ve pull request'ler açıktır.

Bir hata bildirirken mümkünse aşağıdaki bilgileri ekle:

```text
Windows version:
GPU:
NVIDIA driver:
CUDA Toolkit:
cuDNN:
Python:
faster-whisper:
CTranslate2:
Vesper commit/version:
```

CUDA/GPU sorunlarında ayrıca:

```powershell
nvidia-smi
where.exe cublas64_12.dll
where.exe cudnn64_9.dll
python gpu_check.py
```

çıktılarını eklemek teşhisi önemli ölçüde kolaylaştırır.

Lütfen log paylaşmadan önce kullanıcı adları, dosya yolları veya başka kişisel bilgiler içerip içermediğini kontrol et.

---

## Teşekkür

Vesper aşağıdaki açık kaynak projeleri ve araçları kullanır:

- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
- [CTranslate2](https://github.com/OpenNMT/CTranslate2)
- [Whisper large-v3-turbo CT2](https://huggingface.co/deepdml/faster-whisper-large-v3-turbo-ct2)
- [Ollama](https://ollama.com)
- [Gemma](https://ai.google.dev/gemma)
-