# PCM-based room calibration and short-speech preservation

Work unit: <https://github.com/kortexa-ai/hermes-livekit/issues/57>
Program: <https://github.com/kortexa-ai/hermes-livekit/issues/14>

## Contract

Room calibration uses the quieter half of 400 ms of actual 48 kHz mono PCM16
audio, measured in 20 ms windows. Packet size and conference polling must not
change that window or hide speech that precedes the latest poll tail. Original
audio remains unchanged for ASR. Speech classification must not relearn a short
signal as room noise by averaging it with the rest of a large packet.

Retain the existing silence timeout, capture/preroll bounds, mute/pause reset,
early-ASR invalidation and optional stateful Silero classifier. This is energy
detection, not semantic endpointing or proof of acoustic accuracy in every room.

## Verification

Use synthetic PCM through both the gate and real transport endpoint methods,
with only downstream ASR/publication sinks replaced. Cover cold/warm calls,
continuous quiet, small and large packets, delayed conference polling and
preservation of complete early speech. Run the full plugin suite on Linux and
macOS and measure synthetic CPU cost separately from provider latency.

Deployment targets only the Mira gateway on Snappy. Synchronize through Git,
validate with the gateway's interpreter, and use its profile-specific graceful
restart. Preserve a clean pre-change revision for a focused revert. Production
receipts and current execution state belong in the linked issue.
