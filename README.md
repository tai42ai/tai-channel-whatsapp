# tai42-channel-whatsapp

[![CI](https://github.com/tai42ai/tai-channel-whatsapp/actions/workflows/ci.yml/badge.svg)](https://github.com/tai42ai/tai-channel-whatsapp/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

A Meta **WhatsApp API** channel plugin for the TAI ecosystem. It delivers
an `ask_user` question to a human on WhatsApp through the Cloud (Graph) API and
bridges the human's reply back into the interactions store — so an agent can
reach a person out-of-band instead of only showing the question in the Studio
inbox. It implements the `tai42_contract.channels.Channel` protocol and registers
under the name `"whatsapp"`. It is for numbers hosted directly on Meta's
Cloud API (no BSP/Twilio in front); the Twilio-hosted path is the sibling
`tai42-channel-twilio`.

## The TAI ecosystem

TAI is an open-source runtime for MCP tools, agents, and workflows. A `Channel`
is "how a question reaches a human" — a pluggable deliverer the runtime resolves
by name when `ask_user` is called with `channel=...`. This package is one such
deliverer (WhatsApp API); siblings back the same contract with Twilio,
Telegram, or Slack. The ecosystem is open-ended: any package can back the same
contract, so this repo is this plugin's own full doc home, and the documentation
site covers the platform-level story:

- Interactions concept: https://tai42.ai/concepts/interactions
- Build a channel plugin (author guide): https://tai42.ai/guides/authors/channel
- Ecosystem catalog: https://tai42.ai/reference/catalog

Its only tai-* dependencies are `tai42-contract` (the `Channel` protocol,
`ChannelDelivery`, `ChannelDeliveryError`, and the `tai42_app` handle) and
`tai42-kit[redis]` (`HttpxClient`, `RedisClient`, `TaiBaseSettings`, and the
settings cache). Beyond those it depends on `httpx`, `starlette`, and
`pydantic` / `pydantic-settings`. There is **no Meta SDK**: the send is one
Bearer-auth JSON POST over `httpx`, and webhook signature validation is a few
lines of stdlib `hmac`/`hashlib`.

## Install

Requires **Python 3.13+**. Nothing is on PyPI yet, so install from source — clone
this repo alongside your `tai42-skeleton` checkout and add it as an editable
dependency of the environment that runs the server:

```bash
git clone https://github.com/tai42ai/tai-channel-whatsapp
cd tai-skeleton   # or your own app checkout
uv add --editable ../tai-channel-whatsapp   # once published: uv add tai42-channel-whatsapp
```

## Discovery

The runtime discovers this plugin through the manifest's `channel_modules` key:

```yaml
channel_modules: ["tai42_channel_whatsapp"]
```

At app load the runtime imports every module under the package, and
`register.py` fires the registrations as its import side-effect: the
`"whatsapp"` channel on `tai42_app.channels`, and — via the `inbound`
import — the unauthenticated webhook route on `tai42_app.http`. A bare
`import tai42_channel_whatsapp` registers **nothing** — the package is
library-safe; only the register module carries the side-effect.

## Configuration

Settings are read from the `CHANNEL_WHATSAPP_` environment group (see
`WhatsAppSettings` / `WhatsAppRedisSettings`):

| Env var | Required | Meaning |
|---|---|---|
| `CHANNEL_WHATSAPP_ACCESS_TOKEN` | yes | Graph API access token (`SecretStr`) — the Bearer credential for the send |
| `CHANNEL_WHATSAPP_APP_SECRET` | yes | Meta app secret (`SecretStr`) — the `X-Hub-Signature-256` HMAC key for inbound webhooks |
| `CHANNEL_WHATSAPP_VERIFY_TOKEN` | yes | Shared token (`SecretStr`) echoed during Meta's GET webhook verification handshake |
| `CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID` | for ask_user | The `phone_number_id` messages are sent FROM when no sender identity is routed |
| `CHANNEL_WHATSAPP_ALLOWED_RECIPIENTS` | for ask_user | Whitelist of `wa_id`s a caller-requested recipient must be on — comma-separated or a JSON list; an unlisted request is refused loudly |
| `CHANNEL_WHATSAPP_API_BASE_URL` | no | Graph API origin + pinned version (default `https://graph.facebook.com/v23.0`) |
| `CHANNEL_WHATSAPP_REDIS_URL` | yes | Correlation store (plugin-owned Redis) |
| `CHANNEL_WHATSAPP_REDIS_MAX_CONNECTIONS` … | no | The rest of the kit `RedisConnectionSettings` fields, same names under this prefix |
| `CHANNEL_WHATSAPP_HTTP_TIMEOUT_SECONDS` | no (30.0) | Outbound send + answer-forward timeout, seconds |
| `CHANNEL_WHATSAPP_DEDUPE_TTL` | no (172800) | Seen-`wamid` replay-guard window, seconds |

One credential (`ACCESS_TOKEN` + `APP_SECRET`) serves many `phone_number_id`s: a
bridge reply is sent from the exact `phone_number_id` that received the inbound
message, while an ask_user delivery is sent from
`CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID`. Recipient policy is
operator-owned: a caller may request a `wa_id` per ask, but it is sent to only if
it is on `CHANNEL_WHATSAPP_ALLOWED_RECIPIENTS` (fail closed — an unlisted or
absent recipient raises, nothing is sent). Secrets live only in the environment.

Two steps happen **out-of-band** (the plugin never mutates Meta app configuration
at startup):

1. In the Meta App dashboard, point the WhatsApp webhook callback URL at
   `{public base URL}/api/channels/whatsapp/inbound` and set the verify
   token to `CHANNEL_WHATSAPP_VERIFY_TOKEN`. Meta issues a `GET` handshake
   the route answers by echoing `hub.challenge`.
2. Subscribe the app to the `messages` webhook field so message and delivery-status
   events reach the same `POST` endpoint.

## How a human answers

A `text` or `select` question arrives as a normal WhatsApp message (a select ask
lists its options numbered). The human **just replies** — no code to quote, no
prefix. Correlation is fully out-of-band: any reply from the recipient resolves
the `(phone_number_id, wa_id)` pair's pending question, so one question can be
pending per pair at a time; a second concurrent one is rejected loudly.

A `confirm` or `external` question arrives as a tappable link and is answered in
the browser via the callback door — no WhatsApp reply is expected or matched, and
it never consumes the pair.

## Delivery statuses

WhatsApp reports delivery asynchronously: a send returns `2xx` with a `wamid`, and
a later `statuses` webhook carries `sent`/`delivered`/`read`/`failed`. The same
`POST` endpoint records those receipts through the interactions facet — `failed`
(e.g. a message outside the 24-hour session window, error 131047) marks the
answer failed loudly; `sent`/`delivered` confirm it; `read` is informational and
ignored. A status for a message the bridge does not track is acknowledged, never
retried.

## Security

- Inbound requests authenticate via `X-Hub-Signature-256`:
  `sha256=` + hex(HMAC-SHA256(app_secret, raw body)), validated **fail-closed**
  with a constant-time compare **before the body is parsed**. A missing/empty app
  secret is an operator error that raises loudly (logged 500) — never a soft 401
  that reads like a bad signature.
- The GET verification handshake echoes `hub.challenge` only when
  `hub.verify_token` matches the configured token under a constant-time compare.
- Meta's signature scheme carries no timestamp, so a captured request validates
  forever; the `wamid` dedupe window (48h default) plus HTTPS are the replay
  guards.
- The access token, app secret, and verify token are `SecretStr` — never in a
  repr, log line, or traceback; the plaintext is read only at the Bearer-auth and
  HMAC seams.
- The unauthenticated route bounds its body read (1 MiB → 413) before any
  signature work.

## v1 limits

| Limit | Consequence | Future |
|---|---|---|
| Recipients fixed by operator env (`_ALLOWED_RECIPIENTS`) | A question can only reach a whitelisted `wa_id` — no dynamic/unlisted destinations, and no operator default recipient | Directory-backed recipient resolution |
| One pending question per `(phone_number_id, wa_id)` pair | A second concurrent `ask_user` over this channel fails loudly with `PendingQuestionExistsError` while the first is unanswered/unexpired | — |
| Freeform text only, inside the 24h window | A send outside the human's 24-hour session window is rejected by Meta (error 131047), synchronously as a delivery error or asynchronously as a `failed` status | Approved-template sends |
| Single send attempt | A transient Cloud API outage fails the ask instead of retrying (no idempotency key → a blind retry risks double-messaging) | App-side dedupe + retry |
| Text messages only | Inbound non-text (image, audio, …) is acknowledged and ignored with a debug log; outbound is text | Media messages |
| No timestamp in Meta's signature scheme | Replay of a captured request validates forever for that body; `wamid` dedupe (48h default window) + HTTPS are the guards | — (Meta protocol property) |

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run pyright
```

The live integration suite (`pytest -m integration`) sends real messages via the
WhatsApp API and runs only when the `CHANNEL_WHATSAPP_*` credentials
are present in the environment; it skips cleanly otherwise.

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
