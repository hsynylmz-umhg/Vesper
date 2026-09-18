"""
Vesper — Yerel Webinar Zekâsı
--------------------------------------------------------------------
NiceGUI tabanlı, koyu temalı, canlı güncellenen web arayüzü.
Tarayıcıda açılır, aynı ağdaki telefondan da erişilebilir
(bilgisayarın IP'si + :8080).

v0.2.0: "Oturum" (canlı görünüm, değişmedi) ve "Geçmiş" (kalıcı oturum
listesi + detay) olmak üzere iki sekme var. Geçmiş sekmesi sadece
assistant.repository üzerinden konuşur — burada hiç ham SQL yok.
"""

from datetime import datetime

from nicegui import ui, run

from vesper.engine import WebinarAssistant
from vesper.session import STATUS_COMPLETED, SUMMARY_FINAL, SUMMARY_LIVE

assistant = WebinarAssistant()

# NiceGUI eleman referansları — arka plan mantığı bunlarla konuşur
transcript_area = None
summary_area = None
status_badge = None
status_dot = None
waveform_icon = None
start_button = None
stop_button = None
device_select = None

# main.py'deki self.status metinlerinin TAM KARŞILIĞI (nokta rengi, canlı mı)
STATUS_STYLES = {
    "Beklemede": ("bg-slate-500", False),
    "Dinleniyor": ("bg-cyan-400", True),
    "Canlı özet güncelleniyor...": ("bg-amber-400", True),
    "Final özet hazırlanıyor...": ("bg-amber-400", True),
    "Durduruldu": ("bg-slate-500", False),
    # v0.3.0 — mevcut renk paletinden (amber) yeniden kullanılıyor, yeni bir
    # tasarım dili eklenmiyor. Aktif arıza pulse'lanır (kullanıcı henüz
    # Durdur'a basmadı); durduruldu-sonrası durum pulse'lanmaz (final durum).
    "Ses kaynağı beklenmedik şekilde koptu": ("bg-amber-400", True),
    "Durduruldu (ses kaynağı koptu)": ("bg-amber-400", False),
}


def _apply_status_visuals(status: str) -> None:
    """Durum noktasının rengini/pulse'unu ve eşitleyici ikonunu günceller.
    NOT: .classes(replace=...) TÜM sınıf listesini değiştirir, bu yüzden
    'flex-none' her çağrıda string'in içine yeniden dahil edilir."""
    color_class, is_live = STATUS_STYLES.get(status, ("bg-slate-500", False))
    dot_classes = "flex-none vesper-dot " + color_class
    if is_live:
        dot_classes += " vesper-dot-pulse"
    status_dot.classes(replace=dot_classes)

    if status == "Dinleniyor":
        waveform_icon.classes(add="vesper-eq-active")
    else:
        waveform_icon.classes(remove="vesper-eq-active")


# ---------------------------------------------------------------------------
# Arka plan mantığına bağlanan fonksiyonlar — ORİJİNAL DAVRANIŞ KORUNMUŞTUR
# ---------------------------------------------------------------------------

def _build_device_options() -> dict:
    """v0.3.0: ses kaynağı seçimi. UI, vesper.audio_capture'a DOĞRUDAN bağımlı
    DEĞİLDİR — sadece assistant.list_audio_devices() üzerinden konuşur
    (motorun tek uygulama-yüzeyi, bkz. CLAUDE.md §5.7)."""
    options = {None: "Sistem varsayılanı (otomatik)"}
    for device in assistant.list_audio_devices():
        options[device["index"]] = device["name"]
    return options


def on_device_change(e):
    try:
        assistant.set_audio_device(e.value)
    except Exception as exc:
        ui.notify(str(exc), type="negative")


def on_start():
    try:
        assistant.start()
    except Exception as e:
        ui.notify(str(e), type="negative")
        return
    start_button.disable()
    stop_button.enable()
    device_select.disable()

    # v0.3.0: bir önceki oturumdan panelde kalan içeriği/durumu temizle.
    # Bu SADECE start() başarılı olduktan SONRA yapılır — başarısız bir
    # start() önceki oturumun hâlâ geçerli olan görünümünü SİLMEMELİDİR.
    # (capture_failure_notified sıfırlaması da buraya taşındı — daha önce
    # ayrı bir satırdaydı, iki kez sıfırlanmasın diye tek yerde toplandı.)
    transcript_area.set_content("_Transkript burada görünecek..._")
    summary_area.set_content("Henüz özet yok, başlat'a bas.")
    _last["transcript"] = None
    _last["summary"] = None
    _last["capture_failure_notified"] = False

    # Rozet/nokta, bir sonraki 5 saniyelik ui.timer turunu beklemeden hemen
    # senkronize edilir — aksi halde kullanıcı kısaca bir önceki oturumun
    # durumunu görebilir.
    status_badge.set_text(assistant.status)
    _apply_status_visuals(assistant.status)


async def on_stop():
    stop_button.disable()
    ui.notify("Final özet hazırlanıyor, bu 1-2 dakika sürebilir...", type="info")
    final_summary = await run.io_bound(assistant.stop)
    if final_summary:
        summary_area.set_content(final_summary)
    start_button.enable()
    device_select.enable()
    if assistant.last_stop_drained and assistant.last_stop_finalized:
        ui.notify("Final özet hazır ve outputs/ klasörüne kaydedildi.", type="positive")
        # Az önce biten oturum Geçmiş'te hemen görünsün.
        refresh_history_list()
    else:
        ui.notify(f"Oturum tamamlanamadı: {assistant.status}", type="negative")


_last = {"transcript": None, "summary": None, "capture_failure_notified": False}

def refresh():
    status_badge.set_text(assistant.status)
    _apply_status_visuals(assistant.status)

    # v0.3.0: yakalama işçisi çöktüğünde BİR KEZ negatif bildirim göster.
    # _last["capture_failure_notified"] on_start()'ta sıfırlanır, böylece
    # bir SONRAKI oturum da kaynağını kaybederse BAĞIMSIZ olarak bildirilir.
    if assistant.capture_failed and not _last["capture_failure_notified"]:
        _last["capture_failure_notified"] = True
        ui.notify(assistant.status, type="negative")

    tail = assistant.get_full_transcript()[-4000:]
    if tail and tail != _last["transcript"]:
        transcript_area.set_content(tail)
        _last["transcript"] = tail

    if assistant.live_summary and assistant.live_summary != _last["summary"]:
        summary_area.set_content(assistant.live_summary)
        _last["summary"] = assistant.live_summary


# ---------------------------------------------------------------------------
# Geçmiş (History) — sadece assistant.repository üzerinden konuşur
# ---------------------------------------------------------------------------

_history_state = {"selected_id": None}

history_list_container = None
history_detail_empty = None
history_detail_content = None
history_detail_title_label = None
history_detail_summary_area = None
history_detail_transcript_area = None


def _format_local_datetime(iso_str: str) -> str:
    try:
        return datetime.fromisoformat(iso_str).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return iso_str or "—"


def _format_duration(started_iso: str, ended_iso) -> str:
    if not ended_iso:
        return "—"
    try:
        start = datetime.fromisoformat(started_iso)
        end = datetime.fromisoformat(ended_iso)
    except (TypeError, ValueError):
        return "—"
    total_min = max(0, int((end - start).total_seconds() // 60))
    hours, minutes = divmod(total_min, 60)
    return f"{hours}sa {minutes}dk" if hours else f"{minutes}dk"


def _status_label(status: str) -> str:
    return {
        "completed": "Tamamlandı",
        "interrupted": "Yarıda kesildi",
        "active": "Devam ediyor",
    }.get(status, status)


def _status_dot_class(status: str) -> str:
    return {
        "completed": "bg-slate-500",
        "interrupted": "bg-amber-400",
        "active": "bg-cyan-400",
    }.get(status, "bg-slate-500")


def open_session_detail(session_id: str):
    session = assistant.repository.get_session(session_id)
    if session is None:
        return
    _history_state["selected_id"] = session_id

    transcript = assistant.repository.get_full_transcript_text(session_id)
    final = assistant.repository.get_latest_summary(session_id, SUMMARY_FINAL)
    live = assistant.repository.get_latest_summary(session_id, SUMMARY_LIVE)
    summary = final or live
    summary_text = summary.content if summary else "Bu oturum için özet bulunamadı."

    history_detail_title_label.set_text(session.title)
    history_detail_summary_area.set_content(summary_text)
    history_detail_transcript_area.set_content(
        transcript if transcript.strip() else "_Bu oturum için transkript yok._"
    )
    history_detail_empty.set_visibility(False)
    history_detail_content.set_visibility(True)


def clear_history_detail():
    _history_state["selected_id"] = None
    history_detail_content.set_visibility(False)
    history_detail_empty.set_visibility(True)


def open_rename_dialog(session_id: str, current_title: str):
    with ui.dialog() as dialog, ui.card().classes("vesper-glass p-5 gap-3").style("min-width: 320px;"):
        ui.label("Oturumu yeniden adlandır").classes("vesper-display text-base font-medium text-slate-100")
        title_input = ui.input(value=current_title).classes("w-full").props("dark outlined dense")

        def do_rename():
            try:
                assistant.repository.rename_session(session_id, title_input.value)
            except ValueError as e:
                ui.notify(str(e), type="negative")
                return
            dialog.close()
            refresh_history_list()
            if _history_state["selected_id"] == session_id:
                history_detail_title_label.set_text(title_input.value.strip())

        with ui.row().classes("justify-end gap-2 w-full mt-1"):
            ui.button("İptal", on_click=dialog.close).props("flat").classes("text-slate-400")
            ui.button("Kaydet", on_click=do_rename).classes("vesper-btn vesper-btn-primary")
    dialog.open()


def open_delete_dialog(session_id: str, title: str):
    with ui.dialog() as dialog, ui.card().classes("vesper-glass p-5 gap-3").style("min-width: 320px;"):
        ui.label(f'"{title}" silinsin mi?').classes("vesper-display text-base font-medium text-slate-100")
        ui.label("Bu işlem geri alınamaz; transkript ve özetler kalıcı olarak silinir.").classes(
            "text-xs text-slate-400"
        )

        def do_delete():
            assistant.repository.delete_session(session_id)
            dialog.close()
            if _history_state["selected_id"] == session_id:
                clear_history_detail()
            refresh_history_list()
            ui.notify("Oturum silindi.", type="warning")

        with ui.row().classes("justify-end gap-2 w-full mt-1"):
            ui.button("İptal", on_click=dialog.close).props("flat").classes("text-slate-400")
            ui.button("Sil", on_click=do_delete).classes("vesper-btn vesper-btn-stop")
    dialog.open()


def _render_history_row(sess):
    with ui.row().classes("vesper-glass items-center gap-3 px-4 py-3 w-full"):
        with ui.column().classes("gap-1 flex-1 cursor-pointer").on(
            "click", lambda: open_session_detail(sess.id)
        ):
            with ui.row().classes("items-center gap-2"):
                ui.html(f'<span class="vesper-dot {_status_dot_class(sess.status)}"></span>').classes("flex-none")
                ui.label(sess.title).classes("vesper-display text-sm font-medium text-slate-100")
                if sess.status != STATUS_COMPLETED:
                    ui.label(_status_label(sess.status)).classes(
                        "vesper-mono text-xs uppercase text-amber-400"
                    )
            ui.label(
                f"{_format_local_datetime(sess.started_at)}  ·  {_format_duration(sess.started_at, sess.ended_at)}"
            ).classes("vesper-mono text-xs text-slate-500")

        with ui.row().classes("gap-1 flex-none"):
            ui.button(icon="edit", on_click=lambda: open_rename_dialog(sess.id, sess.title)).props(
                "flat round dense"
            ).classes("vesper-icon-btn")
            ui.button(icon="delete", on_click=lambda: open_delete_dialog(sess.id, sess.title)).props(
                "flat round dense"
            ).classes("vesper-icon-btn vesper-icon-btn-danger")


def refresh_history_list():
    history_list_container.clear()
    sessions = assistant.repository.list_sessions()

    if not sessions:
        with history_list_container:
            ui.label("Henüz kaydedilmiş oturum yok.").classes(
                "text-sm text-slate-500 text-center py-6"
            )
        return

    with history_list_container:
        for sess in sessions:
            _render_history_row(sess)

    if _history_state["selected_id"] and not any(s.id == _history_state["selected_id"] for s in sessions):
        clear_history_detail()


# ---------------------------------------------------------------------------
# Sekme geçişi (Oturum / Geçmiş) — Quasar QTabs yerine, mevcut Vesper görsel
# diline uyan basit, elle stillendirilmiş iki etiket.
# ---------------------------------------------------------------------------

session_view = None
history_view = None
tab_session_btn = None
tab_history_btn = None


def _switch_view(name: str):
    session_view.set_visibility(name == "oturum")
    history_view.set_visibility(name == "gecmis")
    tab_session_btn.classes(replace="vesper-tab" + (" vesper-tab-active" if name == "oturum" else ""))
    tab_history_btn.classes(replace="vesper-tab" + (" vesper-tab-active" if name == "gecmis" else ""))
    if name == "gecmis":
        refresh_history_list()


# ---------------------------------------------------------------------------
# <head>: fontlar + Vesper'a özel CSS
# ---------------------------------------------------------------------------

ui.add_head_html("""
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link
  href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap"
  rel="stylesheet"
>
<style>
  :root {
    --vesper-bg: #050507;
    --vesper-line: rgba(255, 255, 255, 0.08);
    --vesper-violet: #7c6cff;
    --vesper-cyan: #22d3ee;
    --vesper-amber: #f5a524;
    --vesper-text: #f2f2f5;
    --vesper-muted: #93939f;
  }

  html, body {
    background: var(--vesper-bg) !important;
    color: var(--vesper-text);
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
  }

  .vesper-display { font-family: 'Space Grotesk', 'Inter', sans-serif; }
  .vesper-mono { font-family: 'JetBrains Mono', ui-monospace, monospace; }

  /* ---------- Aurora arka plan ---------- */
  .vesper-aurora {
    position: fixed;
    inset: 0;
    z-index: 0;
    overflow: hidden;
    pointer-events: none;
    background: var(--vesper-bg);
  }
  .vesper-blob {
    position: absolute;
    border-radius: 9999px;
    filter: blur(110px);
    will-change: transform;
  }
  .vesper-blob-1 {
    width: 44vw; height: 44vw; top: -14%; left: -10%;
    background: radial-gradient(circle, var(--vesper-violet) 0%, transparent 70%);
    opacity: 0.28;
    animation: vesperDrift1 24s ease-in-out infinite;
  }
  .vesper-blob-2 {
    width: 40vw; height: 40vw; bottom: -18%; right: -12%;
    background: radial-gradient(circle, var(--vesper-cyan) 0%, transparent 70%);
    opacity: 0.22;
    animation: vesperDrift2 28s ease-in-out infinite;
  }
  @keyframes vesperDrift1 {
    0%, 100% { transform: translate3d(0, 0, 0) scale(1); }
    50%      { transform: translate3d(5vw, 6vh, 0) scale(1.12); }
  }
  @keyframes vesperDrift2 {
    0%, 100% { transform: translate3d(0, 0, 0) scale(1); }
    50%      { transform: translate3d(-6vw, -5vh, 0) scale(1.08); }
  }
  .vesper-grid {
    position: absolute; inset: 0;
    background-image:
      linear-gradient(rgba(255, 255, 255, 0.035) 1px, transparent 1px),
      linear-gradient(90deg, rgba(255, 255, 255, 0.035) 1px, transparent 1px);
    background-size: 44px 44px;
    -webkit-mask-image: radial-gradient(ellipse 80% 55% at 50% 15%, black 0%, transparent 75%);
            mask-image: radial-gradient(ellipse 80% 55% at 50% 15%, black 0%, transparent 75%);
  }

  /* ---------- Cam panel ---------- */
  .vesper-glass {
    background: rgba(255, 255, 255, 0.035) !important;
    border: 1px solid var(--vesper-line) !important;
    backdrop-filter: blur(24px);
    -webkit-backdrop-filter: blur(24px);
    border-radius: 18px !important;
  }

  /* ---------- İnce scrollbar ---------- */
  .vesper-scroll { scrollbar-width: thin; scrollbar-color: rgba(255, 255, 255, 0.16) transparent; }
  .vesper-scroll::-webkit-scrollbar { width: 6px; }
  .vesper-scroll::-webkit-scrollbar-track { background: transparent; }
  .vesper-scroll::-webkit-scrollbar-thumb { background: rgba(255, 255, 255, 0.16); border-radius: 999px; }

  /* ---------- Markdown içerik tipografisi ---------- */
  .vesper-md :where(p) { margin: 0 0 0.75em 0; }
  .vesper-md :where(ul, ol) { margin: 0 0 0.75em 1.25em; }
  .vesper-md :where(li) { margin-bottom: 0.25em; }
  .vesper-md :where(strong) { color: #fff; font-weight: 600; }
  .vesper-md :where(h1, h2, h3) {
    font-family: 'Space Grotesk', sans-serif; color: #fff;
    margin: 0.6em 0 0.4em; font-weight: 600;
  }
  .vesper-md :where(h1) { font-size: 1.05rem; }
  .vesper-md :where(h2) { font-size: 0.98rem; }
  .vesper-md :where(h3) { font-size: 0.9rem; }
  .vesper-md :where(code) {
    background: rgba(255, 255, 255, 0.08); padding: 0.1em 0.4em;
    border-radius: 4px; font-family: 'JetBrains Mono', monospace; font-size: 0.85em;
  }
  .vesper-md :where(a) { color: var(--vesper-cyan); }
  .vesper-md :where(em) { color: var(--vesper-muted); }
  .vesper-md > :first-child { margin-top: 0 !important; }
  .vesper-md > :last-child { margin-bottom: 0 !important; }

  /* ---------- Durum noktası ---------- */
  .vesper-dot { display: inline-block; width: 7px; height: 7px; border-radius: 999px; }
  .vesper-dot-pulse { animation: vesperPulse 1.6s ease-in-out infinite; }
  @keyframes vesperPulse {
    0%, 100% { opacity: 1; box-shadow: 0 0 0 0 currentColor; }
    50%      { opacity: 0.55; box-shadow: 0 0 0 5px transparent; }
  }

  /* ---------- Logo / eşitleyici ikonu ---------- */
  .vesper-eq-bar { transform-origin: center; transform: scaleY(0.35); }
  .vesper-eq-active .vesper-eq-bar { animation: vesperEq 0.9s ease-in-out infinite; }
  .vesper-eq-active .vesper-eq-bar:nth-child(1) { animation-delay: 0s; }
  .vesper-eq-active .vesper-eq-bar:nth-child(2) { animation-delay: 0.15s; }
  .vesper-eq-active .vesper-eq-bar:nth-child(3) { animation-delay: 0.3s; }
  .vesper-eq-active .vesper-eq-bar:nth-child(4) { animation-delay: 0.45s; }
  @keyframes vesperEq {
    0%, 100% { transform: scaleY(0.35); }
    50%      { transform: scaleY(1); }
  }

  /* ---------- Butonlar ---------- */
  .vesper-btn {
    padding: 0.7rem 1.5rem !important;
    border-radius: 0.75rem !important;
    font-size: 0.875rem !important;
    transition: filter 0.15s ease, background 0.15s ease;
  }
  .vesper-btn-primary {
    background: linear-gradient(90deg, var(--vesper-violet), var(--vesper-cyan)) !important;
    color: #060608 !important;
    font-weight: 600 !important;
    box-shadow: 0 0 0 1px rgba(124, 108, 255, 0.5), 0 10px 30px -10px rgba(124, 108, 255, 0.65);
  }
  .vesper-btn-primary:hover { filter: brightness(1.1); }
  .vesper-btn-primary:disabled, .vesper-btn-primary[disabled] {
    opacity: 0.35 !important; box-shadow: none !important; cursor: not-allowed;
  }
  .vesper-btn-stop {
    background: transparent !important;
    border: 1px solid rgba(248, 113, 113, 0.4) !important;
    color: #fca5a5 !important;
    font-weight: 500 !important;
  }
  .vesper-btn-stop:hover { background: rgba(248, 113, 113, 0.08) !important; }
  .vesper-btn-stop:disabled, .vesper-btn-stop[disabled] {
    opacity: 0.3 !important; cursor: not-allowed;
  }

  /* ---------- Bölünmüş panel + sürüklenebilir ayırıcı (Oturum sekmesi) ---------- */
  .vesper-split { display: flex; flex-wrap: nowrap; align-items: stretch; }
  .vesper-resize-handle {
    flex: 0 0 18px;
    cursor: col-resize;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .vesper-resize-thumb {
    width: 3px; height: 40px; border-radius: 999px;
    background: rgba(255, 255, 255, 0.15);
    transition: background 0.2s ease;
  }
  .vesper-resize-handle:hover .vesper-resize-thumb,
  .vesper-resize-handle.vesper-dragging .vesper-resize-thumb {
    background: var(--vesper-violet);
  }

  .vesper-panel-tag {
    display: inline-block; width: 7px; height: 7px; border-radius: 2px;
  }

  @media (max-width: 860px) {
    .vesper-split { flex-direction: column; }
    .vesper-panel-left, .vesper-panel-right { flex-basis: auto !important; width: 100% !important; }
    .vesper-resize-handle { display: none; }
  }

  /* ---------- Sekmeler (Oturum / Geçmiş) ---------- */
  .vesper-tab {
    padding: 0.45rem 1rem;
    border-radius: 0.6rem;
    font-size: 0.875rem;
    color: var(--vesper-muted);
    cursor: pointer;
    transition: color 0.15s ease, background 0.15s ease;
    user-select: none;
  }
  .vesper-tab:hover { color: #fff; }
  .vesper-tab-active { color: #fff !important; background: rgba(255, 255, 255, 0.06); }

  /* ---------- Geçmiş: statik (sürüklenemeyen) ikili panel ---------- */
  .vesper-history-split { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
  @media (max-width: 860px) {
    .vesper-history-split { grid-template-columns: 1fr; }
  }

  .vesper-icon-btn { color: var(--vesper-muted) !important; }
  .vesper-icon-btn:hover { color: #fff !important; }
  .vesper-icon-btn-danger:hover { color: #fca5a5 !important; }
</style>
""")

ui.dark_mode().enable()

# ---------------------------------------------------------------------------
# Animasyonlu aurora arka planı (sabit, tüm görünümü kaplar, en arkada)
# ---------------------------------------------------------------------------

ui.html("""
<div class="vesper-aurora">
  <div class="vesper-blob vesper-blob-1"></div>
  <div class="vesper-blob vesper-blob-2"></div>
  <div class="vesper-grid"></div>
</div>
""")

# ---------------------------------------------------------------------------
# Ana arayüz
# ---------------------------------------------------------------------------

with ui.column().classes("relative z-10 w-full max-w-6xl mx-auto p-6 gap-6 min-h-screen"):

    # ---- Üst bilgi: logo + durum rozeti ----
    with ui.row().classes("items-center justify-between w-full"):
        with ui.row().classes("items-center gap-3"):
            waveform_icon = ui.html("""
                <svg width="20" height="20" viewBox="0 0 20 20" fill="none" style="color:#7c6cff">
                  <rect class="vesper-eq-bar" x="1" y="7" width="3" height="6" rx="1.5" fill="currentColor"/>
                  <rect class="vesper-eq-bar" x="6" y="3" width="3" height="14" rx="1.5" fill="currentColor"/>
                  <rect class="vesper-eq-bar" x="11" y="5" width="3" height="10" rx="1.5" fill="currentColor"/>
                  <rect class="vesper-eq-bar" x="16" y="8" width="3" height="4" rx="1.5" fill="currentColor"/>
                </svg>
            """).classes("flex-none")

            with ui.column().classes("gap-0"):
                ui.label("VESPER").classes(
                    "vesper-display text-xl font-semibold tracking-widest text-white"
                )
                ui.label("Yerel Webinar Zekâsı").classes("text-xs text-slate-400")

        with ui.row().classes("vesper-glass items-center gap-2 px-3 py-1.5"):
            status_dot = ui.html("<span></span>").classes("flex-none")
            status_badge = ui.label("Beklemede").classes("vesper-mono text-xs text-slate-300")

    # ---- Sekmeler ----
    with ui.row().classes("gap-2"):
        tab_session_btn = ui.label("Oturum").classes("vesper-tab vesper-tab-active")
        tab_session_btn.on("click", lambda: _switch_view("oturum"))
        tab_history_btn = ui.label("Geçmiş").classes("vesper-tab")
        tab_history_btn.on("click", lambda: _switch_view("gecmis"))

    # ======================================================================
    # OTURUM sekmesi — canlı görünüm (mevcut tasarım/davranış AYNEN korunmuştur)
    # ======================================================================
    with ui.column().classes("w-full flex-1 gap-6") as session_view:

        with ui.row().classes("items-center gap-3"):
            device_select = ui.select(
                _build_device_options(), value=None, on_change=on_device_change
            ).props("dark outlined dense").classes("min-w-[220px]")

            start_button = ui.button("Başlat", on_click=on_start)
            start_button.props("unelevated no-caps")
            start_button.classes("vesper-btn vesper-btn-primary")

            stop_button = ui.button("Durdur ve Final Özet Çıkar", on_click=on_stop)
            stop_button.props("unelevated no-caps")
            stop_button.classes("vesper-btn vesper-btn-stop")
            stop_button.disable()

        with ui.row().classes("vesper-split w-full flex-1 gap-0"):

            with ui.column().classes(
                "vesper-glass vesper-panel-left flex-1 p-5 gap-3"
            ).style("flex-basis: 50%;"):
                with ui.row().classes("items-center gap-2"):
                    ui.html('<span class="vesper-panel-tag" style="background:#7c6cff"></span>').classes("flex-none")
                    ui.label("Canlı Özet").classes("vesper-display text-base font-medium text-slate-100")
                summary_area = ui.markdown("Henüz özet yok, başlat'a bas.").classes(
                    "vesper-scroll vesper-md flex-1 overflow-y-auto text-slate-300 text-sm leading-relaxed"
                ).style("max-height: 55vh;")

            ui.html('<div class="vesper-resize-thumb"></div>').classes(
                "vesper-resize-handle select-none"
            )

            with ui.column().classes(
                "vesper-glass vesper-panel-right flex-1 p-5 gap-3"
            ).style("flex-basis: 50%;"):
                with ui.row().classes("items-center gap-2"):
                    ui.html('<span class="vesper-panel-tag" style="background:#22d3ee"></span>').classes("flex-none")
                    ui.label("Canlı Transkript (son kısım)").classes("vesper-display text-base font-medium text-slate-100")
                transcript_area = ui.markdown("_Transkript burada görünecek..._").classes(
                    "vesper-scroll vesper-md flex-1 overflow-y-auto text-slate-400 text-sm leading-relaxed"
                ).style("max-height: 55vh;")

    # ======================================================================
    # GEÇMİŞ sekmesi — kalıcı oturum listesi + detay (v0.2.0, yeni)
    # ======================================================================
    with ui.column().classes("w-full flex-1 gap-4") as history_view:
        history_view.set_visibility(False)

        history_list_container = ui.column().classes(
            "vesper-scroll w-full gap-2 overflow-y-auto"
        ).style("max-height: 32vh;")

        with ui.column().classes("w-full flex-1 gap-3") as history_detail_empty:
            ui.label("Bir oturum seçtiğinde özeti ve transkripti burada göreceksin.").classes(
                "text-sm text-slate-500 text-center py-10"
            )

        with ui.column().classes("w-full flex-1 gap-3") as history_detail_content:
            history_detail_content.set_visibility(False)
            with ui.row().classes("items-center gap-2"):
                ui.html('<span class="vesper-panel-tag" style="background:#7c6cff"></span>').classes("flex-none")
                history_detail_title_label = ui.label("").classes(
                    "vesper-display text-base font-medium text-slate-100"
                )

            with ui.row().classes("vesper-history-split w-full flex-1"):
                with ui.column().classes("vesper-glass flex-1 p-5 gap-3"):
                    ui.label("Özet").classes("vesper-display text-sm font-medium text-slate-300")
                    history_detail_summary_area = ui.markdown("").classes(
                        "vesper-scroll vesper-md flex-1 overflow-y-auto text-slate-300 text-sm leading-relaxed"
                    ).style("max-height: 45vh;")

                with ui.column().classes("vesper-glass flex-1 p-5 gap-3"):
                    ui.label("Transkript").classes("vesper-display text-sm font-medium text-slate-300")
                    history_detail_transcript_area = ui.markdown("").classes(
                        "vesper-scroll vesper-md flex-1 overflow-y-auto text-slate-400 text-sm leading-relaxed"
                    ).style("max-height: 45vh;")

    # ---- Alt bilgi ----
    ui.label("Transkripsiyon ve özetleme yerel olarak çalışır — bulut AI API'si kullanılmaz.").classes(
        "text-center text-xs text-slate-600 mt-2"
    )

# Sayfa ilk yüklendiğinde durum görselini "Beklemede" durumuna göre ayarla
# (ilk refresh() çağrısı 5 saniye sonra gelecek, o ana kadar boş kalmasın)
_apply_status_visuals(assistant.status)

# ---------------------------------------------------------------------------
# Sürüklenebilir ayırıcı için vanilla JS (DOM hazır olduktan sonra çalışır)
# Sadece Oturum sekmesindeki CANLI panel içindir — Geçmiş'teki statik ikili
# panel kasıtlı olarak bunu kullanmaz (bkz. .vesper-history-split).
# ---------------------------------------------------------------------------

ui.add_body_html("""
<script>
(function () {
  function init() {
    var handle = document.querySelector('.vesper-resize-handle');
    var left = document.querySelector('.vesper-panel-left');
    var right = document.querySelector('.vesper-panel-right');
    var split = document.querySelector('.vesper-split');
    if (!handle || !left || !right || !split) return;

    var dragging = false;

    handle.addEventListener('mousedown', function (e) {
      dragging = true;
      handle.classList.add('vesper-dragging');
      document.body.style.userSelect = 'none';
      e.preventDefault();
    });

    window.addEventListener('mousemove', function (e) {
      if (!dragging) return;
      var rect = split.getBoundingClientRect();
      var pct = ((e.clientX - rect.left) / rect.width) * 100;
      pct = Math.min(75, Math.max(25, pct));
      left.style.flexBasis = pct + '%';
      right.style.flexBasis = (100 - pct) + '%';
    });

    window.addEventListener('mouseup', function () {
      dragging = false;
      handle.classList.remove('vesper-dragging');
      document.body.style.userSelect = '';
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
</script>
""")

ui.timer(5.0, refresh)

ui.run(title="Vesper", port=8080, reload=False)
