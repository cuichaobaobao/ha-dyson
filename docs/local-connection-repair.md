# Local connection repair

Scope: local discovery and connection lifecycle, with the existing integration
domain, entity identities and device controls preserved. IP remains optional.
Cloud login is unchanged and is only needed when retrieving credentials from
MyDyson; Wi-Fi sticker/manual credentials continue to support local setup.

Reviewed upstream reports and proposals:
- https://github.com/libdyson-wg/ha-dyson/issues/304 (withdrawn releases)
- https://github.com/libdyson-wg/ha-dyson/issues/211 (IP changes)
- https://github.com/libdyson-wg/ha-dyson/issues/263 (connection/topic failures)
- https://github.com/libdyson-wg/ha-dyson/pull/317 (variant resolution)
- https://github.com/libdyson-wg/ha-dyson/pull/320 (mapping changes)

The repair owns only its discovery browser, never Home Assistant's shared
Zeroconf instance. Discovery is bounded and repeated on setup/retry/reload.
An unavailable device raises ConfigEntryNotReady rather than returning a
successful setup with no entities. A DHCP change during an already-running
MQTT session may still require reloading the integration.

Blocking discovery/connect/disconnect work runs in HA's executor. Configuration
validation disconnects its temporary MQTT client. Platforms are forwarded only
after connection succeeds, and failed platform unload preserves live resources.

Explicit supported MQTT variant codes are retained for newly added devices.
Existing stored device types are not migrated automatically.

Validation runs only on GitHub-hosted Actions, using actual installed HA and
libdyson-neon packages with synthetic device metadata and mocked network
boundaries. These tests do not establish connectivity to a physical purifier
or validate Dyson cloud authentication. No release or HA deployment is part
of this branch.
