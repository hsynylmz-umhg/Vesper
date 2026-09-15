import time
from vesper.engine import WebinarAssistant

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
