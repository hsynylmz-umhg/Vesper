"""
Ses yakalama -> transkript -> özetleme akışını yöneten ana sınıf.
Üç arka plan thread'i çalıştırır: yakalama, transkripsiyon, periyodik özetleme.

v0.2.0: Oturumlar artık kalıcıdır (bkz. storage.py / repository.py / session.py).
Her transkript parçası ve her özet, üretildiği anda SQLite'a yazılır — sadece
Durdur'a basıldığında değil. outputs/*.md dosyası hâlâ üretilir ama artık
KAYNAK değil, kalıcı veriden türetilen bir DIŞA AKTARIMDIR.
"""

import logging
import os
import queue
import threading
import time
from datetime import datetime, timezone

import config
from audio_capture import AudioCapture
from transcriber import Transcriber
from summarizer import Summarizer
from repository import SessionRepository
from session import STATUS_COMPLETED, SUMMARY_FINAL, SUMMARY_LIVE
from storage import Database

log = logging.getLogger("main")
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

# Tüm uygulama tek bir veritabanı dosyasını paylaşır (her thread kendi
# bağlantısını açar, bkz. storage.py). Testler kendi Database(geçici_yol)
# örneğini kurup SessionRepository'yi WebinarAssistant'a enjekte eder.
_default_database = Database(getattr(config, "DB_PATH", "data/vesper.db"))

# Phase A (güvenli kapanış): transkripsiyon işçisine "artık yeni ses gelmeyecek"
# demenin tek güvenli yolu bu sentinel'i FIFO kuyruğa koymak — ama SADECE ses
# üreticisinin (AudioCapture) KESİN olarak öldüğü kanıtlandıktan sonra.
_TRANSCRIBE_SENTINEL = object()
TRANSCRIBE_JOIN_TIMEOUT_SEC = 30
CAPTURE_EXTRA_WAIT_SEC = 3


class WebinarAssistant:
    def __init__(self, repository: SessionRepository = None):
        self.transcript_lock = threading.Lock()
        self.full_transcript = []  # metin parçalarının listesi (hızlı UI erişimi için hâlâ bellekte)
        self.live_summary = ""
        self.status = "Beklemede"

        self._audio_queue: "queue.Queue" = queue.Queue()
        self._running = False
        self._threads = []
        self._transcribe_thread = None
        self._summarize_thread = None
        self._segment_seq = 0
        self.session_id = None

        # Phase A (güvenli kapanış):
        #   last_stop_drained   -> tüm kabul edilmiş ses (kuyrukta + işlenmekte
        #                          olan) transkripsiyonu kanıtlanmış şekilde bitti mi?
        #   last_stop_finalized -> final özet üretildi VE SQLite'a yazıldı VE
        #                          Markdown dışa aktarımı başarılı oldu VE oturum
        #                          gerçekten TAMAMLANDI olarak işaretlendi mi?
        # None = henüz bir stop() denenmedi (yeni oturum/hiç durdurulmamış).
        self.last_stop_drained = None
        self.last_stop_finalized = None
        self._had_persistence_gap = False

        self.capture = AudioCapture(self._audio_queue)
        self.transcriber = Transcriber()
        self.summarizer = Summarizer()

        self.repository = repository or SessionRepository(_default_database)

        # Uygulama önceki çalıştırmada temiz kapanmadıysa (çökme, kill, güç
        # kesintisi vb.), o oturumları "interrupted" işaretle. VERİYİ SİLMEZ —
        # History'de erişilebilir kalır.
        try:
            self.repository.recover_interrupted_sessions()
        except Exception:
            log.exception("Başlangıç kurtarma taraması başarısız oldu (devam ediliyor)")

    # ------------------------------------------------------------------ #
    def start(self):
        if self._running:
            return
        if self._transcribe_thread is not None and self._transcribe_thread.is_alive():
            # Önceki stop() bir zaman aşımına uğradı ve işçi hâlâ canlı.
            # Aynı queue/oturum alanlarını paylaşan yeni bir oturum başlatmak
            # eski işçinin yanlış oturuma segment yazmasına yol açabilir.
            raise RuntimeError(
                "Önceki oturumun transkripsiyon işçisi hâlâ çalışıyor "
                "(muhtemelen bir zaman aşımı sonrası). Yeni oturum başlatılamaz."
            )

        self.full_transcript = []
        self.live_summary = ""
        self._running = True
        self.status = "Dinleniyor"
        self._segment_seq = 0
        self.last_stop_drained = None
        self.last_stop_finalized = None
        self._had_persistence_gap = False

        default_title = f"Webinar — {datetime.now():%Y-%m-%d %H:%M}"
        try:
            self.session_id = self.repository.create_session(
                title=default_title,
                source_type="system_audio",
                source_language=config.WHISPER_LANGUAGE,
                summary_language="tr",
            )
        except Exception:
            # SQLite'a yazılamasa bile oturum bellekte devam edebilmeli;
            # ama bunu sessizce yutma, yüksek sesle logla.
            self.session_id = None
            log.exception("Yeni oturum SQLite'a kaydedilemedi — bu oturum kalıcı OLMAYACAK")

        try:
            self.capture.start()
        except Exception:
            # WASAPI cihaz başlatma hatası vb. — _running'i geri almazsak bu
            # instance BİR DAHA HİÇ start() edilemez (üstteki "if self._running:
            # return" sonsuza kadar sessizce hiçbir şey yapmaz).
            log.exception("Ses yakalama başlatılamadı")
            self._running = False
            raise

        self._transcribe_thread = threading.Thread(target=self._transcribe_loop, daemon=True)
        self._summarize_thread = threading.Thread(target=self._summarize_loop, daemon=True)
        self._threads = [self._transcribe_thread, self._summarize_thread]
        for t in self._threads:
            t.start()

    def stop(self) -> str:
        """Dinlemeyi durdurur. Yalnızca ses üreticisinin KESİN olarak öldüğü
        kanıtlanırsa transkripsiyon sentinel'ini kuyruğa koyar, yalnızca tüm
        kabul edilmiş ses kanıtlanmış şekilde işlendiyse özet üretir, ve
        yalnızca özet+dışa aktarım tamamen başarılı olursa oturumu TAMAMLANDI
        işaretler. Her başarısızlık noktasında oturum 'active' kalır ve bir
        sonraki başlangıçta normal kurtarma akışıyla 'interrupted' olur."""
        if not self._running:
            return self.live_summary
        self._running = False

        # 1) Ses üreticisinin KESİN olarak öldüğünü kanıtla. Bu kanıtlanmadan
        # sentinel'i kuyruğa KOYMA — aksi halde AudioCapture'ın finally
        # bloğundaki son konuşma parçası sentinel'den SONRA kuyruğa girebilir
        # ve transkripsiyon işçisi onu hiç görmeden çıkabilir.
        try:
            producer_dead = self.capture.stop()
        except Exception:
            log.exception("Ses yakalama durdurulurken hata oluştu")
            producer_dead = False

        if not producer_dead:
            try:
                producer_dead = self.capture.wait_until_stopped(timeout=CAPTURE_EXTRA_WAIT_SEC)
            except Exception:
                log.exception("Ses yakalama durumu doğrulanamadı")
                producer_dead = False

        if not producer_dead:
            log.error(
                "Ses yakalama üreticisinin kesin olarak durduğu doğrulanamadı; "
                "sentinel KONULMADI, transkripsiyon işçisi hâlâ bekliyor olabilir."
            )
            self.last_stop_drained = False
            self.status = "Ses yakalama zamanında durmadı"
            return self.live_summary

        # 2) Üretici kesin olarak öldü — artık kuyruğa yeni ses girmez.
        # Sentinel'i koymak güvenli.
        self._audio_queue.put(_TRANSCRIBE_SENTINEL)

        # 3) Kuyrukta kalan + işlenmekte olan HER ŞEYİN bitmesini bekle.
        if self._transcribe_thread:
            self._transcribe_thread.join(timeout=TRANSCRIBE_JOIN_TIMEOUT_SEC)
            if self._transcribe_thread.is_alive():
                log.error(
                    "Transkripsiyon %ds içinde tamamlanamadı (GPU/Whisper çağrısı "
                    "takılmış olabilir). Oturum TAMAMLANDI işaretlenmeyecek.",
                    TRANSCRIBE_JOIN_TIMEOUT_SEC,
                )
                self.last_stop_drained = False
                self.status = "Transkripsiyon tamamlanamadı"
                return self.live_summary

        self.last_stop_drained = True

        # 4) Drain kanıtlandı ama bir segment SQLite'a hiç yazılamadıysa,
        # kalıcı kayıt (ve ondan türeyen Markdown) eksik demektir — "Vesper
        # remembers" vaadini bozar. Bunu TAMAMLANDI olarak işaretleme.
        if self._had_persistence_gap:
            log.error(
                "Oturum sırasında en az bir segment SQLite'a yazılamadı; "
                "kayıt eksik olabileceğinden TAMAMLANDI işaretlenmeyecek."
            )
            self.last_stop_finalized = False
            self.status = "Bazı transkript parçaları kaydedilemedi"
            return self.live_summary

        # 5) Final özet + kalıcı yazım + dışa aktarım — bu sırayla, ve
        # STATUS_COMPLETED en SONDA: dışa aktarım başarısız olursa oturum
        # zaten TAMAMLANDI işaretlenmiş olmamalı.
        self.status = "Final özet hazırlanıyor..."
        full_text = self.get_full_transcript()
        try:
            final_summary = self.summarizer.summarize(full_text, is_final=True)

            if self.session_id:
                self.repository.save_summary(
                    self.session_id, kind=SUMMARY_FINAL,
                    content=final_summary, model=config.OLLAMA_MODEL,
                )

            self._save_output(full_text, final_summary)

            if self.session_id:
                self.repository.finish_session(self.session_id, status=STATUS_COMPLETED)
                self.last_stop_finalized = True
                self.status = "Durduruldu"
            else:
                # SQLite'a oturum hiç kaydedilememişti (start()'taki not).
                # Bellekten Markdown dışa aktarımı yine de üretildi — ama bu
                # v0.2 anlamında KALICI bir oturum tamamlanması DEĞİL; UI'a
                # sanki History'e kaydedilmiş gibi göstermeyelim.
                self.last_stop_finalized = False
                self.status = "Durduruldu (kalıcı oturum kaydı yok)"

            return final_summary

        except Exception:
            log.exception("Final özet üretilemedi, kaydedilemedi veya dışa aktarılamadı")
            self.last_stop_finalized = False
            self.status = "Final özet oluşturulamadı"
            return self.live_summary

    def get_full_transcript(self) -> str:
        with self.transcript_lock:
            return " ".join(self.full_transcript)

    # ------------------------------------------------------------------ #
    def _transcribe_loop(self):
        try:
            # Artık `_running`/`empty()` yoklamıyoruz: stop() üreticinin kesin
            # öldüğünü kanıtladıktan SONRA kuyruğa _TRANSCRIBE_SENTINEL koyar.
            # FIFO sıra sayesinde sentinel'e ulaşmak, ondan önce kabul edilmiş
            # her şeyin işlendiğinin kanıtıdır.
            while True:
                item = self._audio_queue.get()
                if item is _TRANSCRIBE_SENTINEL:
                    break
                audio_chunk = item

                started_at = datetime.now(timezone.utc)
                text = self.transcriber.transcribe(audio_chunk)
                ended_at = datetime.now(timezone.utc)

                if text:
                    with self.transcript_lock:
                        self.full_transcript.append(text)
                        self._segment_seq += 1
                        seq = self._segment_seq

                    if self.session_id:
                        try:
                            self.repository.append_segment(
                                self.session_id, sequence=seq,
                                started_at=started_at, ended_at=ended_at,
                                text=text, language=config.WHISPER_LANGUAGE,
                            )
                        except Exception:
                            # Bellekteki transkript korunuyor (yukarıda eklendi);
                            # sadece kalıcı yazım başarısız oldu, oturumu ÇÖKERTME.
                            # Ama stop() bunu bilmeli — TAMAMLANDI işaretlenmesin.
                            log.exception("Segment #%d SQLite'a yazılamadı (bellekte hâlâ mevcut)", seq)
                            self._had_persistence_gap = True

                    print(f"[Transkript] {text}")
        finally:
            self.repository.close_thread_connection()

    def _summarize_loop(self):
        try:
            while self._running:
                for _ in range(config.LIVE_SUMMARY_INTERVAL_SEC):
                    if not self._running:
                        return
                    time.sleep(1)

                full_text = self.get_full_transcript()
                if full_text.strip():
                    self.status = "Canlı özet güncelleniyor..."
                    self.live_summary = self.summarizer.summarize(full_text, is_final=False)

                    if self.session_id:
                        try:
                            self.repository.save_summary(
                                self.session_id, kind=SUMMARY_LIVE,
                                content=self.live_summary, model=config.OLLAMA_MODEL,
                            )
                        except Exception:
                            log.exception("Canlı özet SQLite'a yazılamadı (arayüzde göstermeye devam ediliyor)")

                    self.status = "Dinleniyor"
                    print(f"\n[Canlı Özet]\n{self.live_summary}\n")
        finally:
            self.repository.close_thread_connection()

    def _save_output(self, transcript: str, summary: str):
        """outputs/*.md hâlâ üretilir (geriye dönük uyumluluk) ama artık
        kalıcı depodan (SQLite) türetilir. transcript/summary parametreleri
        sadece oturum hiç SQLite'a yazılamadıysa (self.session_id None)
        yedek olarak kullanılır."""
        os.makedirs(config.OUTPUT_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(config.OUTPUT_DIR, f"webinar_{ts}.md")

        if self.session_id:
            try:
                content = self.repository.export_markdown(self.session_id)
            except Exception:
                log.exception("Kalıcı veriden Markdown üretilemedi, bellekteki veriyle devam ediliyor")
                content = self._render_markdown_fallback(ts, transcript, summary)
        else:
            content = self._render_markdown_fallback(ts, transcript, summary)

        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"Kaydedildi: {path}")

    @staticmethod
    def _render_markdown_fallback(ts: str, transcript: str, summary: str) -> str:
        """SQLite'a hiç yazılamadığı nadir durumda kullanılır (v0.1.0'daki
        orijinal biçim) — kalıcı veri yoksa elimizdeki tek şey budur."""
        return (
            f"# Webinar Özeti — {ts}\n\n"
            f"{summary}\n\n---\n\n## Tam Transkript\n\n{transcript}"
        )


# Konsoldan doğrudan çalıştırmak için basit bir döngü (arayüzsüz kullanım / hızlı test)
if __name__ == "__main__":
    assistant = WebinarAssistant()
    print("Başlatılıyor... Durdurmak için Ctrl+C.")
    assistant.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nDurduruluyor, final özet hazırlanıyor...")
        summary = assistant.stop()
        print("\n=== FİNAL ÖZET ===\n")
        print(summary)
