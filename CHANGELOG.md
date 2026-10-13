# Changelog

This fork is maintained at https://github.com/cuichaobaobao/ha-dyson.
Install and update from this repository. Upstream releases are separate.

## 1.7.1 — 2026-10-13

First maintenance release of this fork, based on the upstream 1.7.0 code.

- Keep automatic local discovery; a manually entered IP address remains optional.
- Stop only the Dyson discovery browser, preserving Home Assistant's shared Zeroconf service.
- Run blocking discovery and MQTT connection work outside HA's event loop.
- Close validation connections and clean up failed or cancelled connections.
- Retry undiscovered or offline devices instead of reporting incomplete setup as successful.
- Preserve resources when platform unloading fails.
- Preserve supported MQTT product variants when adding cloud-discovered devices.
- Avoid reading filter values while checking supported capabilities.

Existing entity identities, device control payloads and cloud login are retained.
Regression tests cover HA 2025.12.0 and 2026.10.0 with libdyson-neon 1.6.0.

This release focuses on local purifier connections, especially Big+Quiet.
Physical-device connectivity is not established by automated tests.
The inherited robot-vacuum compatibility issue is outside this release's scope.
A DHCP address change during a running connection may require reloading the integration.

## Maintenance

The default branch is `main`. Temporary repair branches can be used for testing
and merged into this fork; they do not require upstream approval.

For each release, update the manifest version and add a changelog entry.
GitHub-hosted Actions publishes the matching version only after regression
tests, HACS and hassfest pass. An existing release is never overwritten.
