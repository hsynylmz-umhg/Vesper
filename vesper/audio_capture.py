"""
Windows'ta sistem sesini (hoparlör çıkışını) WASAPI loopback ile yakalar.
webrtcvad ile konuşma/sessizlik ayrımı yapar, tamamlanan konuşma parçalarını
16kHz mono numpy float32 array olarak bir queue'ya koyar.

ÖNEMLİ: Bu dosya sadece Windows'ta çalışır (pyaudiowpatch Windows'a özgüdür)
ve bu proje bir Linux sandbox'ta hazırlandığı için WASAPI kısmı gerçek bir
Windows makinesinde TEST EDİLEMEDİ. Mantık pyaudiowpatch'in kendi
dokümantasyonundaki örnek kullanıma dayanıyor; ilk çalıştırmada bir cihaz
bulma hatası alırsan README'deki "Sorun Giderme" bölümüne bak.
"""

import logging
import queue
import threading
from math import gcd
from typing import Any, Dict, List, Optional

import numpy as np
import webrtcvad
from scipy.signal import resample_poly

from . import config

try:
    import pyaudiowpatch as pyaudio
except ImportError:
    pyaudio = None  # Linux/Mac'te sadece import hatası vermesin diye; Windows'ta gerçek paket kurulu olacak

log = logging.getLogger("audio_capture")
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )


class AudioCapture:
    def __init__(self, output_queue: "queue.Queue[np.ndarray]"):
        self.output_queue = output_queue
        self._running = False
        self._thread = None
        self.vad = webrtcvad.Vad(config.VAD_AGGRESSIVENESS)
        self._vad_frame_len = int(config.TARGET_SAMPLE_RATE * config.VAD_FRAME_MS / 1000)

        # v0.3.0 — basit cihaz seçimi. None = otomatik (varsayılan çıkışın
        # loopback karşılığı, AYNEN eski davranış). Belirli bir indeks
        # seçilirse _resolve_device() bunu doğrular/açar.
        self._device_index: Optional[int] = None
        # En son başarılı start() ön-kontrolünde çözülen cihazın adı — sadece
        # görüntüleme/kayıt (source_name) amaçlıdır, akış açmak için
        # KULLANILMAZ (bkz. _resolve_device ve start() docstring'leri).
        self._active_device_name: Optional[str] = None
        # v0.3.0 — _run() işçisinin, normal bir stop() DIŞINDA sonlandığını
        # gösteren KALICI hata. None = hiç hata yok (henüz başlamadı YA DA
        # temiz şekilde durduruldu). start() her çağrıldığında None'a
        # sıfırlanır (bkz. start() başı).
        self._error: Optional[Exception] = None

    @staticmethod
    def list_available_devices() -> List[Dict[str, Any]]:
        """Seçilebilir loopback kaynaklarını listeler. Her girdi ZATEN bir
        loopback pseudo-cihazıdır (get_loopback_device_info_generator'dan
        gelir) — ham/genel bir çıkış cihazı indeksi asla döndürülmez.
        pyaudiowpatch yoksa (Linux/Mac/sandbox) boş liste döner."""
        if pyaudio is None:
            return []
        p = pyaudio.PyAudio()
        try:
            return [
                {"index": d["index"], "name": d["name"]}
                for d in p.get_loopback_device_info_generator()
            ]
        finally:
            p.terminate()

    def set_device(self, device_index: Optional[int]) -> None:
        """Kullanılacak loopback cihazının indeksini kaydeder. None =
        otomatik/varsayılan (mevcut davranış). Burada DOĞRULAMA yapılmaz —
        doğrulama start()'ta, o anki gerçek PyAudio durumuna göre yapılır."""
        self._device_index = device_index

    def get_active_device_name(self) -> Optional[str]:
        """En son başarılı start() ön-kontrolünde çözülen cihazın adı.
        Sadece bir etikettir (ör. kalıcı kayıtta source_name için) —
        _run()'ın kendi PyAudio örneğinde AYRICA ve BAĞIMSIZ olarak
        çözdüğü cihazla aynı olması BEKLENİR ama garanti edilmez; bu yüzden
        ses akışını açmak için asla kullanılmaz."""
        return self._active_device_name

    def get_error(self) -> Optional[Exception]:
        """_run() işçisi normal bir stop() DIŞINDA sonlandıysa, o KALICI
        hatayı döner. None = hata yok. Bu asla None'dan başka bir şeye
        SIFIRLANMAZ (start() hariç) — bir kez set edildi mi, o oturum için
        kalıcıdır."""
        return self._error

    def start(self):
        if pyaudio is None:
            raise RuntimeError(
                "pyaudiowpatch bulunamadı. Bu modül sadece Windows'ta çalışır. "
                "Windows makinende 'pip install PyAudioWPatch' çalıştırdığından emin ol."
            )

        # Yeni bir oturum başlarken önceki oturumdan kalma bir hatayı
        # devralmamak için KALICI hatayı burada sıfırla.
        self._error = None

        # v0.3.0: seçili cihazı (veya otomatik/varsayılanı) arka plan
        # thread'i başlamadan ÖNCE, senkron olarak doğrula. Böylece "cihaz
        # artık yok" gibi hatalar sessizce arka plan thread'inde ölüp
        # gitmek yerine buradan, çağırana (WebinarAssistant.start() ->
        # ui.py'nin mevcut except bloğuna kadar) normal şekilde yükselir.
        # NOT: Bu sadece bir ÖN KONTROLdür — kendi PyAudio örneğini kullanır
        # ve onu hemen sonra kapatır. _run() akışı GERÇEKTEN açarken kendi
        # PyAudio örneğinde cihazı YENİDEN ve BAĞIMSIZ olarak çözer; bir
        # PyAudio örneğinden alınan cihaz sözlüğü başka bir örnekte akış
        # açmak için asla saklanıp yeniden kullanılmaz. Bu iki adım arasında
        # (ön kontrol başarılı olduktan hemen sonra, _run() kendi çözümünü
        # yapmadan önce) cihazın kaybolması gibi son derece dar bir yarış
        # penceresi teorik olarak açık kalır; bu durumda hata bugünkü mevcut
        # davranışla aynı şekilde _run() thread'inde sessizce oluşur — bu
        # kapsamda genel bir işçi-hata-iletim mekanizması KURULMAMIŞTIR.
        probe = pyaudio.PyAudio()
        try:
            resolved = self._resolve_device(probe)
            self._active_device_name = resolved.get("name")
        finally:
            probe.terminate()

        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> bool:
        """Var olan davranış AYNEN korunur (sinyal + 2 saniye join).
        Dönüş değeri: True = yakalama thread'i KESİN olarak durdu; artık
        output_queue'ya yeni bir şey yazamaz. False = 2 saniyede bitmedi,
        thread hâlâ canlı olabilir — çağıran üreticinin öldüğünü VARSAYAMAZ
        ve gerekiyorsa wait_until_stopped() ile beklemeye devam etmelidir."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
            return not self._thread.is_alive()
        return True

    def wait_until_stopped(self, timeout: float) -> bool:
        """stop() sonrası üreticinin kesin olarak öldüğünü doğrulamak için
        ek süre bekler. Yeni bir durdurma yolu YOKTUR — aynı thread'e
        tekrar join olur. True = artık kesinlikle canlı değil."""
        if self._thread:
            self._thread.join(timeout=timeout)
            return not self._thread.is_alive()
        return True

    def _resolve_device(self, p):
        """Seçili cihazı (self._device_index) ya da otomatik/varsayılanı,
        KENDİSİNE VERİLEN `p` PyAudio örneğine göre çözer. Her çağrı taze
        bir sorgu yapar; önceki bir örnekten alınmış bir cihaz sözlüğü asla
        yeniden kullanılmaz — start()'taki ön kontrol ve _run()'daki gerçek
        açılış, HER BİRİ KENDİ `p`'siyle bu fonksiyonu ayrı ayrı çağırır."""
        if self._device_index is None:
            return self._find_loopback_device(p)

        try:
            device = p.get_device_info_by_index(self._device_index)
        except Exception as e:
            raise RuntimeError(
                f"Seçili ses cihazı artık mevcut değil (indeks {self._device_index}). "
                "Cihaz listesini yenileyip tekrar seçin."
            ) from e

        if not device.get("isLoopbackDevice"):
            raise RuntimeError(
                f"Seçili cihaz ('{device.get('name', '?')}') bir loopback cihazı değil."
            )
        return device

    def _find_loopback_device(self, p):
        wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_output = p.get_device_info_by_index(wasapi_info["defaultOutputDevice"])

        if default_output.get("isLoopbackDevice"):
            return default_output

        for loopback in p.get_loopback_device_info_generator():
            if default_output["name"] in loopback["name"]:
                return loopback

        raise RuntimeError(
            "Loopback cihazı bulunamadı. Windows ses ayarlarından varsayılan çıkış "
            "cihazını kontrol et, ya da README'deki VB-Cable alternatifine bak."
        )

    def _run(self):
        # p/stream, _resolve_device()/p.open() başarısız olursa hiç var
        # olmayabilir — cleanup'ın güvenle çalışabilmesi için önceden None
        # olarak başlatılırlar; sadece GERÇEKTEN oluşturulanlar temizlenir.
        p = None
        stream = None
        speech_buffer = []

        try:
            p = pyaudio.PyAudio()
            # _resolve_device() burada, BU `p` örneğine göre BAĞIMSIZ olarak
            # yeniden çözülür — start()'taki ön kontrolün sonucu (bkz.
            # get_active_device_name) sadece bir etikettir, burada YENİDEN
            # KULLANILMAZ. Ön kontrolü geçmiş bir cihazın burada yine de
            # kaybolmuş olması (dar bir yarış penceresi) artık bu try
            # bloğu içinde yakalanıp self._error olarak saklı kalıyor.
            device = self._resolve_device(p)

            native_rate = int(device["defaultSampleRate"])
            channels = device["maxInputChannels"]
            frames_per_buffer = int(native_rate * config.VAD_FRAME_MS / 1000)

            stream = p.open(
                format=pyaudio.paFloat32,
                channels=channels,
                rate=native_rate,
                input=True,
                input_device_index=device["index"],
                frames_per_buffer=frames_per_buffer,
            )

            silence_ms = 0
            chunk_ms = 0

            while self._running:
                raw = stream.read(frames_per_buffer, exception_on_overflow=False)
                samples = np.frombuffer(raw, dtype=np.float32)

                if channels > 1:
                    samples = samples.reshape(-1, channels).mean(axis=1)

                frame_16k = self._resample(samples, native_rate, config.TARGET_SAMPLE_RATE)
                frame_16k = self._force_exact_length(frame_16k, self._vad_frame_len)

                is_speech = self._is_speech(frame_16k)

                if is_speech:
                    speech_buffer.append(frame_16k)
                    silence_ms = 0
                    chunk_ms += config.VAD_FRAME_MS
                elif speech_buffer:
                    # Kısa doğal duraklamaları da parçaya dahil et (cümle arası nefes vb.)
                    speech_buffer.append(frame_16k)
                    silence_ms += config.VAD_FRAME_MS
                    chunk_ms += config.VAD_FRAME_MS

                should_flush = speech_buffer and (
                    silence_ms >= config.SILENCE_TIMEOUT_MS
                    or chunk_ms >= config.MAX_CHUNK_SECONDS * 1000
                )

                if should_flush:
                    self.output_queue.put(np.concatenate(speech_buffer))
                    speech_buffer = []
                    silence_ms = 0
                    chunk_ms = 0
        except Exception as e:
            # Buraya düşen HER ŞEY beklenmedik bir hatadır. Normal bir stop()
            # BURAYA ASLA bir istisna fırlatarak ulaşmaz: stop() sadece
            # self._running'i False yapar, bu da bir SONRAKİ "while
            # self._running" kontrolünde döngüyü sessizce/temiz şekilde
            # sonlandırır — hiçbir satır bu yüzden istisna fırlatmaz. Bu
            # yüzden burada normal/anormal ayrımı yapmaya GEREK yok: bu
            # except bloğuna düşmüş olmak zaten anormal olduğunun kanıtıdır.
            # (_resolve_device/p.open() başarısızlıkları dahil.)
            self._error = e
            log.exception(
                "Ses yakalama işleyicisi beklenmedik şekilde durdu (cihaz kaybı, "
                "host hatası veya çözümleme/açma başarısızlığı olabilir)"
            )
        finally:
            # Durdurma anında sessizlik eşiğine ulaşmamış (henüz "flush"
            # edilmemiş) bir konuşma parçası varsa, onu atmak yerine kuyruğa
            # koy. Aksi halde kullanıcı tam cümlenin ortasında Durdur'a
            # basarsa ya da cihaz aniden kaybolursa o son parça Whisper'a hiç
            # ulaşmadan kaybolurdu.
            if speech_buffer:
                self.output_queue.put(np.concatenate(speech_buffer))
            self._safe_cleanup(stream, p)

    def _safe_cleanup(self, stream, p) -> None:
        """`stream`/`p` kısmen ya da hiç oluşturulmamış olabilir (ör.
        _resolve_device()/p.open() başarısız olduysa) — bu yüzden None
        kontrolü ZORUNLU. Art arda gelen kapatma hataları birbirini ya da
        self._error'ı (ana yakalama hatasını) ASLA maskelemez: her adım
        kendi except'inde ayrı ayrı loglanır, diğerleri yine de denenir.
        Sessiz except: pass YOKTUR."""
        if stream is not None:
            try:
                stream.stop_stream()
            except Exception:
                log.exception("stream.stop_stream() başarısız (muhtemelen zaten geçersiz)")
            try:
                stream.close()
            except Exception:
                log.exception("stream.close() başarısız")
        if p is not None:
            try:
                p.terminate()
            except Exception:
                log.exception("PyAudio örneği sonlandırılamadı")

    @staticmethod
    def _resample(samples: np.ndarray, orig_rate: int, target_rate: int) -> np.ndarray:
        if orig_rate == target_rate:
            return samples.astype(np.float32)
        g = gcd(orig_rate, target_rate)
        up, down = target_rate // g, orig_rate // g
        return resample_poly(samples, up, down).astype(np.float32)

    @staticmethod
    def _force_exact_length(samples: np.ndarray, length: int) -> np.ndarray:
        # webrtcvad tam olarak 10/20/30ms'lik frame bekliyor; resample sonrası
        # yuvarlama farkları frame'i 1-2 örnek kaydırabilir, bu da hataya sebep olur.
        if len(samples) == length:
            return samples
        if len(samples) > length:
            return samples[:length]
        return np.pad(samples, (0, length - len(samples)))

    def _is_speech(self, frame_16k: np.ndarray) -> bool:
        pcm16 = (np.clip(frame_16k, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
        try:
            return self.vad.is_speech(pcm16, config.TARGET_SAMPLE_RATE)
        except Exception:
            return False
