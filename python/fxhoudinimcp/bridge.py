"""HTTP bridge connecting the MCP server to Houdini's hwebserver.

Houdini's hwebserver uses an RPC-style calling convention:

    POST /api
    Content-Type: application/x-www-form-urlencoded
    Body: json=["namespace.function", [positional_args], {keyword_args}]

The server returns the function's return value JSON-encoded.
"""

from __future__ import annotations

# Built-in
import asyncio
import contextlib
import json
import logging
import uuid
from typing import Any

# Third-party
import httpx

# Internal
from fxhoudinimcp.errors import ConnectionError, HoudiniCommandError
from fxhoudinimcp.instance import auth_headers, descriptor_for, process_alive

logger = logging.getLogger(__name__)

# Pass as `timeout` to wait for a command with no deadline at all. A cache
# write or a render takes as long as it takes and reports when done.
NO_TIMEOUT = float("inf")


def _rpc_body(func_name: str, **kwargs: Any) -> dict[str, str]:
    """Build form data for an hwebserver JSON-encoded RPC call."""
    return {"json": json.dumps([func_name, [], kwargs])}


# How often Houdini is checked while a command runs.
_WATCH_INTERVAL = 2.0
_WATCH_PROBE_TIMEOUT = 3.0
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


# Matches the plugin's own search range: a second Houdini moves itself to the
# next free port, so the client has to look there rather than assume 8100.
PORT_SEARCH_RANGE = 16


async def find_servers(
    host: str,
    base: int,
    max_tries: int = PORT_SEARCH_RANGE,
    timeout: float = 1.0,
) -> list[dict[str, Any]]:
    """Probe base..base+max_tries for live plugins, lowest port first.

    Each entry is the mcp.health payload plus the port it answered on. Returns
    every server found rather than just the first, so a caller can say how many
    Houdini sessions are running instead of silently picking one.

    mcp.health touches no HOM, so a live Houdini answers in milliseconds even
    while its main thread is busy. The ports are probed at once: on Windows a
    closed localhost port is not refused but times out, and one after another
    the 16 cost 15.8 s at every server start, against 1.1 s together.
    """

    async def probe(client: httpx.AsyncClient, port: int) -> dict[str, Any] | None:
        try:
            response = await client.post(
                f"http://{host}:{port}/api",
                data=_rpc_body("mcp.health"),
                headers=auth_headers(port),
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return None  # nothing there, or not our endpoint
        if isinstance(payload, dict) and payload.get("status") == "ok":
            return {**payload, "port": port}
        return None

    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        answers = await asyncio.gather(
            *(probe(client, port) for port in range(base, base + max_tries))
        )
    return [answer for answer in answers if answer is not None]


def _new_client(timeout: float) -> httpx.AsyncClient:
    # trust_env=False: HTTP(S)_PROXY from the environment would otherwise route
    # commands, and the bearer token, through a proxy.
    return httpx.AsyncClient(timeout=timeout, trust_env=False)


class HoudiniBridge:
    """Manages HTTP communication between the MCP server and Houdini's hwebserver.

    Houdini's hwebserver exposes @apiFunction endpoints via a single /api URL.
    Calls are dispatched by function name inside the JSON-encoded body.
    """

    def __init__(self, host: str = "localhost", port: int = 8100, timeout: float = 60.0):
        self.host = host
        self.port = port
        self.base_url = f"http://{host}:{port}"
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def retarget(self, port: int) -> None:
        """Talk to the Houdini on ``port`` from now on (connect_houdini, start_houdini)."""
        self.port = port
        self.base_url = f"http://{self.host}:{port}"
        await self._reset_client()

    @property
    def _api_url(self) -> str:
        return f"{self.base_url}/api"

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = _new_client(self.timeout)
        return self._client

    async def _reset_client(self) -> httpx.AsyncClient:
        """Discard the connection pool and return a fresh client."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = _new_client(self.timeout)
        return self._client

    async def _post(
        self,
        data: dict[str, Any],
        timeout: float | None = None,
    ) -> httpx.Response:
        """POST to the bridge, retrying once past a dead pooled connection.

        Houdini closes its side of the keep-alive connections when it exits,
        so the first request after a Houdini restart reuses a socket that is
        already gone and httpx raises RemoteProtocolError. Retrying on a fresh
        pool reconnects to the new Houdini, instead of leaving this process
        permanently "disconnected" until the MCP client itself is restarted.
        """
        # httpx reads timeout=None as "wait forever", so fall back to the
        # configured timeout rather than passing None straight through; a
        # caller that does mean forever says so with NO_TIMEOUT.
        effective = self.timeout if timeout is None else timeout
        if effective == NO_TIMEOUT:
            effective = None

        client = await self._get_client()
        try:
            return await client.post(
                self._api_url, data=data, timeout=effective, headers=auth_headers(self.port)
            )
        except httpx.RemoteProtocolError:
            logger.info("Stale connection to Houdini; reconnecting.")
            client = await self._reset_client()
            return await client.post(
                self._api_url, data=data, timeout=effective, headers=auth_headers(self.port)
            )

    async def _post_watched(
        self, command: str, data: dict[str, Any], timeout: float | None
    ) -> httpx.Response:
        """_post, abandoned with ConnectionError if Houdini goes away meanwhile.

        A command with no deadline would otherwise wait forever on a Houdini that
        has exited, or crashed and is sitting on its crash dialog.
        """
        request = asyncio.ensure_future(self._post(data, timeout=timeout))
        watcher = asyncio.ensure_future(self._houdini_gone())
        try:
            done, _ = await asyncio.wait({request, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if request in done:
                return request.result()
            reason = watcher.result()
            raise ConnectionError(
                f"Houdini {reason} while running {command}. Its result is lost; "
                "check the scene once Houdini is back.",
                details={"url": self.base_url, "command": command},
            )
        finally:
            for task in (request, watcher):
                if not task.done():
                    task.cancel()
            with contextlib.suppress(BaseException):
                await watcher

    async def _houdini_gone(self) -> str:
        """Return why Houdini is gone; never returns while it is still there.

        Only definite signs count: the pid gone, a 401 because a new Houdini
        took the port with another token, or health reporting "crashed" (Houdini
        sits on its crash dialog with the process alive and health still
        served). An unanswered probe does not: a long command holding the GIL
        delays health on a healthy Houdini. An exit closes the request's own
        socket in any case.
        """
        headers = auth_headers(self.port)
        pid = None
        if self.host in _LOOPBACK_HOSTS:
            pid = (descriptor_for(self.port) or {}).get("pid")
        async with httpx.AsyncClient(timeout=_WATCH_PROBE_TIMEOUT, trust_env=False) as probe:
            while True:
                await asyncio.sleep(_WATCH_INTERVAL)
                if isinstance(pid, int) and not process_alive(pid):
                    return f"exited (pid {pid})"
                try:
                    response = await probe.post(
                        self._api_url, data=_rpc_body("mcp.health"), headers=headers
                    )
                    payload = response.json() if response.status_code == 200 else None
                except (httpx.TransportError, ValueError):
                    continue
                if response.status_code == 401:
                    return "was replaced by another session on its port"
                if isinstance(payload, dict) and payload.get("status") == "crashed":
                    return "crashed (its crash dialog is open)"

    async def execute(
        self,
        command: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Execute a command on Houdini and return the result data.

        Args:
            command: The command name (e.g. "scene.get_scene_info")
            params: Command parameters
            timeout: Override timeout for this request (seconds)

        Returns:
            The response data dict on success.

        Raises:
            ConnectionError: Cannot reach Houdini
            HoudiniCommandError: Houdini returned an error
        """
        request_id = str(uuid.uuid4())
        logger.info("→ Houdini: %s", command)

        try:
            response = await self._post_watched(
                command,
                _rpc_body(
                    "mcp.execute",
                    command=command,
                    params=params or {},
                    request_id=request_id,
                ),
                timeout or self.timeout,
            )
            response.raise_for_status()
        except (httpx.ConnectError, httpx.RemoteProtocolError) as e:
            raise ConnectionError(
                f"Cannot connect to Houdini at {self.base_url}. "
                "Is Houdini running with the fxhoudinimcp plugin loaded?",
                details={"url": self.base_url, "original_error": str(e)},
            ) from e
        except httpx.HTTPStatusError as e:
            raise ConnectionError(
                f"Houdini returned HTTP {e.response.status_code}",
                details={
                    "status_code": e.response.status_code,
                    "body": e.response.text,
                },
            ) from e
        except httpx.TimeoutException as e:
            raise ConnectionError(
                f"Request to Houdini timed out after {timeout or self.timeout}s",
                details={"timeout": timeout or self.timeout},
            ) from e
        except httpx.TransportError as e:
            # Everything the branches above do not name: ReadError/WriteError/
            # CloseError on a broken socket, and any transport error a future
            # httpx adds. Without this they reached the MCP client as raw httpx
            # exceptions, and several carry an empty message -- so the client
            # saw a failure with no indication of what went wrong or that
            # Houdini was the cause. ReadError is the one that actually escaped
            # in testing, which is why naming individual classes is a losing
            # game.
            raise ConnectionError(
                f"Lost the connection to Houdini at {self.base_url} "
                f"({type(e).__name__}). Has Houdini been closed or restarted?",
                details={"url": self.base_url, "original_error": str(e)},
            ) from e

        result = response.json()
        timing = result.get("timing_ms", "") if isinstance(result, dict) else ""
        logger.info("← Houdini: %s (%sms)", command, timing)

        if isinstance(result, dict) and result.get("status") == "error":
            err = result.get("error", {})
            raise HoudiniCommandError(
                message=err.get("message", "Unknown Houdini error"),
                code=err.get("code", "UNKNOWN"),
                details=err,
            )

        if isinstance(result, dict) and result.get("status") == "success":
            return result.get("data", {})

        # apiFunction may return the raw result directly
        return result

    async def health_check(self) -> dict[str, Any]:
        """Check if Houdini is responsive.

        Deliberately cheap: the plugin answers this without touching HOM, so it
        works while Houdini's main thread is busy. That is also why it reports
        no scene details -- use scene.get_scene_info for hip_file.

        Returns:
            Dict with status, pid and houdini_version.
        """
        try:
            response = await self._post(_rpc_body("mcp.health"))
            response.raise_for_status()
            return response.json()
        except httpx.TransportError as e:
            # TransportError is the base for ConnectError, the timeout family,
            # RemoteProtocolError and the socket errors, so this covers every
            # way the transport can fail rather than the ones we happened to
            # name.
            raise ConnectionError(
                f"Health check failed: cannot reach Houdini at {self.base_url} "
                f"({type(e).__name__})",
                details={"original_error": str(e)},
            ) from e

    async def list_commands(self) -> list[str]:
        """Return the command names the connected plugin has registered.

        Used to detect a plugin older than this server. Calls mcp.list_commands
        rather than going through mcp.execute, so it works even when the
        dispatcher is missing commands.
        """
        try:
            response = await self._post(_rpc_body("mcp.list_commands"))
            response.raise_for_status()
            payload = response.json()
        except httpx.TransportError as e:
            raise ConnectionError(
                f"Could not list plugin commands at {self.base_url} ({type(e).__name__})",
                details={"original_error": str(e)},
            ) from e

        commands = payload.get("commands") if isinstance(payload, dict) else None
        return commands if isinstance(commands, list) else []

    async def close(self) -> None:
        """Close the HTTP client connection."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
