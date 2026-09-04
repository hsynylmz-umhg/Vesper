"""
Hızlı tanı testi: WASAPI loopback cihazı doğru bulunuyor mu, ses gerçekten
yakalanıyor mu? Whisper/Ollama'ya gerek yok, sadece bunu izole test eder.

Kullanım: Windows'ta bir şey çalarken (müzik/video) bunu çalıştır:
    python test_audio.py
"""

import time

import numpy as np

try:
    import pyaudiowpatch as pyaudio
except ImportError:
    print("HATA: pyaudiowpatch kurulu değil. Önce şunu çalıştır: pip install PyAudioWPatch")
    raise SystemExit(1)

p = pyaudio.PyAudio()
wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
default_output = p.get_device_info_by_index(wasapi_info["defaultOutputDevice"])

device = default_output
if not device.get("isLoopbackDevice"):
    for loopback in p.get_loopback_device_info_generator():
        if default_output["name"] in loopback["name"]:
            device = loopback
            break

print(f"Bulunan cihaz: {device['name']}")
print(f"Örnekleme hızı: {device['defaultSampleRate']} Hz, Kanal sayısı: {device['maxInputChannels']}")

rate = int(device["defaultSampleRate"])
channels = device["maxInputChannels"]
frames_per_buffer = int(rate * 0.5)  # 0.5 saniyelik parçalar

stream = p.open(
    format=pyaudio.paFloat32,
    channels=channels,
    rate=rate,
    input=True,
    input_device_index=device["index"],
    frames_per_buffer=frames_per_buffer,
)

print("\n5 saniye dinleniyor — bir şey çal (müzik/video)...\n")
for i in range(10):
    raw = stream.read(frames_per_buffer, exception_on_overflow=False)
    samples = np.frombuffer(raw, dtype=np.float32)
    volume = float(np.abs(samples).mean())
    bar = "#" * min(int(volume * 200), 60)
    print(f"{i * 0.5:4.1f}s  {bar}")
    time.sleep(0.5)

stream.stop_stream()
stream.close()
p.terminate()

print("\nSes çalarken yukarıda '#' çubukları göründüyse, sistem sesi doğru yakalanıyor demektir.")
print("Hiçbir şey görünmediyse: Windows Ses Ayarları > Çıkış cihazını kontrol et,")
print("ya da README.md'deki 'Sorun Giderme' bölümüne bak.")
