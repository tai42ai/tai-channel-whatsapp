"""``WhatsAppChannel.deliver`` and ``.notify`` — outbound JSON payload, tier
split, reservation ordering, the correlation-free notify path, and sender_identity."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from tai42_contract.channels import ChannelDeliveryError, ChannelNotification

from tai42_channel_whatsapp.channel import WhatsAppChannel
from tai42_channel_whatsapp.correlation import PendingQuestionExistsError
from tests.conftest import ALLOWED_A, ALLOWED_B, PHONE_NUMBER_ID, FakeHttpx, FakeRedis, make_delivery, response

pytestmark = pytest.mark.usefixtures("whatsapp_env")

_MESSAGES_URL = f"https://graph.facebook.com/v23.0/{PHONE_NUMBER_ID}/messages"


def _accepted(wamid: str = "wamid.OUT") -> httpx.Response:
    return response(200, json={"messages": [{"id": wamid}]})


async def test_deliver_sends_exact_outbound_json(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())

    await WhatsAppChannel().deliver(make_delivery())

    assert len(fake_httpx.calls) == 1
    call = fake_httpx.calls[0]
    assert call["url"] == _MESSAGES_URL
    assert call["json"] == {
        "messaging_product": "whatsapp",
        "to": ALLOWED_A,
        "type": "text",
        "text": {"body": "Deploy to prod?"},
    }
    assert call["headers"] == {"Authorization": "Bearer test-access-token"}


async def test_messages_url_derives_from_api_base_url(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("CHANNEL_WHATSAPP_API_BASE_URL", "http://127.0.0.1:9098/v23.0")
    reset_all_settings()
    fake_httpx.responses.append(_accepted())

    await WhatsAppChannel().deliver(make_delivery())

    assert fake_httpx.calls[0]["url"] == f"http://127.0.0.1:9098/v23.0/{PHONE_NUMBER_ID}/messages"


async def test_select_rendering_numbers_options(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())

    await WhatsAppChannel().deliver(
        make_delivery(answer_format="select", options=["staging", "production"], question="Which env?")
    )

    body = fake_httpx.calls[0]["json"]["text"]["body"]
    assert body == "Which env?\n1. staging\n2. production\nReply with the text of one option."


async def test_reserve_precedes_send(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())

    await WhatsAppChannel().deliver(make_delivery())

    kinds = [kind for kind, _ in fake_redis.events]
    assert kinds.index("redis_set") < kinds.index("http_post")


async def test_second_concurrent_question_rejected_loudly(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())
    await WhatsAppChannel().deliver(make_delivery())

    with pytest.raises(PendingQuestionExistsError) as excinfo:
        await WhatsAppChannel().deliver(make_delivery(interaction_id="int-2"))

    assert isinstance(excinfo.value, ChannelDeliveryError)
    assert len(fake_httpx.calls) == 1  # no second send happened


async def test_send_rejection_raises_and_releases_reservation(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(response(400, json={"error": {"code": 131009, "message": "Invalid recipient"}}))

    with pytest.raises(ChannelDeliveryError, match=r"HTTP 400.*131009"):
        await WhatsAppChannel().deliver(make_delivery())

    # The pair was released — a follow-up deliver succeeds.
    fake_httpx.responses.append(_accepted())
    await WhatsAppChannel().deliver(make_delivery())


async def test_send_rejection_detail_bounded_for_non_json_body(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(response(502, text="<html>" + "x" * 2000 + "</html>"))

    with pytest.raises(ChannelDeliveryError, match="HTTP 502") as excinfo:
        await WhatsAppChannel().deliver(make_delivery())

    assert len(str(excinfo.value)) < 600  # detail capped at 500 chars


async def test_transport_failure_raises_and_releases_reservation(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(httpx.ConnectError("boom"))

    with pytest.raises(ChannelDeliveryError, match="transport") as excinfo:
        await WhatsAppChannel().deliver(make_delivery())

    assert isinstance(excinfo.value.__cause__, httpx.ConnectError)
    fake_httpx.responses.append(_accepted())
    await WhatsAppChannel().deliver(make_delivery())


async def test_allowlisted_recipient_sends_and_correlates_to_it(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())

    await WhatsAppChannel().deliver(make_delivery(recipient=ALLOWED_B))

    assert fake_httpx.calls[0]["json"]["to"] == ALLOWED_B
    # The reservation is keyed per recipient — the pair uses the actual target.
    assert list(fake_redis.store) == [f"channel:whatsapp:pending:{PHONE_NUMBER_ID}:{ALLOWED_B}"]


async def test_no_recipient_rejected_no_default(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # This channel has no operator default recipient — a recipientless ask is refused.
    with pytest.raises(ChannelDeliveryError, match="no default recipient"):
        await WhatsAppChannel().deliver(make_delivery(recipient=None))

    assert not fake_httpx.calls
    assert not fake_redis.store


async def test_unlisted_recipient_rejected_and_nothing_sent(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    with pytest.raises(ChannelDeliveryError, match="not on CHANNEL_WHATSAPP_ALLOWED_RECIPIENTS"):
        await WhatsAppChannel().deliver(make_delivery(recipient="15559999999"))

    assert not fake_httpx.calls  # nothing sent
    assert not fake_redis.store  # nothing reserved
    assert not [event for event in fake_redis.events if event[0] == "redis_set"]


async def test_json_allowlist_padded_entry_matches_unpadded_recipient(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("CHANNEL_WHATSAPP_ALLOWED_RECIPIENTS", '[" 15551230001 ", ""]')
    reset_all_settings()

    fake_httpx.responses.append(_accepted())
    await WhatsAppChannel().deliver(make_delivery(recipient=ALLOWED_A))

    assert fake_httpx.calls[0]["json"]["to"] == ALLOWED_A


async def test_empty_allowlist_rejects_any_requested_recipient(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_WHATSAPP_ALLOWED_RECIPIENTS")
    reset_all_settings()

    with pytest.raises(ChannelDeliveryError, match="not on CHANNEL_WHATSAPP_ALLOWED_RECIPIENTS"):
        await WhatsAppChannel().deliver(make_delivery(recipient=ALLOWED_A))

    assert not fake_httpx.calls
    assert not fake_redis.store


async def test_deliver_missing_phone_number_id_raises_before_any_work(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID")
    reset_all_settings()

    with pytest.raises(ChannelDeliveryError, match="set CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID"):
        await WhatsAppChannel().deliver(make_delivery())

    assert not fake_httpx.calls  # nothing sent
    assert not fake_redis.store  # nothing reserved
    assert not fake_redis.events  # no HTTP call and no Redis write at all


async def test_deliver_missing_access_token_raises_and_releases_reservation(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_WHATSAPP_ACCESS_TOKEN")
    reset_all_settings()

    with pytest.raises(ChannelDeliveryError, match="set CHANNEL_WHATSAPP_ACCESS_TOKEN"):
        await WhatsAppChannel().deliver(make_delivery())

    assert not fake_httpx.calls  # checked before any network work — nothing sent
    assert not fake_redis.store  # the Tier-2 reservation was released, the pair is free


async def test_past_deadline_raises_before_any_send(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    with pytest.raises(ChannelDeliveryError, match="already timed out"):
        await WhatsAppChannel().deliver(make_delivery(timeout_at=datetime.now(UTC) - timedelta(seconds=1)))

    assert not fake_redis.store
    assert not fake_httpx.calls


@pytest.mark.parametrize("answer_format", ["confirm", "external"])
async def test_tier1_past_deadline_raises_before_any_send(
    answer_format: str, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    with pytest.raises(ChannelDeliveryError, match="already timed out"):
        await WhatsAppChannel().deliver(
            make_delivery(answer_format=answer_format, timeout_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    assert not fake_httpx.calls  # the link would be dead on arrival — nothing sent
    assert not fake_redis.store
    assert not fake_redis.events  # no HTTP call and no reservation attempt at all


async def test_accepted_response_without_id_raises(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(response(200, json={"messages": [{}]}))

    with pytest.raises(ChannelDeliveryError, match="no message id"):
        await WhatsAppChannel().deliver(make_delivery())


async def test_accepted_response_empty_messages_raises(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(response(200, json={"messages": []}))

    with pytest.raises(ChannelDeliveryError, match="no message id"):
        await WhatsAppChannel().deliver(make_delivery())


@pytest.mark.parametrize("answer_format", ["confirm", "external"])
async def test_tier1_link_send_skips_reservation(answer_format: str, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())

    delivery = make_delivery(answer_format=answer_format)
    await WhatsAppChannel().deliver(delivery)

    assert len(fake_httpx.calls) == 1
    body = fake_httpx.calls[0]["json"]["text"]["body"]
    assert delivery.question in body
    assert delivery.callback_url in body  # plain tappable link
    assert not fake_redis.store  # no reservation, no correlation
    assert not [event for event in fake_redis.events if event[0] == "redis_set"]


async def test_notify_sends_plain_body_without_correlation(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted("wamid.N"))

    ids = await WhatsAppChannel().notify(ChannelNotification(message="Deploy finished.", recipient=ALLOWED_A))

    assert ids == ["wamid.N"]
    call = fake_httpx.calls[0]
    assert call["url"] == _MESSAGES_URL
    assert call["json"] == {
        "messaging_product": "whatsapp",
        "to": ALLOWED_A,
        "type": "text",
        "text": {"body": "Deploy finished."},
    }
    # Fire-and-forget: no reservation, no Redis write of any kind.
    assert not fake_redis.store
    assert not [event for event in fake_redis.events if event[0] == "redis_set"]


async def test_notify_returns_wamid(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted("wamid.RET"))

    ids = await WhatsAppChannel().notify(ChannelNotification(message="Done.", recipient=ALLOWED_A))

    assert ids == ["wamid.RET"]


async def test_notify_unlisted_recipient_rejected_and_nothing_sent(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    with pytest.raises(ChannelDeliveryError, match="not on CHANNEL_WHATSAPP_ALLOWED_RECIPIENTS"):
        await WhatsAppChannel().notify(ChannelNotification(message="Heads up.", recipient="15559999999"))

    assert not fake_httpx.calls  # nothing sent
    assert not fake_redis.store


async def test_notify_no_recipient_and_no_sender_identity_rejected(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    with pytest.raises(ChannelDeliveryError, match="no default recipient"):
        await WhatsAppChannel().notify(ChannelNotification(message="Heads up."))

    assert not fake_httpx.calls


async def test_notify_missing_phone_number_id_raises_before_any_work(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID")
    reset_all_settings()

    with pytest.raises(ChannelDeliveryError, match="set CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID"):
        await WhatsAppChannel().notify(ChannelNotification(message="Heads up.", recipient=ALLOWED_A))

    assert not fake_httpx.calls
    assert not fake_redis.store


async def test_notify_missing_access_token_raises_and_nothing_sent(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_WHATSAPP_ACCESS_TOKEN")
    reset_all_settings()

    with pytest.raises(ChannelDeliveryError, match="set CHANNEL_WHATSAPP_ACCESS_TOKEN"):
        await WhatsAppChannel().notify(ChannelNotification(message="Heads up.", recipient=ALLOWED_A))

    assert not fake_httpx.calls  # checked before any network work — nothing sent
    assert not fake_redis.store  # notify never touches the correlation store


async def test_notify_sender_identity_sends_from_it_and_bypasses_allowlist(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # sender_identity present: send FROM that phone_number_id to the recipient
    # VERBATIM, the recipient allowlist NOT consulted (recipient is off-allowlist).
    fake_httpx.responses.append(_accepted("wamid.BRIDGE"))

    ids = await WhatsAppChannel().notify(
        ChannelNotification(message="Reply.", recipient="15559999999", sender_identity="20000000000009")
    )

    assert ids == ["wamid.BRIDGE"]
    call = fake_httpx.calls[0]
    assert call["url"] == "https://graph.facebook.com/v23.0/20000000000009/messages"  # sent from the routed identity
    assert call["json"]["to"] == "15559999999"  # delivered to the initiator despite not being allowlisted
    assert not fake_redis.store  # fire-and-forget, no correlation


async def test_notify_no_sender_identity_uses_default_and_enforces_allowlist(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # sender_identity absent: the configured default phone_number_id sends, and a
    # caller-requested recipient must be on the allowlist.
    with pytest.raises(ChannelDeliveryError, match="not on CHANNEL_WHATSAPP_ALLOWED_RECIPIENTS"):
        await WhatsAppChannel().notify(ChannelNotification(message="Reply.", recipient="15559999999"))
    assert not fake_httpx.calls

    fake_httpx.responses.append(_accepted("wamid.DEF"))
    ids = await WhatsAppChannel().notify(ChannelNotification(message="Reply.", recipient=ALLOWED_A))

    assert ids == ["wamid.DEF"]
    assert fake_httpx.calls[0]["url"] == _MESSAGES_URL  # the default phone_number_id
    assert fake_httpx.calls[0]["json"]["to"] == ALLOWED_A


async def test_notify_sender_identity_without_recipient_raises(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A bridge reply always carries the initiator's wa_id; with none there is no
    # target and no operator default — refuse loudly.
    with pytest.raises(ChannelDeliveryError, match="requires a recipient"):
        await WhatsAppChannel().notify(ChannelNotification(message="Reply.", sender_identity="20000000000009"))

    assert not fake_httpx.calls


async def test_notify_two_phone_number_ids_each_send_from_their_own(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # one credential, many numbers — each sender_identity replies from its own number.
    fake_httpx.responses.append(_accepted("wamid.A"))
    fake_httpx.responses.append(_accepted("wamid.B"))

    await WhatsAppChannel().notify(
        ChannelNotification(message="From A.", recipient="15551110000", sender_identity="30000000000001")
    )
    await WhatsAppChannel().notify(
        ChannelNotification(message="From B.", recipient="15552220000", sender_identity="30000000000002")
    )

    assert fake_httpx.calls[0]["url"] == "https://graph.facebook.com/v23.0/30000000000001/messages"
    assert fake_httpx.calls[0]["json"]["to"] == "15551110000"
    assert fake_httpx.calls[1]["url"] == "https://graph.facebook.com/v23.0/30000000000002/messages"
    assert fake_httpx.calls[1]["json"]["to"] == "15552220000"


async def test_notify_rejection_raises_delivery_error(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A synchronous non-2xx (e.g. an invalid recipient) surfaces as the loud error;
    # the 24h-window failure is usually async — see the status-webhook test path.
    fake_httpx.responses.append(response(400, json={"error": {"code": 131047, "message": "Re-engagement message"}}))

    with pytest.raises(ChannelDeliveryError, match=r"HTTP 400.*131047"):
        await WhatsAppChannel().notify(ChannelNotification(message="Heads up.", recipient=ALLOWED_A))

    assert not fake_redis.store  # notify never touches the correlation store
