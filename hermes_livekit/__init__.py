"""hermes-livekit — LiveKit voice gateway plugin for hermes-agent.

Registers a ``livekit`` platform via the ``hermes_agent.plugins`` entry
point. No core hermes-agent edits are required — every integration touch
point uses an existing ``register_platform()`` hook.
"""

import os
from typing import Optional

from .adapter import LiveKitAdapter, check_livekit_requirements

__all__ = ["register", "LiveKitAdapter", "check_livekit_requirements"]


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


def _is_connected(cfg) -> bool:
    """True when the gateway should consider LiveKit configured.

    Mirrors the ``cfg.extra.get('url')`` check that the kortexa branch
    inlined in ``_PLATFORM_CONNECTED_CHECKERS``. The url is the load-bearing
    field — without it, neither the SDK nor presence polling can run.
    """
    try:
        return bool((cfg.extra or {}).get("url"))
    except Exception:
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
        is_connected=_is_connected,
        required_env=["LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"],
        install_hint="pip install hermes-livekit  # adds livekit + livekit-api SDKs",
        setup_fn=_interactive_setup,
        # Env-driven auto-config: seeds PlatformConfig.extra + home_channel
        # from LIVEKIT_* env vars, so env-only setups show up in
        # `hermes gateway status` without instantiating the adapter.
        env_enablement_fn=_env_enablement,
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

    # Provide a hermes-livekit toolset alias so cli/gateway tooling defaults
    # match the kortexa branch behaviour.  ``get_all_platforms()`` already
    # synthesises the ``hermes-{name}`` mapping in PlatformInfo, but the
    # toolset itself has to exist in the TOOLSETS dict for tool resolution.
    try:
        from toolsets import TOOLSETS, _HERMES_CORE_TOOLS
        if "hermes-livekit" not in TOOLSETS:
            TOOLSETS["hermes-livekit"] = {
                "description": "LiveKit voice toolset — interact with Hermes via WebRTC voice",
                "tools": _HERMES_CORE_TOOLS,
                "includes": [],
            }
    except Exception:
        # Toolset registration is best-effort; the adapter still works
        # without it (resolves through the gateway umbrella toolset).
        pass
