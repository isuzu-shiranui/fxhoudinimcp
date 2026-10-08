"""Server startup and lifecycle management.

Handles starting/stopping the hwebserver and loading handler modules.
"""

from __future__ import annotations

# Built-in
import atexit
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# Internal
from fxhoudinimcp_server import instance

_server_started = False
_port = 8100

# Whether the API answers commands. Separate from _server_started, which stays
# False for the whole life of a foreground hython server. stop() clears this
# rather than shutting hwebserver down, because requestShutdown() would also
# stop Houdini's own web features on the same server.
_accepting = False

_LOOPBACK_ADDRESSES = frozenset({"127.0.0.1", "localhost", "::1"})

# True while an auto-start readiness check is still in flight on a worker
# thread, so a menu click during startup does not start a second server.
_starting = False

# Ceiling for the readiness poll. A healthy start answers in well under a
# second, since mcp.health needs nothing from the main thread; the old 3s was
# tight only because the health endpoint used to deadlock against this very
# loop. Generous now that auto-start no longer waits on the main thread.
_READINESS_TIMEOUT = 15.0

# How many ports to try from the configured base. A second Houdini used to fail
# outright with "port 8100 is owned by another Houdini process", leaving that
# session with no MCP at all. Sixteen covers more concurrent sessions than
# anyone runs while keeping the failed-probe cost bounded.
_PORT_SEARCH_RANGE = 16


# The probe goes to loopback; a proxy from the environment must not see it.
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _health_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/api"


def _health_body() -> bytes:
    return urllib.parse.urlencode({"json": json.dumps(["mcp.health", [], {}])}).encode("utf-8")


def _query_health(port: int, timeout: float = 0.5) -> dict | None:
    """mcp.health on *port* with this session's token, or None if nothing answers.

    A 401 means a server is there but holds another token, i.e. another Houdini,
    and is reported as such so the port is not mistaken for a free one.
    """
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if instance.token() is not None:
        headers["Authorization"] = f"Bearer {instance.token()}"
    request = urllib.request.Request(
        _health_url(port),
        data=_health_body(),
        headers=headers,
        method="POST",
    )
    try:
        with _NO_PROXY.open(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return {"status": "unauthorized", "pid": None}
        return None
    except Exception:
        return None

    try:
        data = json.loads(payload)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _wait_for_current_process_health(
    port: int,
    timeout_seconds: float = _READINESS_TIMEOUT,
) -> dict | None:
    deadline = time.time() + max(0.0, timeout_seconds)
    current_pid = os.getpid()
    last_health = None
    while time.time() < deadline:
        health = _query_health(port)
        if health is not None:
            last_health = health
            if health.get("pid") == current_pid:
                return health
        time.sleep(0.1)
    return last_health


def _pick_free_port(
    base: int,
    probe=None,
    my_pid: int | None = None,
    max_tries: int = _PORT_SEARCH_RANGE,
) -> int:
    """Return the first port at or after *base* this process can serve on.

    A port is free when nothing answers mcp.health there. A port already answering
    as *this* process is returned as-is, so restarting the server in a session
    that already has one is idempotent rather than a move to the next port. A
    port owned by a different pid is another Houdini and is skipped.

    Idea from @husman2012 (PR #13). Note the limitation: "nothing answers
    mcp.health" is not the same as "nothing holds the socket", so a port occupied
    by an unrelated server still fails at bind time. That was true before this
    existed and is reported by the caller either way.
    """
    if probe is None:
        probe = _query_health
    if my_pid is None:
        my_pid = os.getpid()

    for port in range(base, base + max_tries):
        health = probe(port)
        if health is None:
            return port
        if health.get("pid") == my_pid:
            return port
    raise RuntimeError(
        f"No free port in {base}-{base + max_tries - 1}: every one is answering "
        f"as another Houdini process."
    )


def _bind_localhost_only(hwebserver) -> None:
    """Restrict the server to loopback before it starts listening.

    hwebserver binds the any-address (0.0.0.0) by default, which would put
    this bridge on the LAN. That matters more here than for a typical web
    endpoint: the bridge runs arbitrary Python inside Houdini (see
    handlers/code_handlers.py), and the bearer token is the only other thing
    between the network and the session.

    Set FXHOUDINIMCP_BIND to override, e.g. "0.0.0.0" to accept remote
    connections deliberately.

    Raises when a loopback address was asked for and could not be set, since
    serving anyway would fall back to 0.0.0.0.
    """
    address = os.environ.get("FXHOUDINIMCP_BIND", "127.0.0.1")
    try:
        # Note the argument order: (settings, port_name). Passing the port
        # number first raises AttributeError on 'int'.
        hwebserver.setSettingsForPort({"ADDRESS": address}, "main")
    except Exception as exc:
        if address in _LOOPBACK_ADDRESSES:
            raise RuntimeError(
                f"could not restrict the bind address to {address} ({exc}); "
                f"not starting, because hwebserver would listen on every interface"
            ) from exc
        print(f"[fxhoudinimcp] Warning: could not set bind address {address}: {exc}")


def start(
    port: int | None = None,
    background: bool | None = None,
    wait: bool = True,
) -> None:
    """Start the FXHoudini-MCP server.

    Registers all command handlers and ensures hwebserver is running.

    Must be called from the thread that will own the server. hwebserver keeps
    its ``Server`` object in a ``threading.local()``, so API functions
    registered on one thread are invisible to ``run()`` on another -- calling
    ``run()`` from a fresh thread fails outright with "No URL handlers have
    been added to the server."

    Args:
        port: Port for hwebserver. Defaults to FXHOUDINIMCP_PORT env var or 8100.
        background: Serve on a background thread instead of blocking. Defaults
            to Houdini's own choice, which is True in a UI session and False
            under hython. Pass True from a headless script that needs start()
            to return while the server keeps serving.
        wait: Block until the server answers, and raise if it does not. Pass
            False for auto-start, where nothing reads the result and blocking
            would stall Houdini's UI; readiness is then confirmed on a worker
            thread and failure is printed rather than raised.
    """
    global _server_started, _port, _starting, _accepting

    if _server_started:
        print("[fxhoudinimcp] Server already running")
        return
    if _starting:
        print("[fxhoudinimcp] Server is still starting")
        return

    base = port or int(os.environ.get("FXHOUDINIMCP_PORT", "8100"))
    _port = _pick_free_port(base)
    if _port != base:
        # Say so loudly: the MCP client scans for the port, but anyone who
        # pinned HOUDINI_PORT on the client side needs to know it moved.
        print(
            f"[fxhoudinimcp] Port {base} is already serving another Houdini; "
            f"using {_port} instead. Set HOUDINI_PORT={_port} on the MCP client "
            f"if you pin it."
        )

    # Import handlers to trigger registration via register_handler() calls
    # Start hwebserver if not already running. In Houdini 20.5+ it may already
    # be running for built-in features; in that case registering the functions
    # above is enough. Either way, prove the HTTP endpoint is reachable before
    # advertising readiness.
    import hou
    import hwebserver

    # Import hwebserver_app to register the API functions
    from fxhoudinimcp_server import (
        handlers,  # noqa: F401
        hwebserver_app,  # noqa: F401
    )

    if background is None:
        # hwebserver.run() already defaults in_background to isUIAvailable(),
        # so this matches its behaviour; it is passed explicitly so the choice
        # is visible here and does not silently change under us. Blocking in a
        # UI session would wedge Houdini's main thread; blocking under hython
        # is what keeps the process alive to serve.
        background = hou.isUIAvailable()

    _bind_localhost_only(hwebserver)
    instance.new_token()
    if not background:
        # run() never returns while a foreground server is up, so the descriptor
        # has to go out first. A second process racing for the same port can
        # overwrite it here; the background path publishes only once the port
        # is proven to be ours.
        instance.publish(_port, os.environ.get("HOUDINI_VERSION", "unknown"))
    _accepting = True

    run_error = None
    try:
        hwebserver.run(_port, debug=False, in_background=background)
    except Exception as exc:
        run_error = exc

    if not background:
        # run() blocks until shutdown when serving in the foreground, so
        # reaching this point means it either finished or never started.
        _stand_down()
        if run_error is not None:
            raise RuntimeError(f"hwebserver failed to start on port {_port}: {run_error}")
        return

    if wait:
        _confirm_ready(run_error)
        return

    # Auto-start: nobody is waiting on a return value, so do not make Houdini's
    # main thread sit through the poll. The worker only does urllib and
    # os.getpid(), never hou.*, which is safe off the main thread and is exactly
    # why mcp.health had to become HOM-free.
    _starting = True
    worker = threading.Thread(target=_confirm_ready_async, args=(run_error,), daemon=True)
    try:
        worker.start()
    except Exception:
        # The thread never ran, so nothing else will clear this.
        _starting = False
        raise


def _confirm_ready(run_error: Exception | None) -> None:
    """Poll until the server answers as this process, then mark it running.

    Raises on failure, so an explicit Start Server can report why.
    """
    global _server_started

    health = _wait_for_current_process_health(_port)
    if health is None:
        _stand_down()
        detail = f": {run_error}" if run_error is not None else ""
        raise RuntimeError(f"hwebserver did not answer mcp.health on port {_port}{detail}")

    health_pid = health.get("pid")
    if health_pid != os.getpid():
        _stand_down()
        raise RuntimeError(
            f"hwebserver port {_port} is owned by another Houdini process "
            f"(pid {health_pid}), current pid {os.getpid()}"
        )

    if not _accepting or health.get("status") != "ok":
        # Stop Server was pressed while this check was polling.
        return

    instance.publish(_port, health.get("houdini_version", "unknown"))
    _server_started = True
    print(
        "[fxhoudinimcp] Server ready on port {} (Houdini {}, pid {})".format(
            _port,
            health.get("houdini_version", "unknown"),
            health.get("pid", "unknown"),
        )
    )


def _confirm_ready_async(run_error: Exception | None) -> None:
    """_confirm_ready for a daemon thread: reports instead of raising.

    An exception here would die unheard in the worker, so the failure is printed
    in the same shape auto-start used to raise. _starting is always cleared, or
    a failed start would leave the server permanently un-startable from the menu.
    """
    global _starting

    try:
        _confirm_ready(run_error)
    except Exception as exc:
        print(f"[fxhoudinimcp] Auto-start failed: {exc}")
    finally:
        _starting = False


def _stand_down() -> None:
    """Refuse further commands and withdraw the descriptor."""
    global _server_started, _accepting
    _server_started = False
    _accepting = False
    instance.withdraw()


atexit.register(instance.withdraw)


def stop() -> None:
    """Stop answering MCP commands.

    hwebserver keeps listening, since requestShutdown() would take Houdini's own
    web features down with it, but every endpoint refuses from here on and
    mcp.health reports "stopped", so the MCP client no longer picks this session.
    """
    if not _server_started and not _accepting:
        return

    _stand_down()
    print("[fxhoudinimcp] Server stopped")


def is_running() -> bool:
    """Check if the server is currently running."""
    return _server_started


def is_accepting() -> bool:
    """Whether the API endpoints should answer commands."""
    return _accepting


def get_port() -> int:
    """Get the port the server is running on."""
    return _port


def is_starting() -> bool:
    """True while an auto-start readiness check is still in flight."""
    return _starting


def ensure_running(wait: bool = True) -> None:
    """Start the server if it's not already running.

    Args:
        wait: Passed through to start(). Auto-start uses False so Houdini's UI
            is never blocked by the readiness poll.
    """
    global _server_started
    if _starting:
        return
    if _server_started:
        health = _wait_for_current_process_health(_port, timeout_seconds=0.5)
        if health is not None and health.get("pid") == os.getpid():
            return
        _server_started = False
    start(wait=wait)
