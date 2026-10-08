"""Per-session bearer token and the descriptor file that publishes it.

The bridge runs arbitrary Python, so loopback binding alone would hand the
session to any local process. Each start generates a token and writes it, with
the port and pid, to ``<state dir>/instances/<port>.json``; the MCP server reads
that file to authenticate. The file lives in the user's profile, so only the
same OS user can read it.

The state directory must match ``fxhoudinimcp.instance.state_dir`` on the MCP
side: the two halves run in different Pythons and cannot share the code.
"""

from __future__ import annotations

# Built-in
import contextlib
import hmac
import json
import os
import secrets
import sys

_token: str | None = None
_descriptor_path: str | None = None


def state_dir() -> str:
    override = os.environ.get("FXHOUDINIMCP_STATE_DIR")
    if override:
        return override
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "fxhoudinimcp")


def token() -> str | None:
    """The token requests must carry, or None before the first start."""
    return _token


def new_token() -> str:
    """Generate this session's token, or take FXHOUDINIMCP_TOKEN when set.

    The fixed token is for a client on another machine, which cannot read the
    descriptor file.
    """
    global _token
    _token = os.environ.get("FXHOUDINIMCP_TOKEN") or secrets.token_hex(32)
    return _token


def authorized(authorization: str | None) -> bool:
    """Whether an ``Authorization`` header value carries this session's token."""
    if _token is None or not authorization:
        return False
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(value.strip().encode("utf-8"), _token.encode("utf-8"))


def publish(port: int, houdini_version: str) -> str:
    """Write the descriptor for *port* and return its path."""
    global _descriptor_path
    directory = os.path.join(state_dir(), "instances")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    path = os.path.join(directory, f"{port}.json")
    body = json.dumps(
        {
            "port": port,
            "pid": os.getpid(),
            "token": _token,
            "houdini_version": houdini_version,
        }
    )
    temp = f"{path}.{os.getpid()}.tmp"
    # Created 0600 before the token is written, so it is never readable by
    # others even for a moment on POSIX. Windows ignores the mode; the profile
    # directory is already private to the user.
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(body)
    os.replace(temp, path)
    _descriptor_path = path
    return path


def withdraw() -> None:
    """Remove this session's descriptor if it is still the one on disk.

    Another Houdini may have taken the port and rewritten the file since, and
    that one must not be deleted.
    """
    global _descriptor_path
    path, _descriptor_path = _descriptor_path, None
    if path is None:
        return
    with contextlib.suppress(OSError, ValueError):
        with open(path, encoding="utf-8") as handle:
            if json.load(handle).get("pid") != os.getpid():
                return
        os.remove(path)
