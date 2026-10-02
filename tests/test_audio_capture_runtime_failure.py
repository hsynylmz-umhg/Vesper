"""
AudioCapture'ın v0.3.0 çalışma-zamanı (runtime) yakalama arızası yönetimini
gerçek pyaudiowpatch/WASAPI donanımı olmadan, sahte (mock) bir `pyaudio` ve
`stream` nesnesiyle test eder.

CLAUDE.md §9: gerçek WASAPI donanım yolu burada test EDİLMEZ. Bu dosya
_run()'ın hata YAKALAMA/RETANSİYON/temizlik mantığını hedefler — VAD/
resample/chunking iç mekaniğine bağımlı olmamak için, gerçek bir loop
iterasyonu gerektiren testlerde `_is_speech` sahteyle değiştirilir.

_run() doğrudan çağrılır (threading.Thread ÜZERİNDEN DEĞİL) — tamamen
senkron ve deterministik olması için. Bu, gerçek koddaki hiçbir şeyi
DEĞİŞTİRMEZ: _run() sıradan bir metottur, hangi thread'den çağrıldığı onun
davranışını etkilemez.

Çalıştırma:
    python -m unittest discover -s tests -t .
"""

import os
import queue
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

if "webrtcvad" not in sys.modules or not hasattr(sys.modules["webrtcvad"], "Vad"):
    _fake_webrtcvad = types.ModuleType("webrtcvad")
    _fake_webrtcvad.Vad = lambda *a, **k: MagicMock()
    sys.modules["webrtcvad"] = _fake_webrtcvad

from vesper.audio_capture import AudioCapture


def _make_mock_pyaudio_with_stream(default_output_index=3, read_side_effect=None,
                                    stop_stream_side_effect=None, open_side_effect=None):
    """WASAPI + loopback numaralandırmasını VE bir akış (stream) nesnesini
    taklit eder. `read_side_effect`/`stop_stream_side_effect`/
    `open_side_effect` verilen bir istisna örneğini o çağrıda fırlatır."""
    devices = {
        3: {"index": 3, "name": "Hoparlörler (Realtek(R) Audio)", "isLoopbackDevice": False},
        7: {
            "index": 7, "name": "Hoparlörler (Realtek(R) Audio) [Loopback]",
            "isLoopbackDevice": True, "defaultSampleRate": 48000.0, "maxInputChannels": 2,
        },
    }

    def get_device_info_by_index(index):
        if index not in devices:
            raise OSError(f"Invalid device index {index}")
        return devices[index]

    mock_stream = MagicMock()
    if read_side_effect is not None:
        mock_stream.read.side_effect = read_side_effect
    else:
        mock_stream.read.return_value = b"\x00" * 1000
    if stop_stream_side_effect is not None:
        mock_stream.stop_stream.side_effect = stop_stream_side_effect

    mock_instance = MagicMock()
    mock_instance.get_host_api_info_by_type.return_value = {"defaultOutputDevice": default_output_index}
    mock_instance.get_device_info_by_index.side_effect = get_device_info_by_index
    mock_instance.get_loopback_device_info_generator.return_value = [devices[7]]
    if open_side_effect is not None:
        mock_instance.open.side_effect = open_side_effect
    else:
        mock_instance.open.return_value = mock_stream

    mock_pyaudio_module = MagicMock()
    mock_pyaudio_module.PyAudio.return_value = mock_instance
    mock_pyaudio_module.paWASAPI = "WASAPI"
    mock_pyaudio_module.paFloat32 = "float32"
    return mock_pyaudio_module, mock_instance, mock_stream, devices


class TestRuntimeCaptureFailure(unittest.TestCase):
    def test_stream_read_failure_is_retained_as_terminal_error(self):
        mock_module, instance, mock_stream, _devices = _make_mock_pyaudio_with_stream(
            read_side_effect=OSError("[Errno -9999] Unanticipated host error")
        )
        with patch("vesper.audio_capture.pyaudio", mock_module):
            capture = AudioCapture(output_queue=queue.Queue())
            capture._running = True
            capture._run()

        err = capture.get_error()
        self.assertIsInstance(err, OSError)
        self.assertIn("Unanticipated host error", str(err))

    def test_cleanup_failure_does_not_replace_primary_error(self):
        """Rapor edilen gerçek arıza: stream.read() birincil hatayı verir,
        finally'deki stream.stop_stream() İKİNCİL bir hata fırlatır — ikincisi
        birincisini MASKELEMEMELİDİR."""
        mock_module, instance, mock_stream, _devices = _make_mock_pyaudio_with_stream(
            read_side_effect=OSError("primary: [Errno -9999] Unanticipated host error"),
            stop_stream_side_effect=OSError("secondary: Stream not open"),
        )
        with patch("vesper.audio_capture.pyaudio", mock_module):
            capture = AudioCapture(output_queue=queue.Queue())
            capture._running = True
            capture._run()  # bu satırın kendisi istisna fırlatmamalı

        err = capture.get_error()
        self.assertIsInstance(err, OSError)
        self.assertIn("primary", str(err))
        self.assertNotIn("secondary", str(err))

    def test_cleanup_still_attempts_close_and_terminate_after_stop_stream_failure(self):
        mock_module, instance, mock_stream, _devices = _make_mock_pyaudio_with_stream(
            read_side_effect=OSError("boom"),
            stop_stream_side_effect=OSError("Stream not open"),
        )
        with patch("vesper.audio_capture.pyaudio", mock_module):
            capture = AudioCapture(output_queue=queue.Queue())
            capture._running = True
            capture._run()

        mock_stream.stop_stream.assert_called_once()
        mock_stream.close.assert_called_once()
        instance.terminate.assert_called_once()

    def test_resolve_device_worker_side_failure_is_retained(self):
        """Daha önce belgelenen dar yarış: ön kontrolü geçmiş bir cihaz,
        _run()'ın KENDİ çözümlemesi sırasında kaybolmuş olabilir."""
        mock_module, instance, mock_stream, _devices = _make_mock_pyaudio_with_stream()
        with patch("vesper.audio_capture.pyaudio", mock_module):
            capture = AudioCapture(output_queue=queue.Queue())
            capture.set_device(999)  # var olmayan indeks
            capture._running = True
            capture._run()

        self.assertIsInstance(capture.get_error(), RuntimeError)
        instance.open.assert_not_called()
        # stream hiç açılmadı ama p (PyAudio örneği) BAŞARIYLA oluşturuldu —
        # yine de terminate edilmeli.
        instance.terminate.assert_called_once()

    def test_p_open_failure_is_retained_as_terminal_error(self):
        mock_module, instance, mock_stream, _devices = _make_mock_pyaudio_with_stream(
            open_side_effect=OSError("device busy")
        )
        with patch("vesper.audio_capture.pyaudio", mock_module):
            capture = AudioCapture(output_queue=queue.Queue())
            capture._running = True
            capture._run()

        self.assertIsInstance(capture.get_error(), OSError)
        mock_stream.stop_stream.assert_not_called()  # stream hiç oluşmadı
        instance.terminate.assert_called_once()      # ama p yine de terminate edilmeli

    def test_pyaudio_construction_failure_is_retained_without_raising_from_cleanup(self):
        """En kısmi durum: pyaudio.PyAudio() bile başarısız olursa (p=None,
        stream=None) cleanup güvenle hiçbir şey yapmamalı — AttributeError
        FIRLATMAMALI."""
        mock_module = MagicMock()
        mock_module.PyAudio.side_effect = OSError("no audio subsystem")
        with patch("vesper.audio_capture.pyaudio", mock_module):
            capture = AudioCapture(output_queue=queue.Queue())
            capture._running = True
            capture._run()  # bu satırın kendisi İSTİSNA FIRLATMAMALI

        self.assertIsInstance(capture.get_error(), OSError)

    def test_normal_requested_stop_does_not_create_terminal_error(self):
        """Normal bir stop() (ikinci okumadan sonra _running=False) HİÇBİR
        zaman self._error'ı doldurmamalı — sadece anormal çıkışlar doldurur."""
        call_count = {"n": 0}
        capture_holder = {}

        def read_effect(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                capture_holder["capture"]._running = False
            return b"\x00" * 1000

        mock_module, instance, mock_stream, _devices = _make_mock_pyaudio_with_stream(
            read_side_effect=read_effect
        )
        with patch("vesper.audio_capture.pyaudio", mock_module):
            capture = AudioCapture(output_queue=queue.Queue())
            capture_holder["capture"] = capture
            capture._running = True
            with patch.object(capture, "_is_speech", return_value=False):
                capture._run()

        self.assertIsNone(capture.get_error())
        mock_stream.stop_stream.assert_called_once()
        mock_stream.close.assert_called_once()
        instance.terminate.assert_called_once()

    def test_error_is_reset_on_next_start_preflight(self):
        """Bir önceki oturumdan kalma bir hata, yeni bir start() çağrısına
        SIZMAMALI (start() preflight'ı get_error()'ı None'a sıfırlar)."""
        mock_module, instance, mock_stream, _devices = _make_mock_pyaudio_with_stream()
        capture = AudioCapture(output_queue=queue.Queue())
        capture._error = RuntimeError("stale error from a previous session")

        with patch("vesper.audio_capture.pyaudio", mock_module), \
             patch("vesper.audio_capture.threading.Thread"):
            capture.start()

        self.assertIsNone(capture.get_error())


if __name__ == "__main__":
    unittest.main()
