"""Make streaming TTS honour ``tts.openai.base_url``.

hermes-agent's ``OpenAIStreamer`` reads the model and voice from the
``tts.openai`` config section but takes ``base_url`` and ``api_key`` from the
environment. The synchronous path deliberately does the opposite —

    base_url = config_base_url or fallback_base or DEFAULT_OPENAI_BASE_URL
    # "Config override wins over the auth-chain fallback (restores the
    #  pre-refactor precedence, where tts.openai.base_url beat the resolved
    #  default)"

— so a self-hosted OpenAI-compatible TTS server works for ordinary replies and
then silently escapes to api.openai.com the moment streaming is used, asking it
for a local model name in a local voice.

This registers a replacement that applies the synchronous path's precedence.
It is deliberately additive: with no ``base_url`` in config the subclass defers
to the original behaviour, so nothing changes for people using real OpenAI.

Upstreamed separately; this override is safe to delete once that lands.
"""

from __future__ import annotations

import logging
from typing import Iterator, Optional

logger = logging.getLogger("gateway.platforms.livekit")


def install() -> bool:
    """Swap in the config-aware streamer. Returns True when installed."""
    try:
        from tools import tts_streaming
    except Exception:
        # Older hermes-agent without the streaming core — nothing to patch.
        return False

    base_cls = tts_streaming._REGISTRY.get("openai")
    if base_cls is None or getattr(base_cls, "_kortexa_base_url_fix", False):
        return False

    class ConfigBaseUrlOpenAIStreamer(base_cls):  # type: ignore[valid-type,misc]
        """OpenAI streamer that prefers the configured endpoint over the env."""

        _kortexa_base_url_fix = True

        def _configured(self, key: str) -> Optional[str]:
            value = (self.section or {}).get(key)
            return value.strip() if isinstance(value, str) and value.strip() else None

        def stream(self, text: str) -> Iterator[bytes]:
            base_url = self._configured("base_url")
            if not base_url:
                # No local endpoint configured — behave exactly as upstream.
                yield from super().stream(text)
                return

            from openai import OpenAI

            from tools.tts_streaming import get_env_value

            client = OpenAI(
                # A local server usually ignores auth, but the SDK still
                # requires something non-empty.
                api_key=self._configured("api_key") or get_env_value("OPENAI_API_KEY") or "no-auth",
                base_url=base_url,
            )
            with client.audio.speech.with_streaming_response.create(
                model=(self.section or {}).get("model", "gpt-4o-mini-tts"),
                voice=(self.section or {}).get("voice", "alloy"),
                input=text,
                response_format="pcm",
            ) as response:
                yield from response.iter_bytes()

    tts_streaming._REGISTRY["openai"] = ConfigBaseUrlOpenAIStreamer
    logger.info("[livekit] streaming TTS will use tts.openai.base_url when configured")
    return True
