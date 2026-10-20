# Vesper — Proje Yapısı (Güncel Durum)

Bu dosya fiziksel yapı ve uygulanmış davranışlar içindir. Kalıcı kurallar
CLAUDE.md'de, sürüm planı ve onay durumu ROADMAP.md'dedir.

## Durum ve kanıt sınırı

- Son yayınlanan sürüm: **v0.4.0 — Vesper speaks**.
- Aktif çalışma: **v0.5.0**, `v0.5-understands`; A/B/C uygulanmış, sürüm onayı
  **HOLD**. v0.5 tamamlandı/yayınlandı iddiası yoktur.
- **Üretim göçü HOLD**. Geliştirme ve geçici veritabanı testleri üretim izni değildir.
- Kesin referans: Windows 11 x64, Python **3.13.14**, SQLite **3.53.4**,
  NiceGUI 3.16.0, sistem CUDA 12.8 + cuDNN 9. Whisper GPU, Ollama CPU politikası.
  Dış ortam 49 Conda + 45 pip paketi içerir; paket sahipliği ve bütünlük
  kurulumda doğrulanmıştır. `release-tooling.json` kanıt dosyalarını hash ile tanımlar.
- Önceki 157 test (v0.4), seçilmiş 429 ve onarım sonrası 190 ayrı test sonucu
  tarihsel kanıttır; örtüşen sayılar toplanmaz. Güncel seçilmiş modül sonucu
  aşağıdaki test bölümünde tutulur.
- Gerçek Windows kabulünde GPU transkripsiyon, WASAPI, Qwen final özet,
  not/link kalıcılığı, Geçmiş ve otomatik Markdown gözlendi. İkinci deneyin
  eski kapanışında hem completed.json hem failure.json oluştu; bu kayıtlar
  değiştirilmedi. Yeni terminal raporlama gerçek uygulamada henüz yeniden denenmedi.
- Ertelenen manuel kabul: A/B oturum yalıtımı; materyal silme onayı/iptali;
  yeniden başlatma kalıcılığı; 240 saniyelik canlı özet; doğrudan Ollama CPU kanıtı.

## Çalışan dosyalar

```text
Vesper/
├── main.py                  # İnce konsol girişi; varsayılan DB ile kabul testi yapılmaz.
├── ui.py                    # NiceGUI Oturum/Geçmiş ve Meeting Materials.
├── gpu_check.py             # Bağımsız donanım tanısı; güvenli test listesine dahil değil.
├── test_audio.py            # Bağımsız WASAPI tanısı; otomatik keşfe dahil edilmez.
├── requirements.txt         # Genel bağımlılık alt sınırları; kesin ortam kilidi değil.
├── release-tooling.json     # Sürümlü dış başlatıcı/ortam konumu, SHA-256 ve kurulum kanıtı.
├── CLAUDE.md
├── PROJECT_STRUCTURE.md
├── ROADMAP.md
├── README.md
├── vesper/
│   ├── __init__.py          # Kasıtlı boş; yerel donanımı dolaylı yüklemez.
│   ├── config.py            # Tek merkezi ayar; varsayılan DB üretim için ayrılmıştır.
│   ├── session.py           # Session/Segment/Summary/Artifact ve açık tür sabitleri.
│   ├── storage.py           # SCHEMA_VERSION=2; thread-local SQLite/WAL, göç, yedek, parmak izi.
│   ├── repository.py        # SQL CRUD; artifact sahipliği ve Markdown üretimi.
│   ├── engine.py            # Yakalama/transkripsiyon/özet; aktif artifact oturum sınırı.
│   ├── language.py          # Türkçe/English/Otomatik kaynak dili politikası.
│   ├── audio_capture.py     # WASAPI + WebRTC VAD; korunan donanım davranışı.
│   ├── transcriber.py       # Whisper/CTranslate2; GPU watchdog ve sistem DLL politikası.
│   └── summarizer.py        # Yerel Ollama; hızlı/kaliteli profil ve CPU politikası.
└── tests/                   # Açık modül listesiyle, sahte donanım ve geçici verilerle çalışır.
```

Kökte kalmış `audio_capture.py`, `config.py`, `repository.py`, `session.py`,
`storage.py` eski ölü koddur; geliştirme yalnızca `vesper/` paketindedir.
Sotto ve Sloane yalnızca geleceğe ayrılmış adlardır; otomatik platform entegrasyonu yoktur.

## A — Kalıcılık ve şema güvenliği

- Şema v2: sessions, segments, summaries, schema_metadata ve artifacts.
  Artifact alanları: id, session_id, kind, content, capture_method,
  source_segment_id (NULL olabilir), created_at. Bileşik segment/oturum FK korunur.
- `Database(db_path, allow_migration=False)`; izinsiz v1, yeni veya tutarsız
  şema yazılabilir bağlantı sunulmadan reddedilir. Yetkili göç ayrı açık adımdır.
- `BEGIN IMMEDIATE`, kilit altında yeniden sınıflandırma, tarihsel veri varsa
  ayrı kaynak bağlantısından çevrimiçi doğrulanmış yedek, DDL/sürüm/doğrulama,
  ardından COMMIT. Hata sonucu rolled_back/unknown/committed olarak ayrılır.
- Yedek özel deneme dizininde özel dosyaya yazılır; içerik/bütünlük ve fsync
  sonrası Windows'ta üzerine yazmasız sonlandırılır. Son yedekler korunur;
  çökmeden kalan dizinler otomatik temizlenmez. Yedek süre sınırı en iyi çabadır.
- PRAGMA table_xinfo/table_list, dizin/FK/tetikleyici denetimleri ve dar
  CREATE TABLE okuyucusu birlikte kullanılır. CHECK, ertelenen FK, BINARY dışı
  kolasyon, ABORT dışı çatışma politikası ve yanlış AUTOINCREMENT reddedilir.
  SQLite sözcüksel boşlukları ASCII SPACE/TAB/LF/CR/FF ile sınırlıdır.
  Bu yaklaşım genel SQL çözümleyicisi veya tam semantik eşdeğerlik kanıtı değildir.
- Sürümlü şemalar ilgisiz ek nesnelere izin verir; sürümsüz eski v1 katı nesne
  kümesi ister. Salt-okunur bağlantı WAL yan dosyalarında sıfır etki vaat etmez.

## B/C — Elle materyaller ve yaşam döngüsü

- Yalnızca `link`/`note`, yalnızca `manual`. Link mutlak geçerli HTTP/HTTPS
  olmalıdır; not biçimi değiştirilmez. Boş içerik, geçersiz tür/yöntem,
  başka oturumun segmenti ve bayat etkin oturum isteği reddedilir.
- `add_artifact`, `list_artifacts`, `delete_artifact`; parametreli SQL,
  zaman/id sırası, oturum sahipliği ve oturum silme cascade'i korunur.
- Canlı sayfa etkin oturuma ekler; Geçmiş yalnızca seçili oturumu gösterir/siler,
  yeni materyal oluşturmaz. Silme açık onay ister; kullanıcı metni düz etikettir.
- Stop otomatik olarak SQLite'tan Markdown üretir: özet, transkript ve zamanlı
  materyaller. Notlar kapanamayacak uzunlukta kod çitinde düz metin kalır.
  Dosya bir anlık görüntüdür; sonraki silme/yeniden adlandırma eski dışa aktarımı güncellemez.
- Süreçte tek assistant tutulur. Stop Future'ı raporlamadan önce tamamlanır;
  kapanış yinelenmez, bayat/iptal edilmiş sayfaya güncelleme yapılmaz.
  Uzun finalizasyonun zorunlu sunucu kapanış süresine sığacağı varsayılmaz.

## Dış kabul araçları ve yeniden üretim

Başlatıcı repository dışında `C:/Users/-HSYN-/vesper-runtime-plan/acceptance/acceptance_launcher.py`
konumundadır. Konum/hash, referans interpreter ve donmuş 94-artifact paketinin
kanıtları `release-tooling.json` içindedir. Checkout tek başına bu ortamı sağlamaz.
Yeni makine kurulumu, yollar ve artifact tedariki ayrı onay gerektirir; mevcut
ortam taşınmaz, yeniden kurulmaz. Anaconda base ve sistem CUDA değişmez.

Başlatıcı testleri manifestten aracı çözer; `VESPER_ACCEPTANCE_LAUNCHER` yalnızca
aynı hash'e sahip başka bir kopyayı test etmek içindir. CLI'nin onaylı konum
koruması değişmez. Eksik veya değişmiş araç testleri sessizce atlatmaz.
Yeni deneylerde `shutdown-snapshot.json` ara kanıt, `terminal.json` tek yetkili
sonuçtur. PASS için finalizasyon/işçiler/temizlik/ASGI/server kapanışı doğrulanır;
tek Ctrl+C otomatik başarı değildir. FAIL/UNKNOWN deneyleri korunur.

## Testler

Seçilen güvenli modüller: test_schema_migration, test_persistence,
test_artifacts, test_artifact_lifecycle, test_artifact_export, test_artifact_ui,
test_ui_startup, test_acceptance_launcher. 20 Ekim 2026 son toplu sonuç: **430 PASS, 0 FAIL, 0 ERROR, 0 SKIP**,
70.344 saniye; Python 3.13.14 / SQLite 3.53.4. Modül sayıları sırasıyla
224 + 26 + 40 + 24 + 13 + 18 + 27 + 58; yalnızca bu seçilmiş listeye aittir. Testler üretim DB veya gerçek
GPU/WASAPI/Ollama kullanmaz; sınırsız keşif yapılmaz.

Diğer mevcut sahte donanım testleri: test_lifecycle_integration,
test_audio_capture_device_selection, test_audio_capture_runtime_failure,
test_summarizer_profiles, test_language_selection, test_transcriber_language,
test_transcriber_preflight. Bu batch'in toplu komutuna dahil değillerdir.
