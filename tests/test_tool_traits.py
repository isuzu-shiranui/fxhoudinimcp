"""Read-only annotations and tool groups."""

from __future__ import annotations

# Third-party
import pytest

# Internal
import fxhoudinimcp.tools  # noqa: F401  (registers all tools on import)
from fxhoudinimcp import tool_traits
from fxhoudinimcp.server import mcp


@pytest.mark.asyncio
async def test_read_only_names_are_real_tools_and_carry_the_hint():
    """A renamed tool would otherwise drop out of READ_ONLY without a sound."""
    tools = {t.name: t for t in await mcp.list_tools()}
    assert tools.keys() >= tool_traits.READ_ONLY, tool_traits.READ_ONLY - tools.keys()
    # By alias, as the client receives it: mcp 2 renamed the attribute to read_only_hint.
    hinted = {
        n
        for n, t in tools.items()
        if t.annotations and t.annotations.model_dump(by_alias=True).get("readOnlyHint")
    }
    assert hinted == tool_traits.READ_ONLY


def _in(module: str):
    def function():
        pass

    function.__module__ = f"fxhoudinimcp.tools.{module}"
    return function


@pytest.mark.parametrize("value", [None, "", "all", "ALL"])
def test_every_group_is_registered_by_default(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("FXHOUDINIMCP_TOOL_GROUPS", raising=False)
    else:
        monkeypatch.setenv("FXHOUDINIMCP_TOOL_GROUPS", value)
    assert tool_traits.is_registered(_in("lops"))


def test_selected_groups_add_to_the_core(monkeypatch):
    monkeypatch.setenv("FXHOUDINIMCP_TOOL_GROUPS", "lops, TOPS")
    assert tool_traits.is_registered(_in("lops"))
    assert tool_traits.is_registered(_in("tops"))
    assert tool_traits.is_registered(_in("graph"))
    assert not tool_traits.is_registered(_in("dops"))


def test_restricted_header_keeps_the_instructions_within_the_client_cap(monkeypatch):
    """The text already fills the ~2,000 characters the client shows."""
    from fxhoudinimcp._loader import load_markdown

    full = load_markdown("instructions/server_instructions.md")
    monkeypatch.setenv("FXHOUDINIMCP_TOOL_GROUPS", "lops")
    restricted = tool_traits.with_group_header(full)
    assert "only some groups on" in restricted
    assert len(restricted) <= len(full)
    assert "dops" in tool_traits.groups_off() and "lops" not in tool_traits.groups_off()
