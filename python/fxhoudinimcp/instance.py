"""Read the bearer token a Houdini session publishes for its port.

The plugin writes ``<state dir>/instances/<port>.json`` on start and removes it
on stop. The state directory must match
``fxhoudinimcp_server.instance.state_dir`` on the Houdini side.
"""

from __future__ import annotations

# Built-in
import json
import os
import sys
from pathlib import Path


def state_dir() -> Path:
    override = os.environ.get("FXHOUDINIMCP_STATE_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "fxhoudinimcp"


def descriptor_for(port: int) -> dict | None:
    """The descriptor the Houdini on *port* published, or None."""
    try:
        data = json.loads((state_dir() / "instances" / f"{port}.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def token_for(port: int) -> str | None:
    """The token for the Houdini on *port*, or None if none is published.

    FXHOUDINIMCP_TOKEN wins when set: a Houdini on another machine cannot hand
    over its descriptor, so both ends are given the same token instead.
    """
    fixed = os.environ.get("FXHOUDINIMCP_TOKEN")
    if fixed:
        return fixed
    token = (descriptor_for(port) or {}).get("token")
    return token if isinstance(token, str) and token else None


def process_alive(pid: int) -> bool:
    """Whether a process with *pid* is running; True when it cannot be told.

    On Windows os.kill(pid, 0) is TerminateProcess, so the handle is queried
    instead.
    """
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True

    import ctypes

    kernel32 = ctypes.windll.kernel32
    process_query_limited_information = 0x1000
    still_active = 259
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # Access denied still means the process exists.
        return ctypes.GetLastError() == 5
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def auth_headers(port: int) -> dict[str, str]:
    token = token_for(port)
    return {"Authorization": f"Bearer {token}"} if token else {}
