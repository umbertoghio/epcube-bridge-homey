import pytest
from pydantic import ValidationError

from epcube_bridge.config import Settings


def test_environment_only(monkeypatch):
    for key, value in {
        "EPCUBE_EMAIL": "test@example.test",
        "EPCUBE_PASSWORD": "secret",
        "MQTT_HOST": "broker",
        "API_TOKEN": "private",
        "MQTT_TLS": "true",
        "EPCUBE_TIMEZONE": "Europe/Rome",
    }.items():
        monkeypatch.setenv(key, value)
    settings = Settings()
    assert settings.mqtt_tls
    assert settings.poll_interval_s == 30
    assert settings.enable_controls is False
    assert "private" not in repr(settings)
    assert "test@example.test" not in repr(settings)


@pytest.mark.parametrize(
    "update",
    [
        {"api_token": ""},
        {"poll_interval_s": 0},
        {"mqtt_base_topic": "bad/#"},
        {"mqtt_port": 0},
        {"epcube_timezone": "not/a/timezone"},
        {"epcube_region": "AU"},
    ],
)
def test_invalid_configuration(settings, update):
    with pytest.raises(ValidationError):
        Settings(**{**settings.model_dump(), **update})
