# Live voice transcripts

Owning issue: https://github.com/kortexa-ai/hermes-livekit/issues/48
Client companion: https://github.com/kortexa-ai/hermes-desk/issues/6

Use Hermes's existing native text-frame transport for both voice adapters.
Bind a consumer's empty seed to the active response and reject late frames
after cancellation, replacement or disconnection. Publish incremental audio
transcript events, with a replacement snapshot when a draft is revised, and
reconcile the authoritative final text on the same item. Text finalization
must not close the audio response or drain, delay or replay speech.

Test the actual GatewayStreamConsumer and shared protocol with direct and
conference endpoints, streaming updates, final reconciliation, response and
chat isolation, and cancellation while audio is active. Run the plugin suite
and the real TurnRunner's per-platform streaming-policy wiring. Enable
`display.platforms.realtime.streaming` and `display.platforms.livekit.streaming`
for Mira without changing other clients' policy. Qualify live transcript
event ordering on Mira. Deliver through Git and
restart only the managed Mira gateway; rollback is a focused Git revert and
removing those two profile overrides followed by the same targeted restart.
No Hermes core or WPE rebuild is required.

Keep measurements, review and deployment state on the owning GitHub issue.
