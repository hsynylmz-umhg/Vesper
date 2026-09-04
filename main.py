"""
Ses yakalama -> transkript -> özetleme akışını yöneten ana sınıf.
Üç arka plan thread'i çalıştırır: yakalama, transkripsiyon, periyodik özetleme.
"""

import os
import queue
import threading
import time
from datetime import datetime

import config
from audio_capture import AudioCapture
from transcriber import Transcriber
from summarizer import Summarizer


class WebinarAssistant:
    def __init__(self):
        self.transcript_lock = threading.Lock()
        self.full_transcript = []  # metin parçalarının listesi
        self.live_summary = ""
        self.status = "Beklemede"

        self._audio_queue: "queue.Queue" = queue.Queue()
        self._running = False
        self._threads = []

        self.capture = AudioCapture(self._audio_queue)
        self.transcriber = Transcriber()
        self.summarizer = Summarizer()

    # ------------------------------------------------------------------ #
    def start(self):
        if self._running:
            return
        self.full_transcript = []
        self.live_summary = ""
        self._running = True
        self.status = "Dinleniyor"

        self.capture.start()
        self._threads = [
            threading.Thread(target=self._transcribe_loop, daemon=True),
            threading.Thread(target=self._summarize_loop, daemon=True),
        ]
        for t in self._threads:
            t.start()

    def stop(self) -> str:
        """Dinlemeyi durdurur, final özeti üretir, dosyaya kaydeder ve final özeti döner."""
        if not self._running:
            return self.live_summary

        self._running = False
        self.capture.stop()
        self.status = "Final özet hazırlanıyor..."

        full_text = self.get_full_transcript()
        final_summary = self.summarizer.summarize(full_text, is_final=True)
        self._save_output(full_text, final_summary)

        self.status = "Durduruldu"
        return final_summary

    def get_full_transcript(self) -> str:
        with self.transcript_lock:
            return " ".join(self.full_transcript)

    # ------------------------------------------------------------------ #
    def _transcribe_loop(self):
        while self._running:
            try:
                audio_chunk = self._audio_queue.get(timeout=1)
            except queue.Empty:
                continue

            text = self.transcriber.transcribe(audio_chunk)
            if text:
                with self.transcript_lock:
                    self.full_transcript.append(text)
                print(f"[Transkript] {text}")

    def _summarize_loop(self):
        while self._running:
            for _ in range(config.LIVE_SUMMARY_INTERVAL_SEC):
                if not self._running:
                    return
                time.sleep(1)

            full_text = self.get_full_transcript()
            if full_text.strip():
                self.status = "Canlı özet güncelleniyor..."
                self.live_summary = self.summarizer.summarize(full_text, is_final=False)
                self.status = "Dinleniyor"
                print(f"\n[Canlı Özet]\n{self.live_summary}\n")

    def _save_output(self, transcript: str, summary: str):
        os.makedirs(config.OUTPUT_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(config.OUTPUT_DIR, f"webinar_{ts}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# Webinar Özeti — {ts}\n\n")
            f.write(summary)
            f.write("\n\n---\n\n## Tam Transkript\n\n")
            f.write(transcript)
        print(f"Kaydedildi: {path}")


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
