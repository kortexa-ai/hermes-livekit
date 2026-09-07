# First-emission TTS contribution

Tracking: https://github.com/kortexa-ai/hermes-livekit/issues/50

This note uses the approved TTS-only cross-repository tracking exception.
The implementation belongs in hermes-agent; livekit consumes its generic
streaming-TTS interface. Keep contribution branches separate from the
`kortexa-ai/main` runtime branch.

Preserve the whole-response sentence minimum when combining related work.
An optional first-emission override lasts until the first nonempty output,
including an idle/end-of-text flush. An empty flush does not consume it.
After that output, restore the configured whole-response minimum. An unset
override inherits that minimum rather than replacing a user's existing value.

Use the same configuration path in the gateway, CLI speaker and speak-stream
WebSocket. Validate split deltas, multiple sentences in one delta, empty and
nonempty flushes, invalid configuration, text preservation and real consumer
propagation. Run the canonical hermetic test runner with the dependencies
present; missing optional packages must not be mistaken for passing coverage.

Thresholds do not add sentence boundaries. Keep pure-CJK splitter work separate
and preserve the regression that records its current flush-only behavior.

Contribute to another author's branch by agreement and pull request. Retain
authorship, review against the author's exact base, and do not mix upstream
file reorganizations with changes to the deployed runtime. Keep validation,
branch/PR state and any project-adapter blocker in the tracking issue.
