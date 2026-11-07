# Portal automation and the owner's network

Owner incident, 2026-11-07: a Yaencontre restriction page named public IP
185.158.127.131. Read-only IP checks on both Mac mini and MacBook returned that
same address. A separate machine or browser profile did not isolate traffic.

The retained `data/macbook-browser-evidence-20261105-closeout-checkpoint.jsonl`
contains 802 Yaencontre navigations between 06:45:33 and 07:59:39 Europe/Madrid
on November 5. The last response is HTTP 403, classified `blocked`. This is
evidence of substantial automation on the shared route, not proof of the
portal's undisclosed cause or expiry of today's restriction. The archived
reader is stopped and its `reader.pause` marker is present on the MacBook.
Do not remove that marker or resume the archived queue.

## Before any future collector

- Keep the owner's ordinary Chrome profile for the owner. Do not attach a
  bulk reader through AppleScript, DevTools or another automation surface.
  `tools/browser.sh` is for app UI checks, not bulk portal collection.
- Use a dedicated collector profile and an explicitly configured network
  exit. Verify its public IP against both the owner's route and the collector's
  actual runtime, including the Docker namespace when applicable, using a
  neutral IP service. Distinct machines, ports or profiles prove no isolation.
- The collector must fail closed when that route is unavailable. An HTTP
  client must not fall back to its ordinary session/environment route; a
  browser must not silently launch without its proxy. Verify that failure
  locally before enabling a job. Do not reuse the Codex app's own proxy.
- Keep one reader per portal, reuse completed observations and pace every
  request. Stop the source on the first challenge, 401, 403 or 429. Persist the
  stop across jobs and restarts. Do not automatically probe a refused source,
  switch IPs, transfer to another machine or escalate into a browser.

An isolated exit prevents background work from sharing the owner's network
identity; it is not permission to bypass a portal refusal. No collector may be
enabled against a refused portal merely because an alternate exit is available.

## Current supported path

Yaencontre ingestion reads alert-email cards, including their captured photo
URLs, without fetching listing pages. Property availability already excludes
this source. `ListingStatusService.observe` now refuses Yaencontre before any
HTTP operation, covering direct and legacy Land callers as well. A new service,
reset breaker or process restart does not remove that stop. This does not block
the owner's manual browsing or rewrite stored listing status, photos or votes.

Restriction-page text is a failed check even if delivered with HTTP 200. It
must never confirm that a listing is active or removed.

## Amnezia on macOS

Installed on both machines at the incident check: 5.0.3. Presence of the app
is not evidence that the collector is routed through it. The official
[split tunneling documentation](https://docs.amnezia.org/documentation/instructions/vpn-split-tunneling/)
says macOS supports IP-based rules, not per-application rules. Such rules can
route the owner's browser and the collector together and do not by themselves
provide the required separation.

The documented [Local Proxy mode](https://docs.amnezia.org/documentation/instructions/local-proxy/)
is an option for a supported Premium setup, not an established capability of
these installed profiles. A self-hosted dedicated proxy or collector VPN
namespace is another architecture. No VPN settings, subscriptions, credentials
or system-wide routes were changed during this incident diagnosis. No portal
request is needed to prepare or test route isolation.
