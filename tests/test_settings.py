"""Settings: env round-trips, secret hygiene, and the fail-closed require helpers."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError
from tai42_contract.channels import ChannelDeliveryError
from tai42_kit.settings import reset_all_settings

from tai42_channel_whatsapp_cloud.settings import (
    WhatsAppCloudRedisSettings,
    WhatsAppCloudSettings,
    require_delivery_secret,
    require_delivery_setting,
    require_secret,
    whatsapp_cloud_redis_settings,
    whatsapp_cloud_settings,
)


def test_env_round_trip(whatsapp_env):
    settings = WhatsAppCloudSettings()
    assert settings.access_token is not None
    assert settings.access_token.get_secret_value() == "test-access-token"
    assert settings.app_secret is not None
    assert settings.app_secret.get_secret_value() == "test-app-secret"
    assert settings.verify_token is not None
    assert settings.verify_token.get_secret_value() == "test-verify-token"
    assert settings.default_phone_number_id == "10000000000001"
    assert settings.allowed_recipients == ["15551230001", "15551230002"]


def test_secrets_never_surface_in_repr_or_dump(whatsapp_env):
    settings = WhatsAppCloudSettings()
    for exposed in (repr(settings), str(settings.model_dump())):
        assert "test-access-token" not in exposed
        assert "test-app-secret" not in exposed
        assert "test-verify-token" not in exposed


def test_no_env_constructs_cleanly_all_none(no_whatsapp_env):
    # A library import never demands configuration: every credential/target
    # field defaults None / empty.
    settings = WhatsAppCloudSettings()
    assert settings.access_token is None
    assert settings.app_secret is None
    assert settings.verify_token is None
    assert settings.default_phone_number_id is None
    assert settings.allowed_recipients == []


def test_allowed_recipients_comma_string_splits_strips_and_drops_empties(no_whatsapp_env, monkeypatch):
    monkeypatch.setenv("CHANNEL_WHATSAPP_CLOUD_ALLOWED_RECIPIENTS", " 15551230001 ,, 15551230002 , ")
    reset_all_settings()
    assert WhatsAppCloudSettings().allowed_recipients == ["15551230001", "15551230002"]


def test_allowed_recipients_json_list_env(no_whatsapp_env, monkeypatch):
    monkeypatch.setenv("CHANNEL_WHATSAPP_CLOUD_ALLOWED_RECIPIENTS", '["15551230001", "15551230002"]')
    reset_all_settings()
    assert WhatsAppCloudSettings().allowed_recipients == ["15551230001", "15551230002"]


def test_allowed_recipients_json_list_strips_and_drops_empties(no_whatsapp_env, monkeypatch):
    monkeypatch.setenv("CHANNEL_WHATSAPP_CLOUD_ALLOWED_RECIPIENTS", '[" 15551230001 ", "", " 15551230002 "]')
    reset_all_settings()
    assert WhatsAppCloudSettings().allowed_recipients == ["15551230001", "15551230002"]


def test_allowed_recipients_malformed_json_raises(no_whatsapp_env, monkeypatch):
    monkeypatch.setenv("CHANNEL_WHATSAPP_CLOUD_ALLOWED_RECIPIENTS", "[not json")
    reset_all_settings()
    with pytest.raises(ValidationError, match="allowed_recipients"):
        WhatsAppCloudSettings()


def test_allowed_recipients_json_non_string_items_raise(no_whatsapp_env, monkeypatch):
    monkeypatch.setenv("CHANNEL_WHATSAPP_CLOUD_ALLOWED_RECIPIENTS", '[["nested"]]')
    reset_all_settings()
    with pytest.raises(ValidationError, match="allowed_recipients"):
        WhatsAppCloudSettings()


def test_allowed_recipients_list_passes_through(no_whatsapp_env):
    settings = WhatsAppCloudSettings(allowed_recipients=["15551230001"])
    assert settings.allowed_recipients == ["15551230001"]


def test_allowed_recipients_other_type_raises(no_whatsapp_env):
    with pytest.raises(ValidationError, match="comma-separated string or a list"):
        WhatsAppCloudSettings(allowed_recipients=123)  # type: ignore[arg-type]


def test_require_delivery_setting_raises_delivery_error_naming_env_var():
    with pytest.raises(ChannelDeliveryError, match="set CHANNEL_WHATSAPP_CLOUD_DEFAULT_PHONE_NUMBER_ID"):
        require_delivery_setting(None, "CHANNEL_WHATSAPP_CLOUD_DEFAULT_PHONE_NUMBER_ID")


def test_require_delivery_setting_empty_raises_delivery_error():
    with pytest.raises(ChannelDeliveryError, match="set CHANNEL_WHATSAPP_CLOUD_DEFAULT_PHONE_NUMBER_ID"):
        require_delivery_setting("", "CHANNEL_WHATSAPP_CLOUD_DEFAULT_PHONE_NUMBER_ID")


def test_require_delivery_setting_passes_value_through():
    assert require_delivery_setting("10000000000001", "CHANNEL_WHATSAPP_CLOUD_DEFAULT_PHONE_NUMBER_ID") == (
        "10000000000001"
    )


def test_require_delivery_secret_unset_raises_delivery_error_naming_env_var():
    with pytest.raises(ChannelDeliveryError, match="set CHANNEL_WHATSAPP_CLOUD_ACCESS_TOKEN"):
        require_delivery_secret(None, "CHANNEL_WHATSAPP_CLOUD_ACCESS_TOKEN")


def test_require_delivery_secret_empty_raises_delivery_error():
    with pytest.raises(ChannelDeliveryError, match="set CHANNEL_WHATSAPP_CLOUD_ACCESS_TOKEN"):
        require_delivery_secret(SecretStr(""), "CHANNEL_WHATSAPP_CLOUD_ACCESS_TOKEN")


def test_require_delivery_secret_returns_plaintext():
    assert require_delivery_secret(SecretStr("tok"), "CHANNEL_WHATSAPP_CLOUD_ACCESS_TOKEN") == "tok"


def test_require_secret_unset_raises_naming_env_var():
    # The inbound webhook path keeps ValueError — its handler maps it to a
    # logged 500, never a delivery failure.
    with pytest.raises(ValueError, match="set CHANNEL_WHATSAPP_CLOUD_APP_SECRET"):
        require_secret(None, "CHANNEL_WHATSAPP_CLOUD_APP_SECRET")


def test_require_secret_empty_fails_closed():
    with pytest.raises(ValueError, match="set CHANNEL_WHATSAPP_CLOUD_APP_SECRET"):
        require_secret(SecretStr(""), "CHANNEL_WHATSAPP_CLOUD_APP_SECRET")


def test_require_secret_returns_plaintext():
    assert require_secret(SecretStr("tok"), "CHANNEL_WHATSAPP_CLOUD_APP_SECRET") == "tok"


def test_empty_env_secret_is_treated_as_missing(no_whatsapp_env, monkeypatch: pytest.MonkeyPatch):
    # env_ignore_empty: an empty CHANNEL_WHATSAPP_CLOUD_APP_SECRET= leaves the
    # field None, so require_secret raises through the same fail-closed helper.
    monkeypatch.setenv("CHANNEL_WHATSAPP_CLOUD_APP_SECRET", "")
    reset_all_settings()
    settings = WhatsAppCloudSettings()
    assert settings.app_secret is None
    with pytest.raises(ValueError, match="CHANNEL_WHATSAPP_CLOUD_APP_SECRET"):
        require_secret(settings.app_secret, "CHANNEL_WHATSAPP_CLOUD_APP_SECRET")


def test_api_base_url_default_and_override(no_whatsapp_env, monkeypatch: pytest.MonkeyPatch):
    # Production default is the Graph API origin with a pinned version; an operator
    # (or the e2e harness pointing at a stub) may override it.
    assert WhatsAppCloudSettings().api_base_url == "https://graph.facebook.com/v23.0"
    monkeypatch.setenv("CHANNEL_WHATSAPP_CLOUD_API_BASE_URL", "http://127.0.0.1:9098")
    reset_all_settings()
    assert WhatsAppCloudSettings().api_base_url == "http://127.0.0.1:9098"


def test_http_timeout_default_and_override(no_whatsapp_env, monkeypatch: pytest.MonkeyPatch):
    assert WhatsAppCloudSettings().http_timeout_seconds == 30
    monkeypatch.setenv("CHANNEL_WHATSAPP_CLOUD_HTTP_TIMEOUT_SECONDS", "7.5")
    reset_all_settings()
    assert WhatsAppCloudSettings().http_timeout_seconds == 7.5


def test_http_timeout_rejects_non_positive(no_whatsapp_env, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHANNEL_WHATSAPP_CLOUD_HTTP_TIMEOUT_SECONDS", "0")
    reset_all_settings()
    with pytest.raises(ValidationError, match="http_timeout_seconds"):
        WhatsAppCloudSettings()


def test_dedupe_ttl_rejects_non_positive(no_whatsapp_env, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHANNEL_WHATSAPP_CLOUD_DEDUPE_TTL", "0")
    reset_all_settings()
    with pytest.raises(ValidationError, match="dedupe_ttl"):
        WhatsAppCloudSettings()


def test_redis_settings_read_url_and_client_kwargs(whatsapp_env):
    settings = WhatsAppCloudRedisSettings()
    assert settings.redis_url == "redis://test/0"
    assert settings.client_kwargs()["url"] == "redis://test/0"
    # decode_responses default True: the correlation getters return str.
    assert settings.client_kwargs()["decode_responses"] is True


def test_cached_accessors_reset_with_settings_cache(whatsapp_env, monkeypatch: pytest.MonkeyPatch):
    assert whatsapp_cloud_settings() is whatsapp_cloud_settings()
    assert whatsapp_cloud_redis_settings() is whatsapp_cloud_redis_settings()
    before = whatsapp_cloud_settings()
    monkeypatch.setenv("CHANNEL_WHATSAPP_CLOUD_DEFAULT_PHONE_NUMBER_ID", "20000000000002")
    reset_all_settings()
    after = whatsapp_cloud_settings()
    assert after is not before
    assert after.default_phone_number_id == "20000000000002"
