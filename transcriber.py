"""
faster-whisper (CTranslate2) ile NVIDIA GPU üzerinde konuşma -> yazı çevirimi.
"""

import importlib.util
import logging
import os
import sys
import threading
import time
import traceback
from typing import List, Optional, Set, Tuple

import numpy as np

import config

log = logging.getLogger("transcriber")
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

SAMPLE_RATE = getattr(config, "TARGET_SAMPLE_RATE", 16000)
DEVICE = getattr(config, "WHISPER_DEVICE", "cuda")
COMPUTE_TYPE = getattr(config, "WHISPER_COMPUTE_TYPE", "float16")
GPU_INDEX = getattr(config, "WHISPER_GPU_INDEX", 0)
GPU_INIT_TIMEOUT = getattr(config, "WHISPER_GPU_INIT_TIMEOUT_SEC", 180)
ALLOW_CPU_FALLBACK = getattr(config, "WHISPER_ALLOW_CPU_FALLBACK", True)
BEAM_SIZE = getattr(config, "WHISPER_BEAM_SIZE", 5)
EXTRA_DLL_DIRS = list(getattr(config, "WHISPER_EXTRA_DLL_DIRS", []))
MIN_CHUNK_SEC = getattr(config, "WHISPER_MIN_CHUNK_SEC", 0.3)
MAX_CUDA_RUNTIME_ERRORS = getattr(config, "WHISPER_MAX_CUDA_RUNTIME_ERRORS", 2)

GPU_COMPUTE_FALLBACK_ORDER = ["float16", "int8_float16", "int8_bfloat16", "bfloat16", "int8", "float32"]
CUDA_DLLS = ("cublas64_12.dll", "cublasLt64_12.dll", "cudnn64_9.dll")

def _register_dll_dirs() -> List[str]:
    if sys.platform != "win32":
        return []
    candidates: List[str] = []
    cuda_path = os.environ.get("CUDA_PATH")
    if cuda_path:
        candidates.append(os.path.join(cuda_path, "bin"))
    candidates.extend(EXTRA_DLL_DIRS)

    registered = []
    for d in candidates:
        if d and os.path.isdir(d):
            try:
                os.add_dll_directory(d)
                registered.append(d)
            except OSError as e:
                log.warning("DLL klasörü eklenemedi (%s): %s", d, e)
    return registered

def _dll_copies_in_path(name: str) -> List[str]:
    hits = []
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        p = os.path.join(d, name)
        if os.path.isfile(p):
            hits.append(p)
    return hits

def _preflight_diagnostics(registered_dirs: List[str]) -> None:
    if sys.platform != "win32" or DEVICE != "cuda":
        return

    log.info("CUDA_PATH = %s", os.environ.get("CUDA_PATH") or "(tanımlı değil!)")
    for mod in ("nvidia.cublas", "nvidia.cudnn"):
        if importlib.util.find_spec(mod) is not None:
            log.warning("pip paketi '%s' hâlâ kurulu. Çakışma yaratabilir.", mod)

    for name in CUDA_DLLS:
        copies = _dll_copies_in_path(name)
        if len(copies) > 1:
            log.warning("%s için PATH'te %d kopya var: %s", name, len(copies), copies)

_REGISTERED_DIRS = _register_dll_dirs()
_preflight_diagnostics(_REGISTERED_DIRS)

import ctranslate2  # noqa: E402
from faster_whisper import WhisperModel, download_model  # noqa: E402

def _run_with_timeout(fn, timeout: float, label: str) -> Tuple[Optional[object], Optional[BaseException]]:
    box = {}
    def worker():
        try:
            box["value"] = fn()
        except BaseException as e:
            box["error"] = e
            box["tb"] = traceback.format_exc()
    t = threading.Thread(target=worker, name=label, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None, TimeoutError(f"{label} {timeout:.0f} sn içinde tamamlanmadı (donma)")
    if "error" in box:
        return None, box["error"]
    return box.get("value"), None

def _pick_gpu_compute_type(requested: str, supported: Set[str]) -> str:
    if requested in supported:
        return requested
    for ct in GPU_COMPUTE_FALLBACK_ORDER:
        if ct in supported:
            return ct
    raise RuntimeError(f"GPU için uygun compute_type yok. Desteklenenler: {sorted(supported)}")

def _is_cuda_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in ("cuda", "cublas", "cudnn", "device-side", "out of memory"))

class Transcriber:
    def __init__(self):
        self._lock = threading.Lock()
        self.model: Optional[WhisperModel] = None
        self.device: str = "?"
        self.compute_type: str = "?"
        self.status_note: str = ""
        self._cuda_error_count = 0
        self._zombie_models = []
        self._model_path = self._ensure_model_available()

        if DEVICE == "cuda":
            if not self._try_init_gpu():
                if not ALLOW_CPU_FALLBACK:
                    raise RuntimeError("GPU başlatılamadı.")
                self._init_cpu()
        else:
            self._init_cpu()

    def _ensure_model_available(self) -> str:
        name = config.WHISPER_MODEL
        if os.path.isdir(name):
            return name
        log.info("Model indiriliyor: %s", name)
        return download_model(name)

    def _gpu_loader(self):
        n = ctranslate2.get_cuda_device_count()
        if n == 0:
            raise RuntimeError("CTranslate2 CUDA cihazı görmüyor.")
        supported = set(ctranslate2.get_supported_compute_types("cuda", GPU_INDEX))
        compute = _pick_gpu_compute_type(COMPUTE_TYPE, supported)
        model = WhisperModel(self._model_path, device="cuda", device_index=GPU_INDEX, compute_type=compute, num_workers=1)
        self._warm_up(model)
        return model, compute

    def _try_init_gpu(self) -> bool:
        result, err = _run_with_timeout(self._gpu_loader, GPU_INIT_TIMEOUT, "whisper-gpu-init")
        if err is None:
            self.model, self.compute_type = result
            self.device = f"cuda:{GPU_INDEX}"
            self.status_note = "GPU"
            return True
        self.status_note = f"GPU başarısız: {type(err).__name__}"
        return False

    def _init_cpu(self):
        threads = max(2, (os.cpu_count() or 4) - 2)
        self.model = WhisperModel(self._model_path, device="cpu", compute_type="int8", cpu_threads=threads, num_workers=1)
        self._warm_up(self.model)
        self.device = "cpu"
        self.compute_type = "int8"
        self.status_note = (self.status_note + " -> CPU") if self.status_note else "CPU"

    def _warm_up(self, model: WhisperModel):
        dummy = np.zeros(SAMPLE_RATE, dtype=np.float32)
        segments, _ = model.transcribe(dummy, language=config.WHISPER_LANGUAGE, beam_size=1, without_timestamps=True)
        list(segments)

    def transcribe(self, audio_16k: np.ndarray) -> str:
        if audio_16k is None or audio_16k.size == 0:
            return ""
        audio = np.ascontiguousarray(audio_16k, dtype=np.float32)
        if audio.size < int(MIN_CHUNK_SEC * SAMPLE_RATE):
            return ""

        try:
            with self._lock:
                return self._transcribe_unlocked(audio)
        except Exception as e:
            if self.device.startswith("cuda") and _is_cuda_error(e):
                self._cuda_error_count += 1
                if self._cuda_error_count >= MAX_CUDA_RUNTIME_ERRORS and ALLOW_CPU_FALLBACK:
                    self._fallback_to_cpu_after_runtime_error()
                    try:
                        with self._lock:
                            return self._transcribe_unlocked(audio)
                    except Exception:
                        pass
            return ""

    def _transcribe_unlocked(self, audio: np.ndarray) -> str:
        segments, _info = self.model.transcribe(
            audio, language=config.WHISPER_LANGUAGE, task="transcribe", beam_size=BEAM_SIZE,
            condition_on_previous_text=False, vad_filter=False, without_timestamps=True,
            temperature=[0.0, 0.2, 0.4], compression_ratio_threshold=2.4, log_prob_threshold=-1.0, no_speech_threshold=0.6
        )
        return " ".join(t for seg in segments if (t := seg.text.strip()))

    def _fallback_to_cpu_after_runtime_error(self):
        with self._lock:
            self._zombie_models.append(self.model)
            self.model = None
            self._init_cpu()

    @property
    def device_label(self) -> str:
        return f"{self.device} / {self.compute_type}" + (f" ({self.status_note})" if self.status_note else "")