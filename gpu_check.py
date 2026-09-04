import ctypes, os, sys, time

def step(msg):
    print(f"\n[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

step("1) Ortam")
cuda_path = os.environ.get("CUDA_PATH")
print("   CUDA_PATH =", cuda_path)
if sys.platform == "win32" and cuda_path:
    os.add_dll_directory(os.path.join(cuda_path, "bin"))

for name in ("cublas64_12.dll", "cublasLt64_12.dll", "cudnn64_9.dll"):
    copies = [os.path.join(d, name) for d in os.environ["PATH"].split(os.pathsep)
              if d and os.path.isfile(os.path.join(d, name))]
    print(f"   {name}: {len(copies)} kopya -> {copies}")

step("2) DLL'leri tek tek yükle (hata varsa burada net mesaj alırsın)")
for name in ("cublas64_12.dll", "cublasLt64_12.dll", "cudnn64_9.dll"):
    ctypes.WinDLL(name); print("   OK", name)

step("3) ctranslate2 import")
import ctranslate2
print("   ctranslate2", ctranslate2.__version__)

step("4) CUDA cihaz sayısı (burada donarsa sorun sürücü seviyesinde)")
print("   cihaz:", ctranslate2.get_cuda_device_count())

step("5) Desteklenen compute type'lar")
print("  ", sorted(ctranslate2.get_supported_compute_types("cuda", 0)))

step("6) Küçük modelle gerçek GPU çıkarımı (1-2 dk sürebilir)")
from faster_whisper import WhisperModel
import numpy as np
m = WhisperModel("tiny", device="cuda", compute_type="float16")
segs, _ = m.transcribe(np.zeros(16000, dtype=np.float32), language="tr", beam_size=1)
list(segs)
print("   GPU çıkarımı OK")

step("7) Hedef model + hedef compute type")
m = WhisperModel("deepdml/faster-whisper-large-v3-turbo-ct2", device="cuda", compute_type="float16")
segs, _ = m.transcribe(np.zeros(16000, dtype=np.float32), language="tr", beam_size=5)
list(segs)
print("   large-v3-turbo GPU OK — artık ui.py'yi çalıştırabilirsin")