"""Transport-neutral adaptive energy gate for voice activity detection."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial
import logging
import math
from pathlib import Path

from .media import pcm_rms

logger = logging.getLogger("gateway.platforms.livekit.vad")
# The pinned Silero release ships with the package; see speech_detector.py.
DEFAULT_SILERO_MODEL = Path(__file__).with_name("models") / "silero_vad_v6.2.onnx"


# Permit calibration in louder rooms, but bound a bad calibration (e.g. a
# clipped microphone) so it cannot permanently make all speech inaudible.
VAD_NOISE_CEILING = 2000.0
VAD_MIN_START_THRESHOLD = 300.0
VAD_MIN_STOP_THRESHOLD = 220.0
VAD_START_RATIO = 2.2
VAD_START_MARGIN = 100.0
VAD_STOP_RATIO = 1.5
VAD_STOP_MARGIN = 60.0
DEFAULT_SILENCE_DURATION = 1.5
NOISE_RISE_SECONDS = 2.0
NOISE_FALL_SECONDS = 0.5
PCM_FRAME_BYTES = 1920  # 20 ms of the transports' 48 kHz mono PCM16.


def configured_vad_factory(extra: dict):
    """Resolve once at adapter startup; every input gets independent VAD state."""
    backend = extra.get("vad_backend", "silero")
    if backend == "rms":
        logger.info("VAD backend: rms")
        return AdaptiveRmsGate
    if backend != "silero":
        raise ValueError("vad_backend must be silero or rms")
    threshold = extra.get("vad_threshold", 0.5)
    if (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
            or not 0.2 <= threshold <= 0.9 or not math.isfinite(threshold)):
        raise ValueError("vad_threshold must be a finite number from 0.2 to 0.9")
    path = extra.get("vad_model_path", str(DEFAULT_SILERO_MODEL))
    if not isinstance(path, str) or not path.strip():
        raise ValueError("vad_model_path must name a verified copy of the pinned Silero model")
    from .speech_detector import SileroRmsGate, load_silero_model
    model = load_silero_model(path)
    logger.info("VAD backend: silero (threshold=%.2f, model=%s)", threshold, path)
    return partial(SileroRmsGate, model, float(threshold))


def configured_silence_duration(extra: dict) -> float:
    """Read seconds from the platform config, rejecting ambiguous values."""
    value = extra.get("silence_duration", DEFAULT_SILENCE_DURATION)
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not 0.2 <= value <= 5.0 or not math.isfinite(value)):
        raise ValueError("silence_duration must be a finite number from 0.2 to 5.0 seconds")
    return float(value)


@dataclass
class AdaptiveRmsGate:
    """Adaptive RMS gate that tolerates continuous fan and room noise."""

    calibration_frames: int = 20
    minimum_floor: float = 50.0
    noise_rms: float | None = None
    calibration: list[float] = field(default_factory=list)
    _calibration_pcm: bytearray = field(default_factory=bytearray, repr=False)

    @property
    def ready(self) -> bool:
        return self.noise_rms is not None

    @property
    def start_threshold(self) -> float:
        noise = self.noise_rms or self.minimum_floor
        return max(
            VAD_MIN_START_THRESHOLD,
            noise * VAD_START_RATIO,
            noise + VAD_START_MARGIN,
        )

    @property
    def stop_threshold(self) -> float:
        noise = self.noise_rms or self.minimum_floor
        return max(
            VAD_MIN_STOP_THRESHOLD,
            noise * VAD_STOP_RATIO,
            noise + VAD_STOP_MARGIN,
        )

    def calibrate(self, rms: float) -> bool:
        if self.ready:
            return True
        self.calibration.append(rms)
        if len(self.calibration) < self.calibration_frames:
            return False

        # Use the quieter half so somebody beginning to speak during the
        # short calibration window does not become the learned noise floor.
        ordered = sorted(self.calibration)
        quiet = ordered[: max(1, len(ordered) // 2)]
        measured = sum(quiet) / len(quiet)
        self.noise_rms = min(VAD_NOISE_CEILING, max(1.0, measured))
        self.calibration.clear()
        return True

    def calibrate_pcm(self, pcm: bytes) -> bool:
        """Measure actual audio windows, not transport packets or poll ticks."""
        if self.ready:
            return True
        needed = ((self.calibration_frames - len(self.calibration)) * PCM_FRAME_BYTES
                  - len(self._calibration_pcm))
        self._calibration_pcm.extend(pcm[:needed])
        while len(self._calibration_pcm) >= PCM_FRAME_BYTES:
            frame = bytes(self._calibration_pcm[:PCM_FRAME_BYTES])
            del self._calibration_pcm[:PCM_FRAME_BYTES]
            if self.calibrate(pcm_rms(frame)):
                return True
        return False

    def is_speech(self, rms: float, *, speaking: bool, frame_seconds: float = 0.02,
                  pcm: bytes | None = None) -> bool:
        if pcm is None or len(pcm) <= PCM_FRAME_BYTES:
            return self._is_speech_rms(rms, speaking, frame_seconds)
        # A short word can precede a quiet poll tail. Classify every new frame
        # without averaging speech into the noise floor. Preserve hysteresis
        # once speech begins, and process quiet frames too so adaptation runs.
        speech = False
        for offset in range(0, len(pcm), PCM_FRAME_BYTES):
            frame = pcm[offset:offset + PCM_FRAME_BYTES]
            current = self._is_speech_rms(
                pcm_rms(frame), speaking or speech, len(frame) / 96000,
            )
            speech = speech or current
        return speech

    def _is_speech_rms(self, rms: float, speaking: bool, frame_seconds: float) -> bool:
        threshold = self.stop_threshold if speaking else self.start_threshold
        speech = rms > threshold
        if speech or self.noise_rms is None:
            return speech
        if speaking and rms > self.noise_rms + max(30.0, self.noise_rms * 0.15):
            # Near-threshold quiet phonemes must not inflate the noise floor.
            return False
        # Learn ambient sound in pauses too, but freeze for speech frames.
        # Time-based coefficients keep 20 ms WebRTC and 200 ms LiveKit
        # observations consistent. Fall faster when a fan switches off.
        self._learn_noise(rms, frame_seconds)
        return False

    def _learn_noise(self, rms: float, frame_seconds: float) -> None:
        observed = min(rms, VAD_NOISE_CEILING)
        tau = NOISE_RISE_SECONDS if observed > self.noise_rms else NOISE_FALL_SECONDS
        alpha = -math.expm1(-frame_seconds / tau)
        self.noise_rms = max(1.0, self.noise_rms + alpha * (observed - self.noise_rms))
