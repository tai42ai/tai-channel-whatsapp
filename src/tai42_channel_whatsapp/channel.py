"""The WhatsApp channel: ``deliver`` sends one question, ``notify`` sends
one fire-and-forget message.

Tier-1 formats (``confirm``, ``external``) carry the callback_url as a tappable
link and store NO correlation — the human answers via the callback door; confirm
MUST take this path (the door accepts only the link-tap bool). Tier-2 (``text``,
``select``) expect a typed WhatsApp reply matched through the correlation store;
the reservation is written BEFORE the send so a reply racing the send response is
still matchable, and a failed send releases the reservation and raises.

Freeform text is accepted only inside WhatsApp's 24-hour customer-service window;
outside it Meta rejects the send (synchronously as a ``ChannelDeliveryError``, or
asynchronously via a ``failed`` delivery-status webhook).

The recipient allowlist governs ask_user deliveries; a bridge reply carries a
``sender_identity`` and goes solely to the address that initiated the
conversation, bypassing the allowlist.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from tai42_contract.channels import ChannelDelivery, ChannelDeliveryError, ChannelNotification

from tai42_channel_whatsapp.client import send_message
from tai42_channel_whatsapp.correlation import release_pending, reserve_pending
from tai42_channel_whatsapp.settings import (
    WhatsAppSettings,
    require_delivery_setting,
    whatsapp_settings,
)

# Tier-1 answer formats resolve via the callback link, not a WhatsApp reply.
_TIER1_FORMATS = frozenset({"confirm", "external"})


def _render_question(delivery: ChannelDelivery) -> str:
    """The message body for a Tier-2 ask (``text`` or ``select``): the question,
    plus numbered options for a select ask."""
    lines = [delivery.question]
    if delivery.options:
        lines.extend(f"{index}. {option}" for index, option in enumerate(delivery.options, start=1))
        lines.append("Reply with the text of one option.")
    return "\n".join(lines)


def _render_link(delivery: ChannelDelivery) -> str:
    """The message body for a Tier-1 ask (``confirm`` or ``external``): the
    question plus the tappable callback link."""
    return f"{delivery.question}\n\nAnswer here: {delivery.callback_url}"


def _resolve_target(settings: WhatsAppSettings, requested: str | None) -> str:
    """The destination ``wa_id`` for an ask_user send.

    A caller-requested recipient must be on ``CHANNEL_WHATSAPP_ALLOWED_RECIPIENTS``
    or is refused loudly (fail closed). This channel has no operator default
    recipient — the human's ``wa_id`` is always caller-supplied, so a request with
    none is refused loudly too.
    """
    if requested is None:
        raise ChannelDeliveryError(
            "no recipient requested and this channel has no default recipient; "
            "set a wa_id on CHANNEL_WHATSAPP_ALLOWED_RECIPIENTS and request it"
        )
    if requested not in set(settings.allowed_recipients):
        raise ChannelDeliveryError(
            f"recipient {requested!r} is not on CHANNEL_WHATSAPP_ALLOWED_RECIPIENTS; refusing to send"
        )
    return requested


class WhatsAppChannel:
    """Satisfies the ``tai42_contract.channels.Channel`` protocol."""

    async def deliver(self, delivery: ChannelDelivery) -> None:
        """Resolve the destination ``wa_id``, then push the question to it.

        Recipient policy is ``_resolve_target``'s (fail closed on an unlisted or
        absent request). An ask already past its deadline is refused loudly for
        every format before any reservation or send.
        """
        settings = whatsapp_settings()
        phone_number_id = require_delivery_setting(
            settings.default_phone_number_id, "CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID"
        )
        target = _resolve_target(settings, delivery.recipient)

        # Refuse a question whose answer budget is already spent, before any
        # reservation or HTTP work.
        if math.ceil((delivery.timeout_at - datetime.now(UTC)).total_seconds()) <= 0:
            raise ChannelDeliveryError(
                f"interaction {delivery.interaction_id} already timed out "
                f"(timeout_at={delivery.timeout_at.isoformat()}); nothing was sent"
            )

        if delivery.answer_format in _TIER1_FORMATS:
            # Tier-1: answered via the callback link, so no correlation is stored.
            await send_message(phone_number_id=phone_number_id, to=target, body=_render_link(delivery))
            return

        # Reserve before send: a fast reply's webhook can beat the send response,
        # and this enforces one-pending-per-pair before any network cost.
        await reserve_pending(
            phone_number_id=phone_number_id,
            wa_id=target,
            callback_url=delivery.callback_url,
            timeout_at=delivery.timeout_at,
        )
        try:
            await send_message(phone_number_id=phone_number_id, to=target, body=_render_question(delivery))
        except Exception:
            # Send failed — free the pair instead of holding it until its TTL.
            await release_pending(phone_number_id=phone_number_id, wa_id=target)
            raise

    async def notify(self, notification: ChannelNotification) -> list[str]:
        """Send one fire-and-forget message; raise ``ChannelDeliveryError`` on any
        failure. Returns the single ``wamid`` the Cloud API assigned the send.

        No reply is expected, so nothing touches the correlation store. Exactly
        one send attempt (a plain return means Meta ACCEPTED it, not that a human
        saw it). ``sender_identity`` set → send FROM that ``phone_number_id`` to
        the recipient, the recipient allowlist not consulted (a bridge reply goes
        to the address that initiated the conversation); unset → the configured
        ``CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID`` with ``_resolve_target``'s
        allowlist enforced.
        """
        settings = whatsapp_settings()
        if notification.sender_identity is not None:
            phone_number_id = notification.sender_identity
            if notification.recipient is None:
                raise ChannelDeliveryError("a bridge reply requires a recipient wa_id; none was provided")
            target = notification.recipient
        else:
            phone_number_id = require_delivery_setting(
                settings.default_phone_number_id, "CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID"
            )
            target = _resolve_target(settings, notification.recipient)
        wamid = await send_message(phone_number_id=phone_number_id, to=target, body=notification.message)
        return [wamid]
