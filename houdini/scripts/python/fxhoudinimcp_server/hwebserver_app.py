"""hwebserver endpoint registration for the FXHoudini-MCP plugin.

Registers API functions on Houdini's built-in HTTP server that the
external MCP server communicates with over HTTP.

Calling convention (JSON-encoded RPC):
    POST /api
    Body: json=["mcp.execute", [], {"command": "...", "params": {...}, "request_id": "..."}]

Responses are JSON-encoded here rather than left to hwebserver, so that a
value HOM cannot serialise degrades instead of collapsing into an opaque 500.
"""

from __future__ import annotations

# Built-in
import glob
import json
import os
import tempfile
import time
import traceback

# Third-party
import hwebserver

# Internal
from fxhoudinimcp_server import dispatcher, instance, startup
from fxhoudinimcp_server.serialize import json_default

###### Registration


def _api_function(namespace: str):
    """Register an API function with hwebserver and keep the module attribute.

    hwebserver's module-level ``apiFunction`` decorator returns ``None``,
    because ``Server._apiFunction`` has no return statement. Registration
    itself works, but the decorated name would otherwise be bound to ``None``,
    which breaks anything that later imports the function -- including tests.

    Registration is thread-local: hwebserver keeps its ``Server`` in a
    ``threading.local()``, so the thread that imports this module must also be
    the thread that calls ``hwebserver.run()``. See startup.py.
    """

    def decorator(function):
        hwebserver.apiFunction(namespace=namespace)(function)
        return function

    return decorator


def _json_response(payload: dict) -> hwebserver.Response:
    """Encode *payload* as an HTTP JSON response.

    hwebserver would otherwise call ``json.dumps`` itself, from a place
    outside its own exception handling, so an unserialisable value there
    escapes as a bare HTTP 500 with no diagnostic. Encoding here lets a
    ``default=`` hook coerce stray HOM objects, and lets a genuine encoding
    failure come back as a readable error instead of a blank 500.
    """
    try:
        body = json.dumps(payload, default=json_default)
    except Exception as exc:
        body = json.dumps(
            {
                "status": "error",
                "error": {
                    "code": "SERIALIZATION_ERROR",
                    "message": (f"Result could not be JSON-encoded: {type(exc).__name__}: {exc}"),
                    "traceback": traceback.format_exc(),
                },
            }
        )
    return hwebserver.Response(body.encode("utf-8"), 200, "application/json")


###### Request origin guard

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def _bare_host(host: str) -> str:
    """Strip a trailing :port from a Host value, IPv6 brackets included."""
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    if host.count(":") == 1:
        return host.rsplit(":", 1)[0]
    return host


def _headers(request) -> dict[str, str]:
    try:
        return {str(k).lower(): str(v) for k, v in dict(request.headers()).items()}
    except Exception:
        return {}


def _foreign_request_reason(request) -> str | None:
    """Why *request* must not reach the dispatcher, or None if it may.

    Binding to loopback keeps the LAN out but not the browser: a page you have
    open can POST a form-encoded body to 127.0.0.1 without any CORS preflight,
    and this endpoint runs arbitrary Python. Browsers always send ``Origin`` on
    a cross-origin POST and the MCP bridge never does, so the header alone is
    the tell. The ``Host`` check closes DNS rebinding, where a hostname the
    attacker controls resolves to 127.0.0.1: unless FXHOUDINIMCP_BIND was
    widened on purpose, only a loopback host name is served.
    """
    headers = _headers(request)
    if "origin" in headers:
        return (
            f"request carries an Origin header ({headers['origin']}); "
            f"browsers are not clients of this endpoint"
        )

    if os.environ.get("FXHOUDINIMCP_BIND", "127.0.0.1") != "127.0.0.1":
        return None
    try:
        host = str(request.host())
    except Exception:
        host = headers.get("host", "")
    bare = _bare_host(host).lower()
    if bare and bare not in _LOOPBACK_HOSTS:
        return f"Host header '{host}' is not loopback"
    return None


def _error(status: int, code: str, message: str) -> hwebserver.Response:
    body = json.dumps({"status": "error", "error": {"code": code, "message": message}})
    return hwebserver.Response(body.encode("utf-8"), status, "application/json")


def _refusal(request) -> hwebserver.Response | None:
    """The response refusing *request*, or None if it may be served.

    Applied to every endpoint: session_info returns the hip path, so even the
    read-only ones are worth keeping from a browser or another local user.
    """
    reason = _foreign_request_reason(request)
    if reason is not None:
        return _error(403, "FORBIDDEN_ORIGIN", reason)
    if not instance.authorized(_headers(request).get("authorization")):
        return _error(
            401,
            "UNAUTHORIZED",
            "missing or wrong bearer token; the MCP server reads it from the "
            "instance descriptor that Houdini writes on start",
        )
    return None


def _stopped() -> hwebserver.Response:
    return _error(503, "SERVER_STOPPED", "the FXHoudini-MCP server was stopped in Houdini")


###### Crash detection

_LOADED_AT = time.time()
_crash_log: str | None = None


def _crashed() -> bool:
    """Whether Houdini has written a crash log for this process since load.

    After a crash Houdini sits on its crash dialog with the process alive, and
    this worker thread keeps serving, so health alone would answer "ok" for
    ever. The log is named crash.<hip>.<user>_<pid>_log.txt in the Houdini
    temp directory; the time check skips one left by an earlier process that
    had the same pid.
    """
    global _crash_log
    if _crash_log is not None:
        return True
    directory = os.environ.get("HOUDINI_TEMP_DIR") or os.path.join(
        tempfile.gettempdir(), "houdini_temp"
    )
    for path in glob.glob(os.path.join(glob.escape(directory), f"crash.*_{os.getpid()}_log.txt")):
        try:
            if os.path.getmtime(path) >= _LOADED_AT:
                _crash_log = path
                return True
        except OSError:
            continue
    return False


###### Endpoints


@_api_function("mcp")
def execute(request, command="", params=None, request_id=""):
    """Single entry point for all MCP tool calls.

    Args:
        request: hwebserver.Request (always first arg).
        command: Dotted command name (e.g. "scene.get_scene_info").
        params: Tool-specific parameters dict.
        request_id: Correlation ID echoed back in the response.
    """
    refusal = _refusal(request)
    if refusal is not None:
        return refusal
    if not startup.is_accepting():
        return _stopped()
    if params is None:
        params = {}

    result = dispatcher.dispatch(command, params)
    result["request_id"] = request_id
    return _json_response(result)


@_api_function("mcp")
def health(request):
    """Liveness check. Deliberately free of any hou.* access.

    This is what startup polls to decide the server is ready, and hwebserver
    serves it from a worker thread. Touching HOM here deadlocks a GUI session:
    the main thread is inside startup's readiness loop and so is not running
    Houdini's event loop, while HOM access from the worker needs precisely
    that main thread to make progress. Neither side can advance.

    Version comes from the environment for the same reason -- Houdini exports
    HOUDINI_VERSION, so reporting it costs no HOM call. Anything needing the
    scene itself belongs in session_info.

    A stopped server still answers, as "stopped", so a restart in the same
    session recognises its own port; the MCP client skips anything not "ok".
    "crashed" lets a client waiting on a command give up on it.
    """
    refusal = _refusal(request)
    if refusal is not None:
        return refusal
    if _crashed():
        status = "crashed"
    elif startup.is_accepting():
        status = "ok"
    else:
        status = "stopped"
    return {
        "status": status,
        "pid": os.getpid(),
        "houdini_version": os.environ.get("HOUDINI_VERSION", "unknown"),
    }


@_api_function("mcp")
def session_info(request):
    """Scene-level session details, marshalled to the main thread.

    Separate from health because this does touch HOM: it goes through the
    normal dispatch path, so it is only safe once the session is idle.
    """
    refusal = _refusal(request)
    if refusal is not None:
        return refusal
    if not startup.is_accepting():
        return _stopped()
    return _json_response(dispatcher.dispatch("scene.get_scene_info", {}))


@_api_function("mcp")
def list_commands(request):
    """List all registered command names for introspection."""
    refusal = _refusal(request)
    if refusal is not None:
        return refusal
    if not startup.is_accepting():
        return _stopped()
    return {"commands": dispatcher.list_commands()}
