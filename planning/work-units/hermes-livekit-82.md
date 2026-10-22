# Rebrand integration and installed-profile migration

Parent work: <https://github.com/kortexa-ai/hermes-realtime/issues/24>
Work item: <https://github.com/kortexa-ai/hermes-realtime/issues/82>

Update test imports, native discovery fixtures, probes, CI references and
active documents for `hermes_realtime` and the `realtime` plugin identity.
Historical work-unit records keep their original filenames and references.

Back up default and Mira plugin configuration and installations before migration.
Use Hermes PM to install the exact reviewed commit for both profiles, preserve
plugin settings and platform credentials, and remove the superseded plugin.
Restart the shared gateway once; verify profile-bound authenticated discovery,
both transport registrations and LiveKit connection state. No Hermes core
source edits or unrelated service changes are required.

Rollback reinstalls the prior pinned commit under its original native path and
restores the saved settings. Keep validation and delivery evidence on the issue.
