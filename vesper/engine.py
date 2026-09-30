"""
Ses yakalama -> transkript -> özetleme akışını yöneten ana sınıf.
Üç arka plan thread'i çalıştırır: yakalama, transkripsiyon, periyodik özetleme.

v0.2.0: Oturumlar artık kalıcıdır (bkz. storage.py / repository.py / session.py).
Her transkript parçası ve her özet, üretildiği anda SQLite'a yazılır — sadece
Durdur'a basıldığında değil. outputs/*.md dosyası hâlâ üretilir ama artık
KAYNAK değil, kalıcı veriden türetilen bir DIŞA AKTARIMDIR.

v0.3.0: canlı özet işçisi (_summarize_loop) bir özetleme istisnasıyla ÖLMEZ
(bkz. _run_live_summary_once). LLM hız/kalite profili düz Python metotlarıyla
seçilir (list_llm_profiles / set_llm_profile / get_active_llm_profile_key) ve
HER özet, gerçekte kullanılan modelin adıyla summaries.model'e yazılır.
"""

import logging
import os
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import config
from .audio_capture import AudioCapture
from .transcriber import Transcriber
from .summarizer import Summarizer
from .repository import SessionRepository
from .session import STATUS_COMPLETED, STATUS_INTERRUPTED, SUMMARY_FINAL, SUMMARY_LIVE
from .storage import Database

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
# v0.3.0: _transcribe_loop() boş kuyrukta SONSUZA kadar bloke olmak yerine
# bu süre aşıldığında uyanıp capture.get_error()'ı kontrol eder — böylece
# yakalama işçisi çöktüğünde kullanıcı sonsuza kadar "Dinleniyor" görmez.
CAPTURE_QUEUE_POLL_TIMEOUT_SEC = 1.0


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
        # v0.3.0: yakalama işçisi çalışırken çöktüğünde bir kez True olur
        # (bkz. _sync_capture_failure_state). Bu, EN İYİ ÇABA (best-effort)
        # bir UI/durum bildirimi amaçlıdır; stop()'un kendisi finalizasyon
        # kararı için HER ZAMAN capture.get_error()'ı TAZE olarak yeniden
        # okur, bu bayrağa güvenmez (bkz. stop() içindeki yorum).
        self.capture_failed = False

        # v0.3.0: canlı özet arızası bildirimi. live_summary_failure_episode
        # ARTAN (monoton) bir sayaçtır ve YALNIZCA burada, BİR KEZ başlatılır;
        # start() onu ASLA sıfırlamaz (UI kendi "son bildirilen" değerini bu
        # sayaçla eşitler — bkz. ui.py). _live_summary_failing ise şu anki
        # ardışık-arıza döneminin açık olup olmadığını tutar; start() her yeni
        # oturumda yalnızca bunu False'a çeker.
        self.live_summary_failure_episode = 0
        self._live_summary_failing = False

        # v0.3.0 Checkpoint B devamı (overlapping-worker düzeltmesi): bir
        # arka plan canlı-özet işçisinin HANGİ start()'a ait olduğunu
        # belirler, böylece bir SONRAKİ start() tarafından "yetim"
        # bırakılan bir işçi, artık geçerli olmayan bir oturuma
        # yazmak/kaydetmek yerine bunu FARK EDİP durabilir. Monoton, asla
        # sıfırlanmaz (live_summary_failure_episode gibi). Sahiplik
        # kontrolü VE yayınlama (self.live_summary / SQLite hedefi / arıza
        # sayaçları / durum) bu kilit altında TEK, bölünmez bir adımda
        # yapılır; kilit ASLA Ollama çağrısı, uyku veya ses/transkripsiyon
        # işi boyunca tutulmaz — yalnızca bu kısa sahiplik-doğrulama +
        # durum-güncelleme sınırını korur (bkz. start(),
        # _run_live_summary_once, _summarize_loop).
        self._session_generation = 0
        self._session_generation_lock = threading.Lock()

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
    # v0.3.0 — ses kaynağı seçimi. UI (veya başka bir çağıran) SADECE bu
    # ikisiyle konuşur; vesper.audio_capture'a doğrudan bağımlılık kurmaz.
    # Düz Python verisi/parametreleri kullanır — NiceGUI'ye dair hiçbir şey
    # bu sınıra geçmez (CLAUDE.md §5.7).
    def list_audio_devices(self) -> List[Dict[str, Any]]:
        return self.capture.list_available_devices()

    def set_audio_device(self, device_index: Optional[int]) -> None:
        if self._running:
            raise RuntimeError("Oturum sürerken ses kaynağı değiştirilemez.")
        self.capture.set_device(device_index)

    # ------------------------------------------------------------------ #
    # v0.3.0 — LLM profilleri (hız/kalite). UI, Summarizer'a doğrudan DEĞİL,
    # yalnızca bu üç metotla konuşur; düz Python verisi kullanılır, NiceGUI'ye
    # dair hiçbir şey bu sınıra geçmez (CLAUDE.md §5.7). Ses cihazı seçimindeki
    # politika burada da geçerlidir: oturum sürerken profil değişmez.
    def list_llm_profiles(self) -> List[Dict[str, str]]:
        return self.summarizer.list_profiles()

    def get_active_llm_profile_key(self) -> str:
        return self.summarizer.get_active_profile_key()

    def set_llm_profile(self, key: str) -> None:
        if self._running:
            raise RuntimeError("Oturum sürerken LLM profili değiştirilemez.")
        self.summarizer.set_profile(key)

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
        self.capture_failed = False

        try:
            self.capture.start()
        except Exception:
            # WASAPI cihaz başlatma hatası vb. — _running'i geri almazsak bu
            # instance BİR DAHA HİÇ start() edilemez (üstteki "if self._running:
            # return" sonsuza kadar sessizce hiçbir şey yapmaz).
            log.exception("Ses yakalama başlatılamadı")
            self._running = False
            raise

        # Jenerasyon geçişi + bu oturuma ait canlı-özet durumunun
        # sıfırlanması TEK bir kilitli adımda yapılır — böylece bir
        # işçinin sahiplik kontrolü BU geçişle asla yarım kalmış bir
        # hâlde çakışamaz: ya TAMAMEN öncesini ya TAMAMEN sonrasını görür
        # (bkz. __init__'teki not). `my_generation`, az sonra
        # oluşturulacak _summarize_thread'e SABİT bir argüman olarak
        # geçirilir (thread kendi kendine yeniden OKUMAZ) — böylece bu
        # işçinin kimliği, thread fiilen çalışmaya başlamadan önce bile
        # belirsizlik taşımaz. live_summary_failure_episode SIFIRLANMAZ
        # (monoton kalır); yalnızca "şu an arızalı mıyız" bayrağı
        # temizlenir.
        with self._session_generation_lock:
            self._session_generation += 1
            my_generation = self._session_generation
            self._live_summary_failing = False
            self.live_summary = ""

        # v0.3.0: create_session() artık capture.start()'TAN SONRA çağrılır,
        # böylece source_name UI'nin verdiği bir string değil, ses katmanının
        # GERÇEKTEN çözdüğü cihazın adı olur (bkz. AudioCapture.
        # get_active_device_name). Yan etki: capture başlatma hatası artık
        # kalıcı bir "active" oturum satırı YARATMIYOR (önceden yaratıyordu;
        # bir sonraki açılışta recover_interrupted_sessions() tarafından zaten
        # zararsız şekilde temizlenirdi).
        default_title = f"Webinar — {datetime.now():%Y-%m-%d %H:%M}"
        try:
            self.session_id = self.repository.create_session(
                title=default_title,
                source_type="system_audio",
                source_name=self.capture.get_active_device_name(),
                source_language=config.WHISPER_LANGUAGE,
                summary_language="tr",
            )
        except Exception:
            # SQLite'a yazılamasa bile oturum bellekte devam edebilmeli;
            # ama bunu sessizce yutma, yüksek sesle logla.
            self.session_id = None
            log.exception("Yeni oturum SQLite'a kaydedilemedi — bu oturum kalıcı OLMAYACAK")

        self._transcribe_thread = threading.Thread(target=self._transcribe_loop, daemon=True)
        self._summarize_thread = threading.Thread(
            target=self._summarize_loop, args=(my_generation,), daemon=True
        )
        self._threads = [self._transcribe_thread, self._summarize_thread]
        for t in self._threads:
            t.start()

    def stop(self) -> str:
        """Dinlemeyi durdurur. Yalnızca ses üreticisinin KESİN olarak öldüğü
        kanıtlanırsa transkripsiyon sentinel'ini kuyruğa koyar, yalnızca tüm
        kabul edilmiş ses kanıtlanmış şekilde işlendiyse özet üretir, ve
        yalnızca özet+dışa aktarım tamamen başarılı olursa oturumu TAMAMLANDI
        işaretler. Her başarısızlık noktasında oturum 'active' kalır ve bir
        sonraki başlangıçta normal kurtarma akışıyla 'interrupted' olur.

        v0.3.0: capture.get_error() doluysa (yakalama işçisi bir cihaz/host
        hatasıyla çöktüyse), drain ve özet üretimi YİNE DE normal şekilde
        çalışır — ama oturum STATUS_COMPLETED değil STATUS_INTERRUPTED
        işaretlenir (last_stop_finalized=False). Yakalanan hiçbir veri
        kaybolmaz; sadece oturumun TEMİZ tamamlandığı iddiası kaldırılır."""
        if not self._running:
            return self.live_summary
        self._running = False

        # 1) Ses üreticisinin KESİN olarak öldüğünü kanıtla. Bu kanıtlanmadan
        # sentinel'i kuyruğa KOYMA — aksi halde AudioCapture'ın finally
        # bloğundaki son konuşma parçası sentinel'den SONRA kuyruğa girebilir
        # ve transkripsiyon işçisi onu hiç görmeden çıkabilir.
        #
        # v0.3.0 notu: capture.stop()'un "üretici öldü" kanıtı
        # (not self._thread.is_alive()) bir ÇÖKME sonrası da GEÇERLİ kalır.
        # Çöken bir _run() thread'i zaten sona ermiştir — is_alive() anında
        # False döner, tam olarak temiz bir çıkışla AYNI garantiyi verir
        # ("bu thread bir daha ASLA kuyruğa bir şey koymayacak"). Bu yüzden
        # sentinel'i koymak çökme sonrasında da GÜVENLİDİR; invariant
        # ATLANMAZ, sadece çökme de invariant'ın zaten kapsadığı bir
        # "üretici kesinlikle öldü" durumudur.
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
        #
        # v0.3.0: bu noktada capture.get_error()'ı BİR KEZ, TAZE olarak
        # okuyoruz — self.capture_failed'e (best-effort UI bayrağına) DEĞİL.
        # Çünkü kullanıcı Durdur'a, _sync_capture_failure_state()'in bir
        # sonraki yoklama turuna ULAŞMADAN ÖNCE basmış olabilir; bu durumda
        # capture_failed henüz False olabilir ama hata zaten oluşmuştur.
        self.status = "Final özet hazırlanıyor..."
        full_text = self.get_full_transcript()
        capture_error = self.capture.get_error()
        try:
            final_summary = self.summarizer.summarize(full_text, is_final=True)

            if self.session_id:
                self.repository.save_summary(
                    self.session_id, kind=SUMMARY_FINAL,
                    content=final_summary, model=self.summarizer.get_active_model(),
                )

            self._save_output(full_text, final_summary)

            if self.session_id:
                if capture_error is not None:
                    # Ses kaynağı oturum SIRASINDA koptu (ör. Bluetooth cihaz
                    # kapandı). Yakalanan her şey işlendi ve kalıcı kaydedildi,
                    # final özet de üretildi — ama oturum TEMİZ bir Durdur ile
                    # değil, kaynağın beklenmedik ölümüyle bitti. STATUS_
                    # INTERRUPTED zaten "temiz kapanmadı" anlamına geliyor
                    # (bkz. session.py) ve bu durumu da doğru şekilde
                    # kapsıyor; yeni bir şema/durum değeri gerekmiyor.
                    self.repository.finish_session(self.session_id, status=STATUS_INTERRUPTED)
                    self.last_stop_finalized = False
                    self.status = "Durduruldu (ses kaynağı koptu)"
                    log.error(
                        "Oturum ses kaynağı hatasıyla bitti; TAMAMLANDI değil, "
                        "INTERRUPTED olarak işaretlendi: %s", capture_error,
                    )
                else:
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

    def _sync_capture_failure_state(self) -> None:
        """capture.get_error() içinde bir hata VARSA ve daha önce fark
        edilmediyse, capture_failed/status'u BİR KEZ günceller ve loglar.
        İdempotenttir — tekrar tekrar çağrılması güvenlidir, sadece İLK
        geçişte bir şey yapar. _transcribe_loop içinde HEM kuyruk boşken
        (queue.Empty) HEM DE bir yığın (backlog) işlenirken çağrılır, böylece
        kuyruk boşalana kadar fark edilmesi GECİKMEZ. Bu sadece EN İYİ ÇABA
        bir bildirimdir — stop() kendi kararı için capture.get_error()'ı
        HER ZAMAN ayrıca ve TAZE olarak okur, bu fonksiyona/bayrağa güvenmez."""
        if not self.capture_failed and self.capture.get_error() is not None:
            self.capture_failed = True
            self.status = "Ses kaynağı beklenmedik şekilde koptu"
            log.error("Ses yakalama işçisi çalışırken çöktü: %s", self.capture.get_error())

    # ------------------------------------------------------------------ #
    def _transcribe_loop(self):
        try:
            # Artık `_running`/`empty()` yoklamıyoruz: stop() üreticinin kesin
            # öldüğünü kanıtladıktan SONRA kuyruğa _TRANSCRIBE_SENTINEL koyar.
            # FIFO sıra sayesinde sentinel'e ulaşmak, ondan önce kabul edilmiş
            # her şeyin işlendiğinin kanıtıdır.
            #
            # v0.3.0: .get() artık SONSUZA kadar bloke olmuyor — CAPTURE_
            # QUEUE_POLL_TIMEOUT_SEC aşılırsa (kuyruk boş) uyanıp yakalama
            # işçisinin çöküp çökmediğini kontrol eder. Bu kontrol AYRICA her
            # gerçek öğe işlendikten sonra da yapılır — böylece çöküşün
            # fark edilmesi, arkasında bir yığın (backlog) varsa kuyruk
            # boşalana kadar gecikmez.
            while True:
                try:
                    item = self._audio_queue.get(timeout=CAPTURE_QUEUE_POLL_TIMEOUT_SEC)
                except queue.Empty:
                    self._sync_capture_failure_state()
                    continue

                self._sync_capture_failure_state()

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

    def _run_live_summary_once(self, generation: Optional[int] = None) -> None:
        """Tek bir canlı özet turu. _summarize_loop() her aralıkta bunu çağırır,
        kendi doğuşunda sabitlediği `generation` değerini ileterek; ayrı bir
        metot olmasının sebebi, arıza/kurtarma mantığının thread zamanlamasına
        güvenmeden doğrudan test edilebilmesidir (doğrudan çağrılarda
        `generation` verilmezse GÜNCEL jenerasyona çözülür, bu yüzden senkron
        bir test çağrısı için aşağıdaki sahiplik kontrolü asla tetiklenmez).

        Bir özetleme istisnası işçiyi ÖLDÜRMEZ: loglanır, son BAŞARILI
        live_summary korunur ve bir sonraki normal aralıkta yeniden denenir.
        Durum ('Dinleniyor') YALNIZCA oturum hâlâ sürüyorsa VE yakalama arızası
        gözlemlenmediyse (self._running and not self.capture_failed) geri
        yüklenir — hem başarı hem arıza yolunda; aynı koşul, turun başındaki
        'Canlı özet güncelleniyor...' duyurusu için de geçerlidir. stop()
        başladıktan sonra biten bir canlı özet stop()'un durum metnini, ses
        kaynağı koptuktan sonra biten bir canlı özet ise 'Ses kaynağı
        beklenmedik şekilde koptu' durumunu EZEMEZ (Checkpoint A değişmezi:
        kullanıcı Durdur'a basana dek arıza durumu görünür kalır).

        ATOMİK OTURUM SAHİPLİĞİ (overlapping-worker düzeltmesi): summarize()
        (Ollama HTTP'si — YAVAŞ, kilit ALTINDA DEĞİL) döndükten SONRAKİ
        gerçek yayınlama (self.live_summary / SQLite hedefi / arıza
        sayaçları / durum), self._session_generation_lock altında TEK,
        bölünmez bir kritik bölgede yapılır: `generation ==
        self._session_generation` kontrolü İLE bu yazımlar AYNI kilitli
        bölgenin içindedir. start() içindeki jenerasyon geçişi de AYNI
        kilidi kullandığı için, iki taraf da ya TAMAMEN eski jenerasyonun
        sahip olduğu hâliyle ya da TAMAMEN yeni jenerasyona ait hâliyle
        görülür — kontrol GEÇTİKTEN ama yazım TAMAMLANMADAN ÖNCE bir
        start() araya giremez (TOCTOU penceresi yoktur). Jenerasyon
        eşleşmezse (bir SONRAKİ oturum zaten başlamış), bu tur SESSİZCE
        (yalnızca bir log satırıyla) tamamen atılır: ne yeni oturuma ne de
        (jenerasyon zaten değiştiği için artık geçersiz sayılan) eski
        oturuma hiçbir şey yazılmaz — self.live_summary, kalıcı depo,
        _live_summary_failing, live_summary_failure_episode, durum;
        hiçbiri.

        SQLite hedefi: kontrol GEÇERSE, `self.session_id` kilit ALTINDA
        `target_session_id` yerel değişkenine DONDURULUR ve kilit SERBEST
        BIRAKILDIKTAN SONRA o dondurulmuş değere yazılır; self.session_id
        SONRADAN değişse bile bu yazımı ETKİLEMEZ, bu yüzden kilidi SQLite
        yazımı boyunca tutmaya gerek yoktur — kilit yalnızca bellek-içi
        sahiplik doğrulama + atama için tutulur."""
        if not self._running:
            return
        if generation is None:
            generation = self._session_generation

        full_text = self.get_full_transcript()
        if not full_text.strip():
            return

        # Checkpoint A değişmezi: capture_failed iken arıza durumu, kullanıcı
        # Durdur'a basana dek görünür kalır; özeti duyurmak için ezilmez.
        if self._running and not self.capture_failed:
            self.status = "Canlı özet güncelleniyor..."

        try:
            new_summary = self.summarizer.summarize(full_text, is_final=False)
        except Exception:
            log.exception(
                "Canlı özet üretilemedi (önceki özet korunuyor, bir sonraki "
                "aralıkta yeniden denenecek)"
            )
            with self._session_generation_lock:
                if generation != self._session_generation:
                    log.info(
                        "Bayat canlı özet arızası yok sayıldı (oturum artık geçerli değil)."
                    )
                    return
                if not self._live_summary_failing:
                    self._live_summary_failing = True
                    self.live_summary_failure_episode += 1
                if self._running and not self.capture_failed:
                    self.status = "Dinleniyor"
            return

        with self._session_generation_lock:
            if generation != self._session_generation:
                log.info(
                    "Bayat canlı özet sonucu yayınlanmadan atlandı (oturum artık geçerli değil)."
                )
                return
            self.live_summary = new_summary
            target_session_id = self.session_id
            self._live_summary_failing = False
            if self._running and not self.capture_failed:
                self.status = "Dinleniyor"

        if target_session_id:
            try:
                self.repository.save_summary(
                    target_session_id, kind=SUMMARY_LIVE,
                    content=new_summary, model=self.summarizer.get_active_model(),
                )
            except Exception:
                log.exception("Canlı özet SQLite'a yazılamadı (arayüzde göstermeye devam ediliyor)")
        print(f"\n[Canlı Özet]\n{new_summary}\n")

    def _summarize_loop(self, my_generation: int) -> None:
        """LIVE_SUMMARY_INTERVAL_SEC saniyede bir _run_live_summary_once()
        çağırır. `my_generation`, start()'ın self._session_generation_lock
        altında ürettiği DEĞİŞMEZ bir değerdir (bkz. start()) — bu işçi
        HANGİ oturuma ait doğduysa onu taşır; thread argümanı olarak
        SABİTLENİR, yeniden okunmaz.

        Döngü koşulları, PAYLAŞILAN self._running yeniden True olsa BİLE
        (bir SONRAKİ oturum başladığı için), kendi jenerasyonu artık
        self._session_generation'a EŞİT DEĞİLSE yeni bir tur PLANLAMAZ ve
        çıkar — böylece 'yetim' bir işçi, kendi oturumu bittikten sonra
        sonsuza dek yeni oturumla birlikte dönmeye devam etmez. Bu
        döngü-kontrolü okumaları kilit GEREKTİRMEZ (ucuz, kendi kendini
        düzelten okumalar — asıl sahiplik garantisi
        _run_live_summary_once()'daki yayınlama sınırındadır). Hâlâ Ollama
        içinde bloke olan TEK bir çağrı varsa (iş parçacığı öldürülemez),
        bu çağrı serbest kaldığında sonucunu ATAR (_run_live_summary_once
        içinde) ve döngüye HİÇ dönmeden sonlanır."""
        try:
            while self._running and self._session_generation == my_generation:
                for _ in range(config.LIVE_SUMMARY_INTERVAL_SEC):
                    if not self._running or self._session_generation != my_generation:
                        return
                    time.sleep(1)
                self._run_live_summary_once(my_generation)
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
