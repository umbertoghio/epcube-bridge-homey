from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REGIONS = {
    "EU": "https://monitoring-eu.epcube.com/api",
    "US": "https://epcube-monitoring.com/app-api",
    "JP": "https://monitoring-jp.epcube.com/api",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, hide_input_in_errors=True)

    epcube_email: SecretStr
    epcube_password: SecretStr
    epcube_region: Literal["EU", "US", "JP"] = "EU"
    epcube_timezone: str = "Europe/Rome"
    poll_interval_s: float = Field(default=30, ge=10, le=86400)
    mqtt_host: str = Field(min_length=1)
    mqtt_port: int = Field(default=1883, ge=1, le=65535)
    mqtt_user: SecretStr | None = None
    mqtt_password: SecretStr | None = None
    mqtt_tls: bool = False
    mqtt_base_topic: str = "epcube"
    api_token: SecretStr
    enable_controls: bool = False  # Reserved; never installs write routes.

    @field_validator("epcube_email", "epcube_password", "api_token")
    @classmethod
    def nonempty_secret(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("mqtt_base_topic")
    @classmethod
    def topic(cls, value: str) -> str:
        value = value.strip("/")
        if not value or any(c in value for c in ("+", "#", "\0")):
            raise ValueError("invalid MQTT topic prefix")
        return value

    @field_validator("epcube_timezone")
    @classmethod
    def timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("must be an IANA timezone") from None
        return value
