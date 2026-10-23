# Contributing to hermes-realtime

Thanks for poking at this. The project is small, the maintenance bar is low,
and we'd rather merge a working patch than gate on process.

## Dev install

```bash
git clone https://github.com/kortexa-ai/hermes-realtime
cd hermes-realtime

# Build a separate Python 3.14 test environment through Hermes PM.
scripts/bootstrap_tests.sh
```

Bootstrap uses `uv python find 3.14`; install Python 3.14 with uv first if it is
absent. It fetches a clean vanilla Hermes checkout and asks PM to build a fresh
`.venv/tests314` environment from a core-plus-plugin workspace under
`.venv/test-project314`. It never clears an existing environment. For a rebuild,
stop its test processes and remove only those two disposable directories first.
The generated workspace resolves dependencies for the host OS and architecture,
while retaining Hermes' Python constraints. It does not produce a cross-platform
lockfile or change the source manifests.

Keep runtime dependency pins in the distribution's `pyproject.toml` and the
native `hermes_realtime/pyproject.toml` aligned. The native project is deliberately
metadata-only so two Hermes profiles can share one PM environment.

Install a reviewed commit in a gateway through the normal plugin command:

```bash
hermes plugins install kortexa-ai/hermes-realtime/hermes_realtime \
  --ref <full-commit-sha> --enable --yes-deps
hermes gateway restart
```

Use `hermes --profile <name>` for each additional profile. Do not add packages
to a PM-managed gateway environment with pip or uv.

## Running it locally

You'll need:

- A reachable LiveKit server (cloud or self-hosted)
- `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` in `~/.hermes/.env`
- `ffmpeg` on `PATH` (TTS decode)
- The LiveKit CLI (`brew install livekit-cli`) for ad-hoc room testing

### Tests

```bash
scripts/run_tests.sh
```

The first lane always uses the vanilla checkout under `.development` and the
Python 3.14 environment under `.venv/tests314`. It fails before collection if
Python resolves the adjacent Kortexa checkout by accident. The runner clears
inherited credentials and gives tests a disposable home.

Kortexa developers can copy `.env.development.example` to
`.env.development.local`. The same command then runs a second lane against the
adjacent `../hermes-agent` checkout after verifying its `origin` is
`https://github.com/kortexa-ai/hermes-agent.git` and its checked-out branch is
`kortexa-ai/main`. This lane includes the optimized realtime pipeline tests.

`tests/test_adapter_contract.py` checks that every `LiveKitAdapter` override
still accepts what the gateway passes to `BasePlatformAdapter`. That drift has
broken a whole path twice — `play_tts()` missing `caption` killed every voice
reply, `connect()` missing `is_reconnect` kept the platform down after any
disconnect — and neither shows up until that exact path runs against a live
gateway. Run it after any upstream bump.

A reasonable smoke test loop, given those:

1. Restart the gateway, confirm `Connecting to livekit... ✓ livekit connected`
   in `~/.hermes/logs/gateway.log`.
2. From any LiveKit client, join the room named in `LIVEKIT_ROOM` (default
   `hermes-<lowercase-agent-name>`).
3. Speak. The agent should hear, transcribe, respond.

For frame-capture testing, see the `data channel protocol` section of the
README — send `{"type": "conference.capture_frame"}` on the
`conference.extensions` topic while a video track is published.

To enable verbose adapter logs:

```bash
HERMES_LIVEKIT_LOG_LEVEL=DEBUG hermes gateway run
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
