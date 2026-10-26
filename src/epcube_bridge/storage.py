import hashlib
import json
import os
import tempfile
from pathlib import Path

from .config import Settings


class TokenStore:
    def __init__(self, settings: Settings, directory: Path = Path("/data")):
        self.path = directory / "token.json"
        # Bind the cache to account, region AND credentials without storing them.
        identity = json.dumps(
            [
                settings.epcube_region,
                settings.epcube_email.get_secret_value(),
                settings.epcube_password.get_secret_value(),
            ]
        )
        self.identity = hashlib.sha256(identity.encode()).hexdigest()

    def load(self) -> str | None:
        try:
            data = json.loads(self.path.read_text())
            if data.get("identity") == self.identity and isinstance(data.get("token"), str):
                return data["token"] or None
        except (OSError, ValueError, AttributeError):
            pass
        return None

    def save(self, token: str) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".token-")
        try:
            with os.fdopen(fd, "w") as stream:
                # mkstemp creates a 0600 file. Atomic replace also fixes old permissions.
                json.dump({"identity": self.identity, "token": token}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
