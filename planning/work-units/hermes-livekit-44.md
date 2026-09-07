# Same-model reasoning latency

Owning issue: https://github.com/kortexa-ai/hermes-livekit/issues/44

Compare medium and low reasoning on Mira's current model using isolated voice
calls. Reuse the native session-only `/reasoning` command before the first
measured utterance. Confirm the English command acknowledgement; fail on a
different setting, persistent scope, error, unexpected audio or timeout. Do not
change the real kiosk session, global config, model, toolset or service tier.

Exclude command setup from measured voice events. Preserve split/premature
utterance rejection. Make bounded fixture-answer output explicit so basic
correctness can be checked without logging arbitrary payloads by default.

Test command construction and acknowledgement/error/timeout behavior offline,
then run serial real voice cases with counterbalanced effort order. Compare
model stage timings as well as end-to-end latency. Check byte-identical Mira
configuration and unchanged healthy services. Simple fixtures are screening
evidence, not proof of general reasoning or tool quality. Do not promote a
production default on a weak or noisy result. Deliver probe/docs through Git;
no runtime restart is needed for these diagnostic changes.
