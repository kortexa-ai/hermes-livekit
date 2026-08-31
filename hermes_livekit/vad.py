"""Transport-neutral adaptive energy gate for voice activity detection."""

from __future__ import annotations

from dataclasses import dataclass, field


VAD_NOISE_CEILING = 300.0
VAD_MIN_START_THRESHOLD = 300.0
VAD_MIN_STOP_THRESHOLD = 220.0
VAD_START_RATIO = 2.2
VAD_START_MARGIN = 100.0
VAD_STOP_RATIO = 1.5
VAD_STOP_MARGIN = 60.0
VAD_NOISE_ALPHA = 0.02


@dataclass
class AdaptiveRmsGate:
    """Adaptive RMS gate that tolerates continuous fan and room noise."""

    calibration_frames: int = 20
    minimum_floor: float = 50.0
    noise_rms: float | None = None
    calibration: list[float] = field(default_factory=list)

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

    def is_speech(self, rms: float, *, speaking: bool) -> bool:
        threshold = self.stop_threshold if speaking else self.start_threshold
        speech = rms > threshold
        if not speaking and not speech and self.noise_rms is not None:
            # Follow slow changes such as a fan ramping up, but never learn a
            # speech frame or allow an outlier to raise the floor indefinitely.
            observed = min(rms, VAD_NOISE_CEILING)
            self.noise_rms = (
                (1.0 - VAD_NOISE_ALPHA) * self.noise_rms
                + VAD_NOISE_ALPHA * observed
            )
        return speech
