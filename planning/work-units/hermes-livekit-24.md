# Rebrand the realtime gateway

Work item: <https://github.com/kortexa-ai/hermes-realtime/issues/24>
Installation and validation: <https://github.com/kortexa-ai/hermes-realtime/issues/82>
Broker capability: <https://github.com/kortexa-ai/factory/issues/96>

The repository/distribution is `hermes-realtime`, the import and native plugin
subdirectory is `hermes_realtime`, and the Hermes plugin name is `realtime`.
The `livekit` and `realtime` platform identifiers and wire protocols stay stable.
The package and both manifests use version 0.5.0. Existing work-unit filenames
retain their historical issue coordinates; GitHub redirects their old links.

The README defines the explicit native and pip migration and rollback paths.
Preserve repository identity 1239404044 and its history during the GitHub rename.
The user authorized personal gh credentials for this rename because the broker
has no rename command. Use the normal broker for issue and project operations.

Validate package discovery, both registered platforms, the vanilla and Kortexa
Hermes test lanes, and the migrated profile gateways. Keep execution evidence
on the linked issues.
