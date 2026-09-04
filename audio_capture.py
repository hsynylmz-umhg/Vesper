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

import queue
import threading
from math import gcd

import numpy as np
import webrtcvad
from scipy.signal import resample_poly

import config

try:
    import pyaudiowpatch as pyaudio
except ImportError:
    pyaudio = None  # Linux/Mac'te sadece import hatası vermesin diye; Windows'ta gerçek paket kurulu olacak


class AudioCapture:
    def __init__(self, output_queue: "queue.Queue[np.ndarray]"):
        self.output_queue = output_queue
        self._running = False
        self._thread = None
        self.vad = webrtcvad.Vad(config.VAD_AGGRESSIVENESS)
        self._vad_frame_len = int(config.TARGET_SAMPLE_RATE * config.VAD_FRAME_MS / 1000)

    def start(self):
        if pyaudio is None:
            raise RuntimeError(
                "pyaudiowpatch bulunamadı. Bu modül sadece Windows'ta çalışır. "
                "Windows makinende 'pip install PyAudioWPatch' çalıştırdığından emin ol."
            )
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)

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
        p = pyaudio.PyAudio()
        device = self._find_loopback_device(p)

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

        speech_buffer = []
        silence_ms = 0
        chunk_ms = 0

        try:
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
        finally:
            stream.stop_stream()
            stream.close()
            p.terminate()

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
