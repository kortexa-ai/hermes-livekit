"""Opt-in local voice timings. Never retain prompt, reply, reasoning or audio."""

from __future__ import annotations

from collections import OrderedDict
import json
import logging
import math
import threading
import time

logger = logging.getLogger("gateway.platforms.livekit.voice_metrics")
_VOICES = {"realtime", "livekit"}
_SEEN_LIMIT = 1024


def _number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return value if value >= 0 else None
    return None


def _label(value):
    # JSON escapes control characters; cap identifiers independently of provider payloads.
    return value[:128] if isinstance(value, str) else ""


class VoiceMetrics:
    """One observer per plugin registration/profile, with bounded deduplication.

    Text timestamps are callback observation times, NOT provider token timestamps.
    Separate hook workers may run out of order; log independent events and join
    by (session, turn, iteration), never infer ordering from log line order.
    """

    def __init__(self):
        self._seen = OrderedDict()
        self._lock = threading.Lock()

    def _emit(self, event, *, session_id, turn_id, iteration, **metrics):
        logger.info("voice_timing %s", json.dumps({
            "event": event, "session_id": _label(session_id), "turn_id": _label(turn_id),
            "iteration": _number(iteration), **metrics,
        }, sort_keys=True, allow_nan=False))

    def on_text(self, *, session_id="", turn_id="", iteration=0, surface="",
                delta="", kind="text", **_ignored):
        if surface not in _VOICES or kind != "text" or not isinstance(delta, str) or not delta.strip():
            return
        observed_at = time.time()
        key = (_label(session_id), _label(turn_id), _number(iteration))
        if not key[0] or not key[1]:
            return
        with self._lock:
            if key in self._seen:
                return
            self._seen[key] = None
            if len(self._seen) > _SEEN_LIMIT:
                self._seen.popitem(last=False)
        self._emit("first_text_observed", session_id=session_id, turn_id=turn_id,
                   iteration=iteration, observed_at=observed_at)

    def on_api(self, *, session_id="", turn_id="", api_call_count=0, platform="",
               api_request_id="", api_duration=None, started_at=None, first_chunk_at=None,
               model="", provider="", assistant_tool_call_count=None, usage=None, **_ignored):
        if platform not in _VOICES:
            return
        started, first, duration = map(_number, (started_at, first_chunk_at, api_duration))
        # Missing/invalid provider timing stays unknown. Never call this prefill.
        ttfb = first - started if first is not None and started is not None else None
        if ttfb is not None and (duration is None or not 0 <= ttfb <= duration):
            ttfb = None
        usage = usage if isinstance(usage, dict) else {}
        self._emit("api_completed", session_id=session_id, turn_id=turn_id,
                   iteration=api_call_count, api_request_id=_label(api_request_id),
                   model=_label(model), provider=_label(provider), started_at=started,
                   api_s=duration, first_chunk_s=ttfb,
                   tool_calls=_number(assistant_tool_call_count),
                   usage={key: _number(usage.get(key)) for key in (
                       "prompt_tokens", "output_tokens", "cache_read_tokens", "reasoning_tokens",
                   )})


def register_voice_metrics(ctx):
    """Register only on explicit opt-in; old Hermes contexts remain unaffected."""
    get_config = getattr(ctx, "get_config", None)
    if get_config is None or get_config("voice_latency_metrics", False) is not True:
        return
    observer = VoiceMetrics()
    ctx.register_hook("on_stream_delta", observer.on_text)
    ctx.register_hook("post_api_request", observer.on_api)
