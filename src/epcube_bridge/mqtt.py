import asyncio
import hashlib
import json
import logging
import ssl
import time

import aiomqtt

from .bridge import Runtime, backoff
from .config import Settings
from .state import Snapshot

LOG = logging.getLogger(__name__)
HEARTBEAT_S = 300


class Publisher:
    """Per-topic change detection; timestamps alone never cause publication."""

    def __init__(self, client, topic: str):
        self.client, self.topic = client, topic
        self.sent: dict[str, tuple[str, float]] = {}

    async def _send(self, suffix: str, payload: str, comparison: str | None = None):
        now = time.monotonic()
        key = payload if comparison is None else comparison
        previous = self.sent.get(suffix)
        if previous is None or previous[0] != key or now - previous[1] >= HEARTBEAT_S:
            await self.client.publish(f"{self.topic}/{suffix}", payload, qos=1, retain=True)
            self.sent[suffix] = (key, now)

    async def publish(self, snapshot: Snapshot | None, online: bool):
        # Send state before announcing online; don't mark a new connection ready early.
        if snapshot is not None:
            metrics = snapshot.metrics()
            comparison = json.dumps({**metrics, "date": snapshot.daily_energy_date}, sort_keys=True)
            await self._send("state", snapshot.model_dump_json(), comparison)
            for name, value in metrics.items():
                # JSON null replaces stale retained scalars when a field disappears.
                payload = value if isinstance(value, str) else json.dumps(value, allow_nan=False)
                await self._send(name, payload)
        await self._send("availability", "online" if online else "offline")


class MqttWorker:
    def __init__(self, settings: Settings, runtime: Runtime, client_factory=aiomqtt.Client):
        self.settings, self.runtime, self.client_factory = settings, runtime, client_factory

    def client_options(self, device_id: str) -> dict:
        settings = self.settings
        topic = f"{settings.mqtt_base_topic}/{device_id}"
        return dict(
            hostname=settings.mqtt_host,
            port=settings.mqtt_port,
            username=settings.mqtt_user.get_secret_value() if settings.mqtt_user else None,
            password=settings.mqtt_password.get_secret_value() if settings.mqtt_password else None,
            tls_context=ssl.create_default_context() if settings.mqtt_tls else None,
            identifier="epcube-bridge-" + hashlib.sha256(topic.encode()).hexdigest()[:16],
            will=aiomqtt.Will(f"{topic}/availability", "offline", qos=1, retain=True),
            keepalive=30,
            timeout=10,
        )

    async def run(self):
        failures = 0
        while True:
            if self.runtime.device is None:
                await asyncio.sleep(1)
                continue
            topic = f"{self.settings.mqtt_base_topic}/{self.runtime.device['id']}"
            try:
                async with self.client_factory(
                    **self.client_options(self.runtime.device["id"])
                ) as client:
                    self.runtime.mqtt_connected = True
                    publisher = Publisher(client, topic)  # Republish all on reconnection.

                    async def publish_loop():
                        nonlocal failures
                        while True:
                            self.runtime.changed.clear()
                            await publisher.publish(
                                self.runtime.snapshot,
                                self.runtime.available(self.settings.poll_interval_s),
                            )
                            failures = 0
                            try:
                                await asyncio.wait_for(self.runtime.changed.wait(), timeout=1)
                            except TimeoutError:
                                pass

                    async def watch_disconnect():
                        # aiomqtt's message iterator raises on disconnect even without subscriptions.
                        async for _ in client.messages:
                            pass
                        raise aiomqtt.MqttError("disconnected")

                    try:
                        async with asyncio.TaskGroup() as group:
                            group.create_task(publish_loop())
                            group.create_task(watch_disconnect())
                    finally:
                        self.runtime.mqtt_connected = False
                        # Clean disconnect doesn't trigger Last Will; explicitly mark offline.
                        try:
                            await client.publish(
                                f"{topic}/availability", "offline", qos=1, retain=True
                            )
                        except Exception:
                            pass
            except Exception:
                self.runtime.mqtt_connected = False
                failures += 1
                delay = backoff(failures, 5)
                LOG.warning("MQTT unavailable; retry in %.0fs", delay)
                await asyncio.sleep(delay)
