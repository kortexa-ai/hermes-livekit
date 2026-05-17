#!/usr/bin/env python3
"""Hermes-livekit interactive test client.

Drives the hermes-livekit plugin and the agent behind it as if it were
a regular client. Useful for developing the plugin without depending
on the voice/ASR path.

Capabilities:
  - Joins a LiveKit room with a self-minted token
  - Registers a single tool, ``desktop_notify(title, body)``, that
    pops a macOS notification via ``osascript`` and logs the call
  - Logs every inbound data-channel message (any topic)
  - Reads stdin: each typed line is sent as a ``client:message`` user
    prompt to the agent (same path the voice transcript takes)
  - Special commands: ``/quit``, ``/raw <json>``, ``/reregister``

Run:
  python examples/test_client.py [--room hermes-avery] [--identity test-client]

Auth: reads LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET from the
environment. Falls back to parsing ``~/.hermes/.env`` if any are unset.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from livekit import api, rtc

LOG = logging.getLogger("test-client")

HERMES_CONTROL_TOPIC = "hermes-control"

TOOL_NAME = "desktop_notify"
TOOL_DESCRIPTION = "Show a desktop notification on the user's machine (macOS)."
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Notification title."},
        "body": {"type": "string", "description": "Notification body text."},
    },
    "required": ["title", "body"],
}


def load_env_fallback() -> None:
    """Source LIVEKIT_* from ~/.hermes/.env when not already set in the environment."""
    env_path = Path.home() / ".hermes" / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def mint_token(room: str, identity: str) -> tuple[str, str]:
    url = os.environ.get("LIVEKIT_URL", "").strip()
    key = os.environ.get("LIVEKIT_API_KEY", "").strip()
    secret = os.environ.get("LIVEKIT_API_SECRET", "").strip()
    if not (url and key and secret):
        raise SystemExit(
            "missing LiveKit creds — set LIVEKIT_URL / LIVEKIT_API_KEY / "
            "LIVEKIT_API_SECRET, or populate ~/.hermes/.env"
        )
    token = (
        api.AccessToken(api_key=key, api_secret=secret)
        .with_identity(identity)
        .with_name(identity)
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room,
                can_publish=False,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
        .to_jwt()
    )
    return url, token


def macos_notify(title: str, body: str) -> None:
    """Pop a macOS notification via osascript with AppleScript-safe escaping."""

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    script = f'display notification "{esc(body)}" with title "{esc(title)}"'
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


class TestClient:
    def __init__(self, room_name: str, identity: str, agent_prefix: str = "hermes-") -> None:
        self.room_name = room_name
        self.identity = identity
        # Adapter identity is `hermes-<agent_name_lowercased>`; we register
        # tools only after we see *some* participant with this prefix in the
        # room, otherwise the data message goes to an empty room and is lost.
        self.agent_prefix = agent_prefix
        # Room is built lazily inside connect() — rtc.Room.__init__ captures
        # asyncio.get_event_loop() at construction, so we have to create it
        # on the same loop that asyncio.run() spins up.
        self.room: Optional[rtc.Room] = None
        self._stop = asyncio.Event()
        self._reply_done = asyncio.Event()   # set when agent:agent-transcript final:true arrives
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._registered: bool = False

    async def connect(self) -> None:
        url, token = mint_token(self.room_name, self.identity)
        # Build the Room here, inside the running event loop — the SDK binds
        # itself to the loop in __init__.
        self.room = rtc.Room()
        self.room.on("data_received", self._on_data)
        self.room.on("participant_connected", self._on_participant_connected)
        self.room.on("participant_disconnected", lambda p: LOG.info("participant left: %s", p.identity))
        self.room.on("connected", lambda: LOG.info("room event: connected"))
        self.room.on("connection_state_changed", lambda s: LOG.info("room event: connection_state_changed -> %s", s))
        self.room.on("disconnected", lambda reason: LOG.info("disconnected: %s", reason))
        LOG.info("connecting to %s as %s in room %s", url, self.identity, self.room_name)
        await self.room.connect(url, token)
        LOG.info("connected")

    async def publish(self, payload: dict[str, Any], topic: str = "") -> None:
        if self.room is None:
            LOG.warning("publish called before connect — dropping %s", payload.get("type"))
            return
        data = json.dumps(payload).encode()
        await self.room.local_participant.publish_data(data, reliable=True, topic=topic)

    async def register_tool(self) -> None:
        # Short-circuit: connect-path and participant_connected event can both
        # race here; the second arrival is a no-op.
        if self._registered:
            return
        self._registered = True
        await self.publish(
            {
                "type": "client:tool-register",
                "name": TOOL_NAME,
                "description": TOOL_DESCRIPTION,
                "input_schema": TOOL_SCHEMA,
            },
            topic=HERMES_CONTROL_TOPIC,
        )
        LOG.info("sent client:tool-register for %s", TOOL_NAME)

    def _agent_in_room(self) -> bool:
        if self.room is None:
            return False
        for p in self.room.remote_participants.values():
            if p.identity.startswith(self.agent_prefix):
                return True
        return False

    def _on_participant_connected(self, participant: rtc.RemoteParticipant) -> None:
        LOG.info("participant joined: %s", participant.identity)
        if self._registered:
            return
        if not participant.identity.startswith(self.agent_prefix):
            return
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self.register_tool(), self._loop)

    async def unregister_tool(self) -> None:
        try:
            await self.publish(
                {"type": "client:tool-unregister", "name": TOOL_NAME},
                topic=HERMES_CONTROL_TOPIC,
            )
        except Exception as exc:
            LOG.debug("tool unregister failed: %s", exc)

    def _on_data(self, packet: rtc.DataPacket) -> None:
        """Sync handler (livekit-rtc fires synchronously). Decode and dispatch."""
        try:
            msg = json.loads(packet.data.decode())
        except Exception:
            LOG.info(
                "inbound raw bytes on topic %r (non-JSON, %d bytes)",
                packet.topic,
                len(packet.data),
            )
            return
        topic = packet.topic or "(default)"
        if isinstance(msg, dict):
            msg_type = msg.get("type")
            summary = json.dumps(msg)
            if len(summary) > 500:
                summary = summary[:500] + "…"
            LOG.info("inbound [%s] %s: %s", topic, msg_type, summary)
            if msg_type == "agent:tool-call" and self._loop is not None:
                asyncio.run_coroutine_threadsafe(self._handle_tool_call(msg), self._loop)
            elif msg_type == "agent:agent-transcript":
                # Final assistant reply for this turn — signal oneshot mode to
                # exit. (Wrapped events have payload.final; flat ones would have
                # final at top level. Check both.)
                payload = msg.get("payload") if isinstance(msg.get("payload"), dict) else msg
                if payload.get("final"):
                    self._reply_done.set()
        else:
            LOG.info("inbound [%s] non-dict payload: %r", topic, msg)

    async def _handle_tool_call(self, msg: dict[str, Any]) -> None:
        call_id = msg.get("call_id")
        name = msg.get("name")
        args = msg.get("arguments") or {}
        if name != TOOL_NAME:
            LOG.warning("tool-call for unknown tool %r (call_id=%s)", name, call_id)
            await self.publish(
                {
                    "type": "client:tool-result",
                    "call_id": call_id,
                    "error": f"unknown tool: {name}",
                },
                topic=HERMES_CONTROL_TOPIC,
            )
            return
        title = str(args.get("title", ""))
        body = str(args.get("body", ""))
        LOG.info("→ desktop_notify(title=%r, body=%r) [call_id=%s]", title, body, call_id)
        try:
            macos_notify(title, body)
            result = {"shown": True}
            await self.publish(
                {"type": "client:tool-result", "call_id": call_id, "result": result},
                topic=HERMES_CONTROL_TOPIC,
            )
            LOG.info("← tool-result %s", result)
        except Exception as exc:
            LOG.exception("notification failed")
            await self.publish(
                {"type": "client:tool-result", "call_id": call_id, "error": str(exc)},
                topic=HERMES_CONTROL_TOPIC,
            )

    async def send_prompt(self, text: str) -> None:
        await self.publish(
            {"type": "client:message", "text": text},
            topic=HERMES_CONTROL_TOPIC,
        )
        LOG.info("→ prompt: %s", text)

    async def send_raw(self, raw_json: str) -> None:
        try:
            obj = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            print(f"  invalid JSON: {exc}", file=sys.stderr)
            return
        if isinstance(obj, dict):
            topic = obj.pop("__topic", HERMES_CONTROL_TOPIC)
        else:
            topic = HERMES_CONTROL_TOPIC
        await self.publish(obj, topic=topic)
        LOG.info("→ raw on topic %r: %s", topic, obj)

    async def repl(self) -> None:
        loop = asyncio.get_running_loop()
        print(
            "test-client ready. type messages to send to the agent.\n"
            "  /quit                end the session\n"
            "  /raw <json>          publish arbitrary JSON on hermes-control\n"
            "                       (set __topic in the JSON to override topic)\n"
            "  /reregister          re-send the desktop_notify registration\n",
            file=sys.stderr,
        )
        while not self._stop.is_set():
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            line = line.rstrip("\n")
            if not line:
                continue
            if line == "/quit":
                self._stop.set()
                break
            if line.startswith("/raw "):
                await self.send_raw(line[5:])
                continue
            if line == "/reregister":
                await self.register_tool()
                continue
            await self.send_prompt(line)

    async def wait_for_agent(self, timeout: float = 60.0) -> bool:
        """Block until the adapter participant (hermes-*) joins, or timeout."""
        deadline = asyncio.get_running_loop().time() + timeout
        tick = 0
        while asyncio.get_running_loop().time() < deadline:
            if self._agent_in_room():
                return True
            tick += 1
            if tick % 6 == 0:
                idents = sorted(p.identity for p in self.room.remote_participants.values())
                LOG.info("still waiting; remote_participants=%s", idents)
            await asyncio.sleep(0.5)
        return False

    async def run(self, oneshot_prompt: Optional[str] = None, wait_timeout: float = 60.0) -> None:
        self._loop = asyncio.get_running_loop()
        await self.connect()
        # Either Avery is already here, or we need to wait. _on_participant_connected
        # also registers when she shows up — this just makes the initial path explicit.
        if self._agent_in_room():
            await self.register_tool()
        else:
            LOG.info("waiting up to %.0fs for agent (prefix=%r) to join…", wait_timeout, self.agent_prefix)
            ok = await self.wait_for_agent(wait_timeout)
            if not ok:
                LOG.warning("no agent participant joined within %.0fs", wait_timeout)
            elif not self._registered:
                # Defensive: participant_connected event may have fired before
                # we attached the handler in some race; register now if not yet done.
                await self.register_tool()

        try:
            if oneshot_prompt is not None:
                # Non-interactive: send one prompt, wait for the agent's final
                # transcript (any number of tool calls in between are fine).
                await asyncio.sleep(0.5)  # let agent:tool-registered ack land in logs
                self._reply_done.clear()
                await self.send_prompt(oneshot_prompt)
                LOG.info("oneshot mode: waiting up to %.0fs for agent reply…", wait_timeout)
                try:
                    await asyncio.wait_for(self._reply_done.wait(), timeout=wait_timeout)
                    LOG.info("oneshot mode: agent reply received, settling briefly")
                    # Small grace window so any trailing events (TTS lifecycle,
                    # late tool acks) land in the log before we tear down.
                    await asyncio.sleep(0.5)
                except asyncio.TimeoutError:
                    LOG.warning("oneshot mode: no final agent reply within %.0fs, exiting", wait_timeout)
            else:
                await self.repl()
        finally:
            if self.room is not None:
                await self.unregister_tool()
                await self.room.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="hermes-livekit interactive test client"
    )
    parser.add_argument(
        "--room",
        default=os.getenv("LIVEKIT_ROOM", "hermes-avery"),
        help="room to join (default: LIVEKIT_ROOM env or hermes-avery)",
    )
    parser.add_argument(
        "--identity",
        default="test-client",
        help="participant identity (default: test-client)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="verbose (DEBUG) logging"
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help="non-interactive: send this single prompt, wait for response, exit",
    )
    parser.add_argument(
        "--wait-timeout",
        type=float,
        default=60.0,
        help="seconds to wait for agent to join / for oneshot reply (default: 60)",
    )
    parser.add_argument(
        "--agent-prefix",
        default="hermes-",
        help="identity prefix the agent uses (default: 'hermes-')",
    )
    args = parser.parse_args()

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        level=logging.DEBUG if args.verbose else logging.INFO,
    )
    load_env_fallback()

    client = TestClient(args.room, args.identity, agent_prefix=args.agent_prefix)
    try:
        asyncio.run(client.run(oneshot_prompt=args.prompt, wait_timeout=args.wait_timeout))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)


if __name__ == "__main__":
    main()
