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
_default_database = Database(config.DB_PATH)


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
        self.full_transcript = []
        self.live_summary = ""
        self._running = True
        self.status = "Dinleniyor"
        self._segment_seq = 0

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

        self.capture.start()
        self._transcribe_thread = threading.Thread(target=self._transcribe_loop, daemon=True)
        self._summarize_thread = threading.Thread(target=self._summarize_loop, daemon=True)
        self._threads = [self._transcribe_thread, self._summarize_thread]
        for t in self._threads:
            t.start()

    def stop(self) -> str:
        """Dinlemeyi durdurur, kuyruktaki sesi TAMAMEN boşaltır, final özeti
        üretir, kalıcı depoya yazar, dosyaya aktarır ve final özeti döner."""
        if not self._running:
            return self.live_summary

        self._running = False
        self.capture.stop()  # WASAPI akışını kapatır; bundan sonra kuyruğa yeni ses gelmez

        # v0.2.0 düzeltmesi: _running False olur olmaz _transcribe_loop'un
        # while kontrolü hemen çıkabiliyordu — kuyrukta birikmiş birden fazla
        # parça varsa sadece o an bloke olduğu TEK parça işlenip geri kalanı
        # sessizce kayboluyordu. Döngü artık kuyruk tamamen boşalana kadar
        # sürüyor (bkz. _transcribe_loop); burada bitmesini bekliyoruz.
        if self._transcribe_thread:
            self._transcribe_thread.join(timeout=30)
            if self._transcribe_thread.is_alive():
                log.warning("Transkript kuyruğu 30 saniyede boşalmadı, final özet eldeki veriyle üretilecek")

        self.status = "Final özet hazırlanıyor..."
        full_text = self.get_full_transcript()
        final_summary = self.summarizer.summarize(full_text, is_final=True)

        if self.session_id:
            try:
                self.repository.save_summary(
                    self.session_id, kind=SUMMARY_FINAL,
                    content=final_summary, model=config.OLLAMA_MODEL,
                )
                self.repository.finish_session(self.session_id, status=STATUS_COMPLETED)
            except Exception:
                log.exception("Final özet/oturum durumu SQLite'a yazılamadı")

        self._save_output(full_text, final_summary)

        self.status = "Durduruldu"
        return final_summary

    def get_full_transcript(self) -> str:
        with self.transcript_lock:
            return " ".join(self.full_transcript)

    # ------------------------------------------------------------------ #
    def _transcribe_loop(self):
        try:
            # `or not self._audio_queue.empty()`: _running False olsa bile kuyrukta
            # kalan parçaları işlemeye devam et (bkz. stop() içindeki not — bu,
            # durdurma anında ses kaybını önleyen asıl düzeltme).
            while self._running or not self._audio_queue.empty():
                try:
                    audio_chunk = self._audio_queue.get(timeout=1)
                except queue.Empty:
                    continue

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
                            log.exception("Segment #%d SQLite'a yazılamadı (bellekte hâlâ mevcut)", seq)

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
