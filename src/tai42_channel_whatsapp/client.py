"""Outbound WhatsApp (Meta Graph) send.

One endpoint: ``POST {api}/{phone_number_id}/messages``, Bearer auth, JSON body,
via the kit's pooled ``HttpxClient``. Exactly ONE send attempt per message — a
blind retry of an ambiguous failure risks messaging the human twice. Any failure
raises ``ChannelDeliveryError``.
"""

from __future__ import annotations

import httpx
from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelDeliveryError
from tai42_kit.clients.impl.http import HttpxClient

from tai42_channel_whatsapp.settings import require_delivery_secret, whatsapp_settings


async def send_message(phone_number_id: str, to: str, body: str) -> str:
    """Send one WhatsApp text message FROM ``phone_number_id`` TO ``to``; return
    its ``wamid`` (``messages[0].id``).

    Raises ``ChannelDeliveryError`` on a missing access token (checked before any
    network work), transport failure, a non-2xx response, or a 2xx that carries no
    message id. Never retries.
    """
    settings = whatsapp_settings()
    access_token = require_delivery_secret(settings.access_token, "CHANNEL_WHATSAPP_ACCESS_TOKEN")
    url = f"{settings.api_base_url}/{phone_number_id}/messages"
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with tai42_app.clients.client_ctx(HttpxClient, timeout=settings.http_timeout_seconds) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise ChannelDeliveryError(f"WhatsApp send failed in transport: {exc!r}") from exc
    if response.status_code not in (200, 201):
        raise ChannelDeliveryError(
            f"WhatsApp rejected the send: HTTP {response.status_code}: {_error_detail(response)}"
        )
    messages = response.json().get("messages") or []
    if not messages or not messages[0].get("id"):
        raise ChannelDeliveryError("WhatsApp accepted the send but returned no message id")
    return messages[0]["id"]


def _error_detail(response: httpx.Response) -> str:
    """Meta's ``error.code``/``error.message`` when the body is JSON, else raw
    text; bounded to 500 chars so an HTML error page cannot flood the exception."""
    try:
        payload = response.json()
    except ValueError:
        return response.text[:500]
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    return f"code={error.get('code')} message={error.get('message')!r}"[:500]
