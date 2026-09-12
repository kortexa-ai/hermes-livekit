"""Default CPU-only Silero VAD. Model/session is shared; stream state is not."""

from functools import lru_cache
import hashlib
from pathlib import Path

from .vad import AdaptiveRmsGate

SILERO_REVISION = "be95df9152c0d7618fa1edfeb296fc3dae32376f"
SILERO_SHA256 = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"
SILERO_MODEL_BYTES = 2_327_524
SILERO_LICENSE_SHA256 = "2e63e9a38b6e8fc0c7bc37ce174caca1862870856c6daf5697cfb785e925520b"
SILERO_LICENSE_URL = f"https://raw.githubusercontent.com/snakers4/silero-vad/{SILERO_REVISION}/LICENSE"
SILERO_MODEL_URL = (
    f"https://raw.githubusercontent.com/snakers4/silero-vad/{SILERO_REVISION}"
    "/src/silero_vad/data/silero_vad.onnx"
)


@lru_cache(maxsize=2)
def load_silero_model(path: str):
    """No download or GPU fallback on the startup/utterance path."""
    model = Path(path).expanduser()
    if model.stat().st_size != SILERO_MODEL_BYTES:
        raise ValueError("Silero model has an unexpected size; reinstall hermes-livekit or run tools/prepare_vad.py")
    content = model.read_bytes()
    if hashlib.sha256(content).hexdigest() != SILERO_SHA256:
        raise ValueError("Silero model checksum mismatch; reinstall hermes-livekit or run tools/prepare_vad.py")
    try:
        import onnxruntime as ort
    except ImportError as exc:
        raise ValueError("onnxruntime is missing; reinstall hermes-livekit") from exc
    options = ort.SessionOptions()
    options.inter_op_num_threads = options.intra_op_num_threads = 1
    return ort.InferenceSession(content, sess_options=options, providers=["CPUExecutionProvider"])


class SileroRmsGate(AdaptiveRmsGate):
    """Speech probabilities drive decisions; confirmed background updates RMS.

    Silero v6.2 uses 512 new samples plus 64 context samples at 16 kHz and
    recurrent state [2, 1, 128]. Keep the 48-kHz input unchanged for ASR.
    A stateful libav resampler avoids aliasing from simply dropping samples.
    """

    def __init__(self, session, threshold: float, **kwargs):
        import numpy as np
        from av import AudioResampler

        super().__init__(**kwargs)
        self._session, self._threshold = session, threshold
        self._resampler = AudioResampler(format="s16", layout="mono", rate=16000)
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, 64), dtype=np.float32)
        self._pending = np.empty(0, dtype=np.float32)
        self._probability = 0.0

    def is_speech(self, rms: float, *, speaking: bool, frame_seconds: float = 0.02,
                  pcm: bytes | None = None) -> bool:
        import numpy as np
        from av import AudioFrame

        if pcm is None or len(pcm) % 2:
            raise ValueError("Silero requires mono PCM16 frames")
        frame = AudioFrame(format="s16", layout="mono", samples=len(pcm) // 2)
        frame.sample_rate = 48000
        frame.planes[0].update(pcm)
        peak = None
        for converted in self._resampler.resample(frame):
            data = bytes(converted.planes[0])[:converted.samples * 2]
            samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
            pending = np.concatenate((self._pending, samples))
            offset = 0
            while len(pending) - offset >= 512:
                window = np.concatenate((self._context, pending[None, offset:offset + 512]), axis=1)
                output, self._state = self._session.run(None, {
                    "input": window, "state": self._state, "sr": np.array(16000, dtype=np.int64),
                })
                self._context = window[:, -64:].copy()
                self._probability = float(output[0, 0])
                peak = self._probability if peak is None else max(peak, self._probability)
                offset += 512
            self._pending = pending[offset:].copy()
        # Preserve short speech inside a conference poll batch; between model
        # windows, retain the last decision rather than inventing silent gaps.
        probability = self._probability if peak is None else peak
        threshold = max(0.05, self._threshold - 0.15) if speaking else self._threshold
        speech = probability >= threshold
        if not speech and self.noise_rms is not None:
            self._learn_noise(rms, frame_seconds)
        return speech
