# Restore LiveKit after the Hermes aiohttp update

Work item: <https://github.com/kortexa-ai/hermes-livekit/issues/81>

Hermes' `aiohttp==3.14.5` requirement conflicts with the plugin's `3.14.3`
pin. Both distribution and native-plugin metadata must use the host's
reviewed version and carry the same patch release number.

The shared gateway also needs profile-scoped connection reads in discovery,
validation, and adapter construction. Plugin registration must defer media
imports so the host can finish discovery before starting either transport.
Regression tests cover populated and missing profile settings and cold
registration without loading media adapters.

Validation covers a fresh PM-owned core-plus-plugin dependency environment,
the vanilla and Kortexa test lanes, and two profile copies in the shared
gateway. Delivery installs the exact reviewed revision for default and Mira,
re-enables the plugin through PM admission, and restarts the shared gateway.
Authenticated discovery, LiveKit room joins, and synthetic speech with
received response audio establish runtime behavior. Preserve credentials
and profile settings; leave unrelated services untouched.

Keep execution results and delivery evidence on the linked issue.
