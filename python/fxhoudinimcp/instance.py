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


def token_for(port: int) -> str | None:
    """The token for the Houdini on *port*, or None if none is published.

    FXHOUDINIMCP_TOKEN wins when set: a Houdini on another machine cannot hand
    over its descriptor, so both ends are given the same token instead.
    """
    fixed = os.environ.get("FXHOUDINIMCP_TOKEN")
    if fixed:
        return fixed
    try:
        data = json.loads((state_dir() / "instances" / f"{port}.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None
    token = data.get("token") if isinstance(data, dict) else None
    return token if isinstance(token, str) and token else None


def auth_headers(port: int) -> dict[str, str]:
    token = token_for(port)
    return {"Authorization": f"Bearer {token}"} if token else {}
