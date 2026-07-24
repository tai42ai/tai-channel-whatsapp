"""The pending-question correlation store — reservation, claim, restore, dedupe."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest
from tai42_contract.channels import ChannelDeliveryError

from tai42_channel_whatsapp_cloud.correlation import (
    PendingQuestion,
    PendingQuestionExistsError,
    already_seen,
    mark_seen,
    pop_pending,
    release_pending,
    reserve_pending,
    restore_pending,
)
from tests.conftest import FakeRedis

pytestmark = pytest.mark.usefixtures("whatsapp_env")

_PNID = "10000000000001"
_WA = "15559990001"
_KEY = f"channel:whatsapp-cloud:pending:{_PNID}:{_WA}"
_CALLBACK = "https://app.example/api/interactions/callback/ticket-1"


def _deadline(seconds: float = 300) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


async def test_reserve_pop_round_trip(fake_redis: FakeRedis):
    timeout_at = _deadline()
    await reserve_pending(_PNID, _WA, _CALLBACK, timeout_at)

    popped = await pop_pending(_PNID, _WA)

    assert popped == PendingQuestion(callback_url=_CALLBACK, timeout_at=timeout_at)


async def test_reservation_ttl_is_remaining_budget(fake_redis: FakeRedis):
    before = datetime.now(UTC)
    timeout_at = _deadline(120)
    await reserve_pending(_PNID, _WA, _CALLBACK, timeout_at)
    after = datetime.now(UTC)

    ttl = fake_redis.ttls[_KEY]
    assert math.ceil((timeout_at - after).total_seconds()) <= ttl <= math.ceil((timeout_at - before).total_seconds())


async def test_double_reserve_rejected(fake_redis: FakeRedis):
    await reserve_pending(_PNID, _WA, _CALLBACK, _deadline())

    with pytest.raises(PendingQuestionExistsError, match="already pending"):
        await reserve_pending(_PNID, _WA, "https://app.example/other", _deadline())


async def test_reserve_past_deadline_raises_and_stores_nothing(fake_redis: FakeRedis):
    with pytest.raises(ChannelDeliveryError, match="already passed"):
        await reserve_pending(_PNID, _WA, _CALLBACK, _deadline(-1))

    assert not fake_redis.store  # nothing reserved for an expired budget


async def test_pop_claim_is_atomic(fake_redis: FakeRedis):
    await reserve_pending(_PNID, _WA, _CALLBACK, _deadline())

    assert await pop_pending(_PNID, _WA) is not None
    assert await pop_pending(_PNID, _WA) is None


async def test_release_frees_the_pair(fake_redis: FakeRedis):
    await reserve_pending(_PNID, _WA, _CALLBACK, _deadline())
    await release_pending(_PNID, _WA)

    await reserve_pending(_PNID, _WA, "https://app.example/next", _deadline())


async def test_restore_uses_remaining_ttl(fake_redis: FakeRedis):
    timeout_at = _deadline(600)
    await reserve_pending(_PNID, _WA, _CALLBACK, timeout_at)
    original_ttl = fake_redis.ttls[_KEY]
    popped = await pop_pending(_PNID, _WA)
    assert popped is not None

    before = datetime.now(UTC)
    await restore_pending(_PNID, _WA, popped)
    after = datetime.now(UTC)

    restored_ttl = fake_redis.ttls[_KEY]
    assert restored_ttl <= original_ttl
    assert (
        math.ceil((timeout_at - after).total_seconds())
        <= restored_ttl
        <= math.ceil((timeout_at - before).total_seconds())
    )
    assert await pop_pending(_PNID, _WA) == popped


async def test_restore_past_deadline_stores_nothing(fake_redis: FakeRedis):
    stale = PendingQuestion(callback_url=_CALLBACK, timeout_at=datetime.now(UTC) - timedelta(seconds=1))

    await restore_pending(_PNID, _WA, stale)

    assert not fake_redis.store


async def test_restore_never_clobbers_a_new_reservation(fake_redis: FakeRedis, caplog: pytest.LogCaptureFixture):
    await reserve_pending(_PNID, _WA, _CALLBACK, _deadline())
    popped = await pop_pending(_PNID, _WA)
    assert popped is not None
    new_callback = "https://app.example/api/interactions/callback/ticket-2"
    await reserve_pending(_PNID, _WA, new_callback, _deadline())

    with caplog.at_level("ERROR"):
        await restore_pending(_PNID, _WA, popped)

    # The NEW question's reservation is intact — never a blind overwrite.
    still_pending = await pop_pending(_PNID, _WA)
    assert still_pending is not None
    assert still_pending.callback_url == new_callback
    assert any("could not restore" in record.message for record in caplog.records)


async def test_seen_round_trip_uses_dedupe_ttl(fake_redis: FakeRedis):
    assert await already_seen("wamid.ABC") is False

    await mark_seen("wamid.ABC")

    assert await already_seen("wamid.ABC") is True
    assert fake_redis.ttls["channel:whatsapp-cloud:seen:wamid.ABC"] == 172_800


async def test_missing_redis_url_raises_on_every_store_function(fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_WHATSAPP_CLOUD_REDIS_URL")
    reset_all_settings()

    question = PendingQuestion(callback_url=_CALLBACK, timeout_at=_deadline())
    for call in (
        reserve_pending(_PNID, _WA, _CALLBACK, _deadline()),
        release_pending(_PNID, _WA),
        pop_pending(_PNID, _WA),
        restore_pending(_PNID, _WA, question),
        already_seen("wamid.ABC"),
        mark_seen("wamid.ABC"),
    ):
        with pytest.raises(ValueError, match="CHANNEL_WHATSAPP_CLOUD_REDIS_URL"):
            await call
