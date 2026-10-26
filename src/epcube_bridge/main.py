import logging

import uvicorn
from pydantic import ValidationError
from pydantic_settings import SettingsError

from .app import create_app
from .config import Settings


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # HTTP/MQTT debug logging could expose URL parameters or authentication metadata.
    for name in ("httpx", "httpcore", "aiomqtt", "paho"):
        logging.getLogger(name).disabled = True
    try:
        settings = Settings()
    except (ValidationError, SettingsError):
        logging.error(
            "Invalid environment configuration; check the variables documented in README.md"
        )
        raise SystemExit(2) from None
    if settings.enable_controls:
        logging.warning("ENABLE_CONTROLS is reserved; this build remains read-only")
    uvicorn.run(create_app(settings), host="0.0.0.0", port=8080, access_log=False)


if __name__ == "__main__":
    main()
