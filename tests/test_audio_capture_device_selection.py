"""
AudioCapture'ın v0.3.0 cihaz keşfi/seçimi yüzeyini, gerçek pyaudiowpatch/WASAPI
donanımı olmadan, sahte (mock) bir `pyaudio` modülüyle test eder.

CLAUDE.md §9: gerçek WASAPI donanım yolu (asıl akış açma, VAD, resample) bu
sandbox'ta çalıştırılamaz ve burada test EDİLMEZ. Bu dosya sadece
_resolve_device()/start()'ın SENKRON ön-kontrol/doğrulama mantığını hedefler —
threading.Thread sahteyle değiştirilir, _run()'ın kendisi hiç çalıştırılmaz.

Çalıştırma:
    python -m unittest discover -s tests -t .
"""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# webrtcvad bu sandbox'ta gerçekten kurulu değil. AudioCapture.__init__
# Vad(...) çağırdığı için ÇALIŞAN bir sahte gerekiyor — test_lifecycle_
# integration.py'nin boş stub'ı yetmez, çünkü o dosya gerçek AudioCapture'ı
# hiç örneklemiyor (hep FakeAudioCapture kullanıyor). Hangi test dosyasının
# önce yüklendiğine bağlı kalmamak için: modül zaten varsa ama Vad
# taşımıyorsa da üzerine yaz.
if "webrtcvad" not in sys.modules or not hasattr(sys.modules["webrtcvad"], "Vad"):
    _fake_webrtcvad = types.ModuleType("webrtcvad")
    _fake_webrtcvad.Vad = lambda *a, **k: MagicMock()
    sys.modules["webrtcvad"] = _fake_webrtcvad

from vesper.audio_capture import AudioCapture


def _make_mock_pyaudio(default_output_index=3):
    """WASAPI + loopback numaralandırmasını taklit eden bir PyAudio() sahtesi
    kurar. Gerçek pyaudiowpatch'teki gibi: varsayılan çıkış cihazının kendisi
    loopback DEĞİLDİR; ayrı bir loopback pseudo-cihaz listesi vardır ve
    _find_loopback_device bunları isim alt-dizesiyle eşleştirir."""
    devices = {
        3: {"index": 3, "name": "Hoparlörler (Realtek(R) Audio)", "isLoopbackDevice": False},
        7: {
            "index": 7, "name": "Hoparlörler (Realtek(R) Audio) [Loopback]",
            "isLoopbackDevice": True, "defaultSampleRate": 48000.0, "maxInputChannels": 2,
        },
        9: {
            "index": 9, "name": "USB Kulaklık [Loopback]",
            "isLoopbackDevice": True, "defaultSampleRate": 44100.0, "maxInputChannels": 2,
        },
    }

    def get_device_info_by_index(index):
        if index not in devices:
            raise OSError(f"Invalid device index {index}")
        return devices[index]

    mock_pyaudio_module = MagicMock()
    mock_instance = MagicMock()
    mock_instance.get_host_api_info_by_type.return_value = {"defaultOutputDevice": default_output_index}
    mock_instance.get_device_info_by_index.side_effect = get_device_info_by_index
    mock_instance.get_loopback_device_info_generator.return_value = [devices[7], devices[9]]
    mock_pyaudio_module.PyAudio.return_value = mock_instance
    mock_pyaudio_module.paWASAPI = "WASAPI"
    return mock_pyaudio_module, mock_instance, devices


class TestListAvailableDevices(unittest.TestCase):
    def test_returns_empty_list_when_pyaudio_unavailable(self):
        # Bu sandbox'ta pyaudiowpatch zaten gerçekten kurulu değil (pyaudio
        # modül-seviyesinde None) — bu, mock'suz gerçek koşulu doğrudan
        # test eder.
        with patch("vesper.audio_capture.pyaudio", None):
            self.assertEqual(AudioCapture.list_available_devices(), [])

    def test_returns_only_loopback_devices(self):
        mock_module, _instance, _devices = _make_mock_pyaudio()
        with patch("vesper.audio_capture.pyaudio", mock_module):
            result = AudioCapture.list_available_devices()
        self.assertEqual(
            result,
            [
                {"index": 7, "name": "Hoparlörler (Realtek(R) Audio) [Loopback]"},
                {"index": 9, "name": "USB Kulaklık [Loopback]"},
            ],
        )

    def test_terminates_probe_instance(self):
        mock_module, instance, _devices = _make_mock_pyaudio()
        with patch("vesper.audio_capture.pyaudio", mock_module):
            AudioCapture.list_available_devices()
        instance.terminate.assert_called_once()


class TestSetDevice(unittest.TestCase):
    def test_set_device_stores_index_without_validating(self):
        capture = AudioCapture(output_queue=MagicMock())
        capture.set_device(42)
        self.assertEqual(capture._device_index, 42)
        self.assertIsNone(capture.get_active_device_name(), "start() çağrılmadan doğrulama/etiket olmamalı")


class TestResolveDeviceAndStartPreflight(unittest.TestCase):
    """_run() hiç çalıştırılmaz (threading.Thread sahte) — sadece start()'ın
    senkron ön kontrol/doğrulama kısmı hedeflenir. Bu, VAD/resample/chunking/
    queue-drain davranışına HİÇ dokunmadan, sadece cihaz çözümleme mantığını
    izole eder."""

    def setUp(self):
        self.mock_module, self.mock_instance, self.devices = _make_mock_pyaudio()
        self.pyaudio_patcher = patch("vesper.audio_capture.pyaudio", self.mock_module)
        self.pyaudio_patcher.start()
        self.thread_patcher = patch("vesper.audio_capture.threading.Thread")
        self.mock_thread_cls = self.thread_patcher.start()

    def tearDown(self):
        self.pyaudio_patcher.stop()
        self.thread_patcher.stop()

    def test_default_none_resolves_via_existing_default_lookup(self):
        capture = AudioCapture(output_queue=MagicMock())
        capture.start()
        self.assertEqual(
            capture.get_active_device_name(),
            "Hoparlörler (Realtek(R) Audio) [Loopback]",
        )
        self.assertTrue(capture._running)
        self.mock_thread_cls.assert_called_once()

    def test_explicit_valid_loopback_index_resolves_directly(self):
        capture = AudioCapture(output_queue=MagicMock())
        capture.set_device(9)
        capture.start()
        self.assertEqual(capture.get_active_device_name(), "USB Kulaklık [Loopback]")
        self.mock_thread_cls.assert_called_once()

    def test_explicit_index_no_longer_present_raises_synchronously(self):
        capture = AudioCapture(output_queue=MagicMock())
        capture.set_device(999)
        with self.assertRaises(RuntimeError):
            capture.start()
        self.assertFalse(capture._running, "başarısız ön kontrol sonrası _running True olmamalı")
        self.mock_thread_cls.assert_not_called()

    def test_explicit_index_that_is_not_loopback_is_rejected(self):
        capture = AudioCapture(output_queue=MagicMock())
        capture.set_device(3)  # gerçek çıkış cihazı — loopback DEĞİL
        with self.assertRaises(RuntimeError):
            capture.start()
        self.mock_thread_cls.assert_not_called()

    def test_probe_instance_is_terminated_on_success(self):
        capture = AudioCapture(output_queue=MagicMock())
        capture.start()
        self.mock_instance.terminate.assert_called_once()

    def test_probe_instance_is_terminated_on_failure(self):
        capture = AudioCapture(output_queue=MagicMock())
        capture.set_device(999)
        with self.assertRaises(RuntimeError):
            capture.start()
        self.mock_instance.terminate.assert_called_once()

    def test_pyaudio_none_raises_before_any_probe(self):
        with patch("vesper.audio_capture.pyaudio", None):
            capture = AudioCapture(output_queue=MagicMock())
            with self.assertRaises(RuntimeError):
                capture.start()
            self.mock_thread_cls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
