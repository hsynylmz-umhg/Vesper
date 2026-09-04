"""
Basit, canlı güncellenen web arayüzü (NiceGUI). Tarayıcıda açılır,
aynı ağdaki telefondan da erişilebilir (bilgisayarın IP'si + :8080).
"""

from nicegui import ui

from main import WebinarAssistant

assistant = WebinarAssistant()

transcript_area = None
summary_area = None
status_badge = None
start_button = None
stop_button = None


def on_start():
    assistant.start()
    start_button.disable()
    stop_button.enable()


def on_stop():
    final_summary = assistant.stop()
    summary_area.set_content(final_summary)
    start_button.enable()
    stop_button.disable()
    ui.notify("Final özet hazır ve outputs/ klasörüne kaydedildi.", type="positive")


def refresh():
    status_badge.set_text(assistant.status)
    transcript_text = assistant.get_full_transcript()
    if transcript_text:
        # arayüzü yormamak için son ~4000 karakteri göster
        transcript_area.set_content(transcript_text[-4000:])
    if assistant.live_summary:
        summary_area.set_content(assistant.live_summary)


with ui.column().classes("w-full max-w-4xl mx-auto p-4 gap-4"):
    with ui.row().classes("items-center justify-between w-full"):
        ui.label("Webinar Dinleme Asistanı").classes("text-2xl font-semibold")
        status_badge = ui.badge("Beklemede").classes("text-sm")

    with ui.row().classes("gap-2"):
        start_button = ui.button("Başlat", on_click=on_start, color="primary")
        stop_button = ui.button("Durdur ve Final Özet Çıkar", on_click=on_stop, color="negative")
        stop_button.disable()

    with ui.row().classes("w-full gap-4"):
        with ui.card().classes("flex-1"):
            ui.label("Canlı Özet").classes("text-lg font-medium")
            summary_area = ui.markdown("Henüz özet yok, başlat'a bas.")

        with ui.card().classes("flex-1"):
            ui.label("Canlı Transkript (son kısım)").classes("text-lg font-medium")
            transcript_area = ui.markdown("")

ui.timer(5.0, refresh)

ui.run(title="Webinar Asistanı", port=8080, reload=False)
