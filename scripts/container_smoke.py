"""Run inside a --network none, --read-only container with a disposable /data volume.

Usage: docker exec -i epcube-bridge-smoke python < scripts/container_smoke.py
Uses only container loopback; never connects to the cloud or a broker.
"""

import errno
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from epcube_bridge.config import Settings
from epcube_bridge.history import History
from epcube_bridge.state import normalize
from epcube_bridge.storage import TokenStore

assert os.getuid() == 10001
assert os.getgid() == 10001
try:
    Path("/tmp/epcube-write-probe").write_text("must fail")
except OSError as error:
    assert error.errno == errno.EROFS
else:
    raise AssertionError("Root filesystem is writable")

with urlopen("http://127.0.0.1:8080/health", timeout=3) as response:
    health = json.load(response)
assert health == {"status": "ok", "cloud_online": False, "mqtt_connected": False}
try:
    urlopen("http://127.0.0.1:8080/devices", timeout=3)
except HTTPError as error:
    assert error.code == 401
else:
    raise AssertionError("REST endpoint accepted an unauthenticated request")

for path, marker in [("/", b"Energy at home"), ("/assets/dashboard.js", b"Authorization")]:
    with urlopen("http://127.0.0.1:8080" + path, timeout=3) as response:
        assert marker in response.read()
        assert response.headers["Cache-Control"] == "no-store"

settings = Settings()
request = Request(
    "http://127.0.0.1:8080/devices",
    headers={"Authorization": "Bearer " + settings.api_token.get_secret_value()},
)
with urlopen(request, timeout=3) as response:
    assert json.load(response) == []
now = datetime.now(UTC)
history = History(settings, Path("/data/smoke-history.sqlite3"))
history.append(
    normalize(
        "smoke",
        {"batterySoc": 50},
        {},
        {},
        timestamp=now,
        stats_timestamp=now,
        day=now.date().isoformat(),
        timezone="Europe/Rome",
    )
)
assert len(History(settings, history.path).read("smoke")["points"]) == 1
store = TokenStore(settings)
store.save("disposable-smoke-token")
assert store.load() == "disposable-smoke-token"
assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
print(
    "PASS: persistent history, dashboard assets, health, REST auth, UID/GID 10001, read-only root, writable /data, token mode 0600"
)
