# Vesper — Yol Haritası

Vesper, sürüm sürüm genişleyen, tamamen yerel bir "meeting intelligence" sistemi olmayı hedefliyor. Sürüm numaraları henüz `0.x` aralığında; kararlı bir genel API taahhüdü yoktur. Her sürüm bağımsız ve kullanılabilir bir aşamadır.

Bu dosya, hangi sürümün aktif geliştirmede olduğu ve hangi özelliklerin hangi
sürüme ait olduğu sorusunun **tek yetkili kaynağıdır** (bkz. `CLAUDE.md` §2,
§7). Fiziksel kod/dosya durumu için `PROJECT_STRUCTURE.md`'ye, değişmeyen
mimari kurallar için `CLAUDE.md`'ye bakılmalıdır.

**Durum sütunu değerleri:**
- **Yayında** — `main`'e merge edildi, tamamlandı.
- **Üzerinde çalışılıyor** — aktif geliştirme branch'inde, henüz `main`'e
  merge edilmedi. Aynı anda en fazla bir sürüm bu durumda olabilir.
- **Planlandı** — henüz başlanmadı.

## Sürümler

| Sürüm | Başlık | Durum |
|---|---|---|
| v0.1.0 | Vesper enters the room | Yayında |
| v0.1.1 | Interface refresh | Yayında |
| v0.2.0 | Vesper remembers | Yayında |
| v0.3.0 | Vesper listens | Yayında |
| v0.4.0 | Vesper speaks | Yayında |
| v0.5.0 | Vesper understands | Planlandı |
| v0.6.0 | Vesper attends | Planlandı |
| v0.7.0 | Vesper sees | Planlandı |
| v0.8.0 | Vesper connects | Planlandı |
| v0.9.0 | Vesper matures | Planlandı |
| v1.0.0 | Vesper | Planlandı |

## v0.2.0 — Vesper remembers

- [x] SQLite kalıcı depolama (`data/vesper.db`)
- [x] Her transkript parçasının üretildiği anda diske yazılması (crash recovery)
- [x] Geçmiş oturum listesi + görüntüleyici (özet + tam transkript)
- [x] Oturum yeniden adlandırma / silme
- [x] Güvenli kapanış: bekleyen ses kuyruğunun tamamen işlenmesini garanti etme
- [x] Motor/UI ayrımı (`vesper/` paket yapısına geçiş)

## v0.3.0 — Vesper listens

- [x] Ses kaynağı / cihaz seçimi (basit cihaz seçici; uygulama bazlı loopback ayrı ve daha zor bir alt görevdir)
- [x] Birden fazla LLM modeli / hız-kalite profili

## v0.4.0 — Vesper speaks

- [x] Dil seçici arayüzü
- [ ] *(gelecekteki alt sistem adı: "Sotto" — dil/çeviri katmanı)*

## v0.5.0 — Vesper understands

- [ ] Chat / link yakalama
- [ ] `artifacts` tablosu (transkript dışı yakalanan veri: link, not, dosya) — v0.8'in provenance/export özellikleri bu şemaya dayanacak

## v0.6.0 — Vesper attends

- [ ] Zamanlayıcı, otomatik toplantı katılımı — yalnızca resmî URL şeması/SDK'sı olan platformlar (Zoom, Teams). Google Meet ve Discord için otomatik katılım planlanmıyor.
- [ ] Kapanış tespiti (ses + zaman sinyaline dayalı; görsel sinyal v0.7'den önce yok)

## v0.7.0 — Vesper sees

- [ ] Görsel / OCR analizi
- [ ] *(gelecekteki alt sistem adı: "Sloane" — görsel/OCR katmanı)*

## v0.8.0 — Vesper connects

- [ ] Provenance / export özellikleri (v0.5'teki `artifacts` şemasına dayanır; v0.5 şeması v0.8'den önce bozulmamalı)

## v0.9.0 — Vesper matures

*(henüz ayrıntılandırılmadı)*

## v1.0.0 — Vesper

*(henüz ayrıntılandırılmadı)*

## v1.0.0 sonrası

- [ ] Linux PulseAudio / PipeWire desteği
- [ ] macOS desteği

## Sürüm numarası henüz atanmamış fikirler

Gerçek fikirler, ama hangi sürüme ait oldukları henüz netleşmedi:

- [ ] Zaman damgalı transkript
- [ ] Speaker diarization / konuşmacı ayrımı
- [ ] Daha iyi Türkçe / İngilizce dil geçişi
- [ ] Aranabilir eski oturumlar
- [ ] Markdown / JSON / SRT export
- [ ] GPU / CPU telemetry paneli
- [ ] Ollama model kontrolü ve yardımcı kurulum akışı
- [ ] Windows portable / `.exe` build
- [ ] English README