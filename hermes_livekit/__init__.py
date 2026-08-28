"""hermes-livekit — LiveKit voice gateway plugin for hermes-agent.

Registers a ``livekit`` platform via the ``hermes_agent.plugins`` entry
point. No core hermes-agent edits are required — every integration touch
point uses an existing ``register_platform()`` hook.
"""

import logging
import os
from typing import Optional

from .adapter import TOOLSET_NAME, LiveKitAdapter, check_livekit_requirements
from .direct_tools import install_direct_toolsets
from .realtime_webrtc import (
    RealtimeWebRTCAdapter,
    check_realtime_requirements,
)

logger = logging.getLogger("gateway.platforms.livekit")

__all__ = [
    "register",
    "LiveKitAdapter",
    "RealtimeWebRTCAdapter",
    "check_livekit_requirements",
    "check_realtime_requirements",
]


_LIVEKIT_PLATFORM_HINT = (
    "You are communicating via a LiveKit voice channel (WebRTC). "
    "The user speaks to you and hears your replies as audio. "
    "Keep responses concise and conversational — they will be read aloud via TTS. "
    "Avoid markdown formatting, long lists, code blocks, or URLs. "
    "Do not include MEDIA: tags. Focus on clear, spoken-word responses."
)


def _env_enablement() -> Optional[dict]:
    """Seed ``PlatformConfig.extra`` from env vars during gateway config load.

    Called by the platform registry BEFORE the adapter is constructed, so
    ``hermes gateway status`` reflects env-only configuration without
    instantiating the LiveKit SDK. Returns ``None`` when LiveKit isn't
    minimally configured; the caller skips auto-enabling.
    """
    url = (os.getenv("LIVEKIT_URL") or "").strip()
    api_key = (os.getenv("LIVEKIT_API_KEY") or "").strip()
    api_secret = (os.getenv("LIVEKIT_API_SECRET") or "").strip()
    if not (url and api_key and api_secret):
        return None

    room = os.getenv("LIVEKIT_ROOM", "hermes")
    seed: dict = {
        "url": url,
        "api_key": api_key,
        "api_secret": api_secret,
        "room": room,
        "agent_name": os.getenv("LIVEKIT_AGENT_NAME", "Hermes"),
        "agent_avatar": os.getenv("LIVEKIT_AGENT_AVATAR", ""),
    }

    # LiveKit's adapter only ever joins one room, so the room IS the home
    # channel by definition. Default LIVEKIT_HOME_CHANNEL to LIVEKIT_ROOM
    # unless explicitly overridden — keeps cron / cross-platform delivery
    # sensible without requiring the user to duplicate the value.
    home = (os.getenv("LIVEKIT_HOME_CHANNEL") or room).strip()
    if home:
        os.environ.setdefault("LIVEKIT_HOME_CHANNEL", home)
        seed["home_channel"] = {
            "chat_id": home,
            "name": os.getenv("LIVEKIT_HOME_CHANNEL_NAME", "Home"),
        }
    return seed


def _configured_value(cfg, key: str, env_name: str) -> str:
    """Read a platform extra with the matching environment variable fallback."""
    try:
        value = (cfg.extra or {}).get(key)
    except Exception:
        value = None
    return str(value or os.getenv(env_name) or "").strip()


def _validate_config(cfg) -> bool:
    """True when all credentials needed to connect are configured."""
    return all(
        (
            _configured_value(cfg, "url", "LIVEKIT_URL"),
            _configured_value(cfg, "api_key", "LIVEKIT_API_KEY"),
            _configured_value(cfg, "api_secret", "LIVEKIT_API_SECRET"),
        )
    )


def _is_connected(cfg) -> bool:
    """Tell Hermes whether LiveKit is sufficiently configured to start."""
    return _validate_config(cfg)


def _apply_yaml_config(_yaml_cfg: dict, platform_cfg: dict) -> Optional[dict]:
    """Preserve LiveKit-specific YAML keys in ``PlatformConfig.extra``.

    Current Hermes preserves arbitrary adapter settings only when they are
    nested under ``extra``. This bridge also accepts the ergonomic direct form
    under ``platforms.livekit`` or ``gateway.platforms.livekit``.
    """
    nested = platform_cfg.get("extra")
    nested = nested if isinstance(nested, dict) else {}
    seeded: dict = {}
    for key in ("url", "api_key", "api_secret", "room", "agent_name", "agent_avatar"):
        if key in nested:
            seeded[key] = nested[key]
        elif key in platform_cfg:
            seeded[key] = platform_cfg[key]
    return seeded or None


def _realtime_env_enablement() -> Optional[dict]:
    """Enable the direct listener only when the operator opts in."""
    enabled = os.getenv("HERMES_REALTIME_ENABLED", "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return None
    token = os.getenv("HERMES_REALTIME_API_KEY", "").strip()
    if not token:
        return None
    raw_port = os.getenv("HERMES_REALTIME_PORT", "8091")
    try:
        port = int(raw_port)
    except ValueError:
        return None
    if not 0 < port < 65536:
        return None
    return {
        "host": os.getenv("HERMES_REALTIME_HOST", "127.0.0.1"),
        "port": port,
        "api_key": token,
    }


def _apply_realtime_yaml_config(_yaml_cfg: dict, platform_cfg: dict) -> Optional[dict]:
    nested = platform_cfg.get("extra")
    nested = nested if isinstance(nested, dict) else {}
    seeded: dict = {}
    for key in ("host", "port", "api_key", "max_calls", "max_call_seconds"):
        if key in nested:
            seeded[key] = nested[key]
        elif key in platform_cfg:
            seeded[key] = platform_cfg[key]
    return seeded or None


def _validate_realtime_config(cfg) -> bool:
    try:
        extra = cfg.extra or {}
        token = str(extra.get("api_key") or os.getenv("HERMES_REALTIME_API_KEY", ""))
        port = int(extra.get("port") or os.getenv("HERMES_REALTIME_PORT", "8091"))
        return 0 < port < 65536 and bool(token)
    except (TypeError, ValueError):
        return False


def _interactive_setup() -> None:
    """Prompt the user for LiveKit credentials and persist to .env.

    Minimal first-pass setup — falls back to instructions when the
    interactive helpers aren't importable. The standalone-platform
    setup wizard in ``hermes_cli/gateway.py`` covers most env-driven
    setups; this is a plugin-side fallback for ``hermes config`` flows
    that bypass that wizard.
    """
    try:
        from hermes_cli.config import set_env_value
    except Exception:
        print("LiveKit interactive setup requires a hermes-agent install.")
        print("Set these env vars manually in your .env:")
        print("  LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET")
        print("  LIVEKIT_ROOM (default: hermes)")
        return

    print("\nLiveKit setup (press Enter to skip a value)")
    url = input("  LIVEKIT_URL (wss://...): ").strip()
    if url:
        set_env_value("LIVEKIT_URL", url)
    api_key = input("  LIVEKIT_API_KEY: ").strip()
    if api_key:
        set_env_value("LIVEKIT_API_KEY", api_key)
    api_secret = input("  LIVEKIT_API_SECRET: ").strip()
    if api_secret:
        set_env_value("LIVEKIT_API_SECRET", api_secret)
    room = input("  LIVEKIT_ROOM (default: hermes): ").strip()
    if room:
        set_env_value("LIVEKIT_ROOM", room)
    print("LiveKit settings saved.")


def register(ctx) -> None:
    """Plugin entry point — called by the hermes-agent plugin loader.

    Registers a ``livekit`` platform that can be enabled in
    ``~/.hermes/config.yaml`` (``platforms.livekit.enabled: true``) and
    auto-configures from ``LIVEKIT_URL`` / ``LIVEKIT_API_KEY`` /
    ``LIVEKIT_API_SECRET`` env vars.
    """
    ctx.register_platform(
        name="livekit",
        label="LiveKit",
        adapter_factory=lambda cfg: LiveKitAdapter(cfg),
        check_fn=check_livekit_requirements,
        validate_config=_validate_config,
        is_connected=_is_connected,
        required_env=["LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"],
        install_hint="pip install hermes-livekit  # adds livekit + livekit-api SDKs",
        setup_fn=_interactive_setup,
        # Env-driven auto-config: seeds PlatformConfig.extra + home_channel
        # from LIVEKIT_* env vars, so env-only setups show up in
        # `hermes gateway status` without instantiating the adapter.
        env_enablement_fn=_env_enablement,
        apply_yaml_config_fn=_apply_yaml_config,
        # Cron home-channel delivery support.
        cron_deliver_env_var="LIVEKIT_HOME_CHANNEL",
        # Auth env vars
        allowed_users_env="LIVEKIT_ALLOWED_USERS",
        allow_all_env="LIVEKIT_ALLOW_ALL_USERS",
        # Display
        emoji="🎙️",
        # LiveKit identities are not phone numbers / emails
        pii_safe=False,
        # /update from a voice channel makes no sense
        allow_update_command=False,
        # LLM guidance — delivered to run_agent.py via PlatformEntry.platform_hint
        platform_hint=_LIVEKIT_PLATFORM_HINT,
    )

    ctx.register_platform(
        name="realtime",
        label="Realtime WebRTC",
        adapter_factory=lambda cfg: RealtimeWebRTCAdapter(cfg),
        check_fn=check_realtime_requirements,
        validate_config=_validate_realtime_config,
        is_connected=_validate_realtime_config,
        required_env=[],
        install_hint="pip install hermes-livekit  # adds aiortc + aiohttp",
        env_enablement_fn=_realtime_env_enablement,
        apply_yaml_config_fn=_apply_realtime_yaml_config,
        allowed_users_env="HERMES_REALTIME_ALLOWED_USERS",
        allow_all_env="HERMES_REALTIME_ALLOW_ALL_USERS",
        emoji="⚡",
        pii_safe=False,
        allow_update_command=False,
        platform_hint=_LIVEKIT_PLATFORM_HINT.replace("LiveKit voice channel", "direct WebRTC voice call"),
    )

    # Declare the platform bundles and late-bound remote-tool toolsets.
    # Client tools are registered only after a participant connects, but the
    # empty declaration keeps config validation truthful before that happens.
    try:
        from toolsets import TOOLSETS, _HERMES_CORE_TOOLS
        if "hermes-livekit" not in TOOLSETS:
            TOOLSETS["hermes-livekit"] = {
                "description": "LiveKit voice toolset — interact with Hermes via WebRTC voice",
                "tools": _HERMES_CORE_TOOLS,
                "includes": [],
            }
        if TOOLSET_NAME not in TOOLSETS:
            TOOLSETS[TOOLSET_NAME] = {
                "description": "Tools offered by connected LiveKit clients",
                "tools": [],
                "includes": [],
            }
        install_direct_toolsets()
    except Exception:
        # Toolset registration is best-effort; the adapter still works
        # without it (resolves through the gateway umbrella toolset).
        pass
