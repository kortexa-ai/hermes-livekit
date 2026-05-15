# Contributing to hermes-livekit

Thanks for poking at this. The project is small, the maintenance bar is low,
and we'd rather merge a working patch than gate on process.

## Dev install

```bash
git clone https://github.com/kortexa-ai/hermes-livekit
cd hermes-livekit

# Install editable into the SAME venv your hermes-agent gateway runs in.
# (If you don't know which one, check `ps -ef | grep hermes_cli.main` and
# read the python path off the running process.)
pip install -e .
```

If you use `uv` and the gateway venv is at `~/src/hermes-agent/venv`:

```bash
VIRTUAL_ENV=~/src/hermes-agent/venv uv pip install -e .
```

The plugin auto-discovers through the `hermes_agent.plugins` entry-point
group — no config edits required to make the install visible to hermes.

To enable it in a running hermes, add `livekit` to `plugins.enabled` in
`~/.hermes/config.yaml`, then restart the gateway.

## Running it locally

You'll need:

- A reachable LiveKit server (cloud or self-hosted)
- `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` in `~/.hermes/.env`
- `ffmpeg` on `PATH` (TTS decode)
- The LiveKit CLI (`brew install livekit-cli`) for ad-hoc room testing

A reasonable smoke test loop, given those:

1. Restart the gateway, confirm `Connecting to livekit... ✓ livekit connected`
   in `~/.hermes/logs/gateway.log`.
2. From any LiveKit client, join the room named in `LIVEKIT_ROOM` (default
   `hermes-<lowercase-agent-name>`).
3. Speak. The agent should hear, transcribe, respond.

For frame-capture testing, see the `data channel protocol` section of the
README — send `{"type": "client:capture-frame"}` on the `hermes-control`
topic while a video track is published.

To enable verbose adapter logs:

```bash
HERMES_LIVEKIT_LOG_LEVEL=DEBUG launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway
```

## Code style

Match hermes-agent:

- 4-space indent
- `snake_case` for functions and variables
- `PascalCase` for classes
- Type hints encouraged, not enforced
- Comments explain *why*, not *what*. Code should already say *what*.
- No emojis in source unless explicitly requested

## Commit style

Conventional commits, lowercase scope, one of `feat`/`fix`/`chore`/`docs`/`refactor`/`test`:

```
feat(adapter): wire client:capture-frame to video sampling

Subscribe to remote video tracks but don't iterate eagerly. On
client:capture-frame, pull one frame from the sender's stream,
JPEG-encode, queue for next MessageEvent dispatch.
```

Single-purpose commits beat omnibus ones for review.

## Pull requests

Open them against `main`. Include:

- A short description of *why* the change exists (the *what* is in the diff).
- Manual verification steps if the change isn't covered by an existing test.
- Any new config / env vars documented in the README and CHANGELOG.

No template, no labels, no required reviewers. Just be reachable for
follow-up.

## License

By contributing you agree your contributions are licensed under the project's
[MIT License](./LICENSE).
