# Publish hermes-realtime v0.5.0

Work item: <https://github.com/kortexa-ai/hermes-realtime/issues/83>

Publish the first GitHub release under the new repository and plugin names.
Keep the distribution, native plugin and manifest versions at the unpublished
0.5.0 version. Include the README cleanup and consolidate the existing release
notes under 0.5.0.

Build disposable test environments through Hermes PM for their host OS and
architecture, preserving core Python constraints. Validate against vanilla
Hermes and the explicit Kortexa lane without changing a gateway environment.
Use the current PM package-admission property in the packaging contract test.

Build the wheel and source distribution from the exact reviewed commit, verify
metadata and bundled files, and prepare SHA-256 checksums. Push the immutable
v0.5.0 tag, then create a bot-owned draft, upload its complete asset inventory
and publish it as the latest stable release through the Factory broker.

Keep validation, release receipts and delivery evidence on the issue. The
separate Hermes Desk checkout/configuration audit requires no client change
when its transport endpoints and configuration contract remain compatible.
