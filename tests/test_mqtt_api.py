import asyncio
import json
import time
from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from epcube_bridge.app import create_app
from epcube_bridge.bridge import Runtime
from epcube_bridge.mqtt import MqttWorker, Publisher


async def test_mqtt_change_heartbeat_and_null(snapshot, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("epcube_bridge.mqtt.time.monotonic", lambda: clock[0])
    client = AsyncMock()
    publisher = Publisher(client, "epcube/123")
    await publisher.publish(snapshot, True)
    initial = client.publish.await_count
    payloads = {call.args[0]: call.args[1] for call in client.publish.await_args_list}
    assert json.loads(payloads["epcube/123/state"])["battery_power"] == 800
    assert payloads["epcube/123/soc"] == "64.0"
    assert payloads["epcube/123/availability"] == "online"
    assert all(call.kwargs == {"qos": 1, "retain": True} for call in client.publish.await_args_list)
    updated_time = snapshot.model_copy(
        update={"timestamp": snapshot.timestamp + timedelta(seconds=30)}
    )
    await publisher.publish(updated_time, True)
    assert client.publish.await_count == initial
    changed = updated_time.model_copy(update={"grid_power": None})
    await publisher.publish(changed, True)
    assert client.publish.await_count == initial + 2  # state + changed metric only
    assert client.publish.await_args_list[-1].args == ("epcube/123/grid_power", "null")
    clock[0] += 301
    await publisher.publish(changed, True)
    assert client.publish.await_count == 2 * initial + 2
    await publisher.publish(changed, False)
    assert client.publish.await_args.args == ("epcube/123/availability", "offline")


async def test_failed_publish_is_retried(snapshot):
    client = AsyncMock()
    client.publish.side_effect = [OSError("unavailable"), None]
    publisher = Publisher(client, "epcube/123")
    with pytest.raises(OSError):
        await publisher.publish(snapshot, True)
    assert publisher.sent == {}
    client.publish.side_effect = None
    await publisher.publish(snapshot, True)
    assert "state" in publisher.sent


def test_last_will_tls(settings):
    worker = MqttWorker(settings.model_copy(update={"mqtt_tls": True}), Runtime())
    opts = worker.client_options("123")
    assert str(opts["will"].topic) == "epcube/123/availability"
    assert opts["will"].payload == "offline"
    assert opts["will"].retain is True
    assert opts["tls_context"].check_hostname


@pytest.mark.parametrize("controls", [False, True])
async def test_rest_auth_readonly_and_staleness(settings, snapshot, controls):
    state = Runtime(
        device={"id": "123", "serial": "private-serial"},
        snapshot=snapshot,
        cloud_online=True,
        last_success=time.monotonic(),
    )
    app = create_app(
        settings.model_copy(update={"enable_controls": controls}),
        runtime=state,
        start_workers=False,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/health")).status_code == 200
        for path in ("/devices", "/devices/123/state"):
            assert (await client.get(path)).status_code == 401
            assert (
                await client.get(path, headers={"Authorization": "Bearer wrong"})
            ).status_code == 401
        headers = {"Authorization": "Bearer local-api-secret"}
        assert (await client.get("/devices", headers=headers)).json() == [
            {"id": "123", "available": True}
        ]
        result = await client.get("/devices/123/state", headers=headers)
        assert result.json()["battery_power"] == 800
        assert result.json()["available"]
        state.cloud_online = False
        result = await client.get("/devices/123/state", headers=headers)
        assert not result.json()["available"]
        assert result.json()["timestamp"] == snapshot.model_dump(mode="json")["timestamp"]
        assert (await client.get("/devices/unknown/state", headers=headers)).status_code == 404
        assert (await client.post("/devices/123/state", headers=headers)).status_code == 405
        assert (await client.post("/devices/123/mode", headers=headers)).status_code == 404
        assert (await client.get("/docs")).status_code == 404
        assert (await client.get("/openapi.json")).status_code == 404
    assert all(route.methods == {"GET"} for route in app.routes)


async def test_mqtt_shutdown_publishes_offline(settings, snapshot):
    published = asyncio.Event()

    class FakeClient:
        def __init__(self, **kwargs):
            self.calls = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def publish(self, topic, payload, **kwargs):
            self.calls.append((topic, payload))
            if topic.endswith("/availability") and payload == "online":
                published.set()

        @property
        def messages(self):
            async def stream():
                await asyncio.Event().wait()
                yield None

            return stream()

    fake = FakeClient()
    runtime = Runtime(
        device={"id": "123"}, snapshot=snapshot, cloud_online=True, last_success=time.monotonic()
    )
    worker = MqttWorker(settings, runtime, client_factory=lambda **kwargs: fake)
    task = asyncio.create_task(worker.run())
    await asyncio.wait_for(published.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake.calls[-1] == ("epcube/123/availability", "offline")
    assert not runtime.mqtt_connected


async def test_broker_disconnect_reconnects_and_republishes(settings, snapshot, monkeypatch):
    import aiomqtt

    first_online = asyncio.Event()
    reconnected = asyncio.Event()
    clients = []
    delays = []
    real_sleep = asyncio.sleep

    async def quick_sleep(delay):
        delays.append(delay)
        await real_sleep(0)

    class Client:
        def __init__(self, **kwargs):
            self.index = len(clients)
            self.calls = []
            clients.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def publish(self, topic, payload, **kwargs):
            self.calls.append((topic, payload))
            if topic.endswith("/availability") and payload == "online":
                (first_online if self.index == 0 else reconnected).set()

        @property
        def messages(self):
            async def stream():
                if self.index == 0:
                    await first_online.wait()
                    raise aiomqtt.MqttError("private-broker-error")
                await asyncio.Event().wait()
                yield None

            return stream()

    monkeypatch.setattr("epcube_bridge.mqtt.asyncio.sleep", quick_sleep)
    runtime = Runtime(
        device={"id": "123"}, snapshot=snapshot, cloud_online=True, last_success=time.monotonic()
    )
    worker = MqttWorker(settings, runtime, client_factory=Client)
    task = asyncio.create_task(worker.run())
    await asyncio.wait_for(reconnected.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(clients) == 2
    assert 5 <= delays[0] <= 6
    for client in clients:
        assert any(topic == "epcube/123/state" for topic, _ in client.calls)


def test_stale_sample_is_unavailable(snapshot):
    runtime = Runtime(snapshot=snapshot, cloud_online=True, last_success=time.monotonic() - 91)
    assert not runtime.available(30)
