"""Pending-question correlation store (plugin-owned Redis).

A WhatsApp reply carries no thread, so the only correlation key is the pair
(<phone_number_id>, <wa_id>). AT MOST ONE pending question per pair — an atomic
``SET NX`` reservation; a second concurrent question is rejected loudly with
``PendingQuestionExistsError``. The value holds the ``callback_url`` and deadline;
the key TTL is the remaining answer budget, so an expired question cannot capture
a later reply.

``pop_pending`` claims with ``GETDEL`` (atomic — one of two concurrent webhooks
wins). ``restore_pending`` puts a popped question back with its remaining TTL
after a failed forward, itself a ``SET NX`` so it never overwrites a NEW
reservation that took the pair in the gap (refused with a loud log instead).

A handled-``wamid`` set is the replay guard (a redelivered webhook repeats the id).
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime

from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelDeliveryError
from tai42_kit.clients.impl.redis import RedisClient

from tai42_channel_whatsapp.settings import (
    WhatsAppRedisSettings,
    whatsapp_redis_settings,
    whatsapp_settings,
)

logger = logging.getLogger(__name__)


class PendingQuestionExistsError(ChannelDeliveryError):
    """A question is already pending for this pair (one at a time)."""


@dataclass(frozen=True)
class PendingQuestion:
    callback_url: str
    timeout_at: datetime


def _pending_key(phone_number_id: str, wa_id: str) -> str:
    return f"channel:whatsapp:pending:{phone_number_id}:{wa_id}"


def _seen_key(wamid: str) -> str:
    return f"channel:whatsapp:seen:{wamid}"


def _redis_settings() -> WhatsAppRedisSettings:
    """The correlation-store connection, raising a clear config error when unset."""
    settings = whatsapp_redis_settings()
    if not settings.redis_url:
        raise ValueError("WhatsApp channel correlation store is not configured: set CHANNEL_WHATSAPP_REDIS_URL.")
    return settings


def _remaining_seconds(timeout_at: datetime) -> int:
    """Whole seconds until the deadline, raising when it already passed."""
    remaining = math.ceil((timeout_at - datetime.now(UTC)).total_seconds())
    if remaining <= 0:
        raise ChannelDeliveryError(f"question deadline {timeout_at.isoformat()} has already passed")
    return remaining


async def reserve_pending(phone_number_id: str, wa_id: str, callback_url: str, timeout_at: datetime) -> None:
    """Atomically reserve the pair for one question, or raise ``PendingQuestionExistsError``."""
    value = json.dumps({"callback_url": callback_url, "timeout_at": timeout_at.isoformat()})
    ttl = _remaining_seconds(timeout_at)
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        stored = await redis.set(_pending_key(phone_number_id, wa_id), value, nx=True, ex=ttl)
    if not stored:
        raise PendingQuestionExistsError(
            f"a question is already pending for the pair ({phone_number_id}, {wa_id}); "
            "one pending question per pair — answer or let it time out first"
        )


async def release_pending(phone_number_id: str, wa_id: str) -> None:
    """Drop the reservation (the send failed — the human never received the question)."""
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        await redis.delete(_pending_key(phone_number_id, wa_id))


async def pop_pending(phone_number_id: str, wa_id: str) -> PendingQuestion | None:
    """Atomically claim-and-remove the pending question; ``None`` when there is
    none (``GETDEL`` — a concurrent duplicate webhook gets ``None``)."""
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        raw = await redis.getdel(_pending_key(phone_number_id, wa_id))
    if raw is None:
        return None
    data = json.loads(raw)
    return PendingQuestion(
        callback_url=data["callback_url"],
        timeout_at=datetime.fromisoformat(data["timeout_at"]),
    )


async def restore_pending(phone_number_id: str, wa_id: str, question: PendingQuestion) -> None:
    """Put a popped question back with its remaining TTL after a failed forward.

    A ``SET NX``: if a NEW question reserved the pair in the gap, the restore is
    refused with a loud log rather than misrouting the new question's reply. A
    question past its deadline is not restored.
    """
    remaining = math.ceil((question.timeout_at - datetime.now(UTC)).total_seconds())
    if remaining <= 0:
        return
    value = json.dumps({"callback_url": question.callback_url, "timeout_at": question.timeout_at.isoformat()})
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        stored = await redis.set(_pending_key(phone_number_id, wa_id), value, nx=True, ex=remaining)
    if not stored:
        logger.error(
            "could not restore the pending question for (%s, %s): a new question has since "
            "reserved the pair; the old ask will resolve by its own timeout",
            phone_number_id,
            wa_id,
        )


async def already_seen(wamid: str) -> bool:
    """Whether this ``wamid`` was already handled (webhook retry or replay)."""
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        return bool(await redis.exists(_seen_key(wamid)))


async def mark_seen(wamid: str) -> None:
    """Remember a handled ``wamid`` for the configured dedupe window."""
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        await redis.set(_seen_key(wamid), "1", ex=whatsapp_settings().dedupe_ttl)
