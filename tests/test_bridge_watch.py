"""A command in flight is abandoned when Houdini goes away."""

from __future__ import annotations

# Built-in
import asyncio

# Third-party
import httpx
import pytest

# Internal
from fxhoudinimcp import bridge as bridge_module
from fxhoudinimcp.bridge import HoudiniBridge
from fxhoudinimcp.errors import ConnectionError


@pytest.mark.asyncio
async def test_a_hung_command_ends_when_houdini_is_gone(monkeypatch):
    """A command with no deadline used to wait forever on a crashed Houdini."""
    bridge = HoudiniBridge(port=8100)
    cancelled = asyncio.Event()

    async def hung_post(data, timeout=None):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def gone():
        return "exited (pid 42)"

    monkeypatch.setattr(bridge, "_post", hung_post)
    monkeypatch.setattr(bridge, "_houdini_gone", gone)

    with pytest.raises(ConnectionError, match=r"exited \(pid 42\) while running cache.write_cache"):
        await bridge.execute("cache.write_cache", {})
    await asyncio.sleep(0)
    assert cancelled.is_set()


def _probe_client(handler):
    real = httpx.AsyncClient

    def factory(**kwargs):
        return real(transport=httpx.MockTransport(handler), **kwargs)

    return factory


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "reason"),
    [
        (lambda request: httpx.Response(401), "replaced"),
        (lambda request: httpx.Response(200, json={"status": "crashed"}), "crashed"),
    ],
)
async def test_watch_reports_why_houdini_is_gone(monkeypatch, handler, reason):
    monkeypatch.setattr(bridge_module, "_WATCH_INTERVAL", 0)
    monkeypatch.setattr(bridge_module.httpx, "AsyncClient", _probe_client(handler))
    monkeypatch.setattr(bridge_module, "descriptor_for", lambda port: None)
    bridge = HoudiniBridge(port=8100)

    assert reason in await asyncio.wait_for(bridge._houdini_gone(), 5)


def _refused(request):
    raise httpx.ConnectError("refused")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler",
    [
        lambda request: httpx.Response(200, json={"status": "ok"}),
        # A command holding the GIL delays health on a healthy Houdini.
        _refused,
        lambda request: httpx.Response(500, text="oops"),
        lambda request: httpx.Response(200, json=[]),
    ],
)
async def test_watch_stays_quiet_without_a_definite_sign(monkeypatch, handler):
    """A long command on a live Houdini must not be abandoned."""
    monkeypatch.setattr(bridge_module, "_WATCH_INTERVAL", 0)
    monkeypatch.setattr(bridge_module.httpx, "AsyncClient", _probe_client(handler))
    monkeypatch.setattr(bridge_module, "descriptor_for", lambda port: None)
    bridge = HoudiniBridge(port=8100)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bridge._houdini_gone(), 0.2)
