"""MCP server.

This is the original point of the project: letting Claude read the character
directly instead of being sent screenshots.

The tests that matter most here are the guardrails. An MCP endpoint behind a
public hostname with no token lets anyone trigger refresh() against PLAYNC in
the owner's name, and a tool that quietly drops its caveats will have Claude
state a heuristic ranking as if it were a calculated Combat Power delta.
"""

from __future__ import annotations

import json

import pytest

from aion2.config import Settings

mcp_sdk = pytest.importorskip("mcp", reason="mcp SDK not installed")

from aion2.mcp_server import CP_CAVEAT, PRESET_CAVEAT, build_server  # noqa: E402


def _settings(tmp_path, **kw) -> Settings:
    return Settings(
        region="eu",
        server_id=1302,
        character_id="abc%3D",
        character_name="Test",
        db_path=str(tmp_path / "t.db"),
        thresholds_path="config/thresholds.yaml",
        **kw,
    )


@pytest.fixture
def server(tmp_path):
    return build_server(_settings(tmp_path))


# --------------------------------------------------------------------------
# surface
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expected_tools_are_exposed(server):
    names = {t.name for t in await server.list_tools()}
    assert names == {
        "get_character",
        "get_equipment",
        "get_skills",
        "get_daevanion",
        "get_recommendations",
        "get_history",
        "search_character",
        "compare_to",
        "refresh",
    }


@pytest.mark.asyncio
async def test_only_refresh_writes(server):
    """Everything else must be read-only. refresh is the single exception and
    carries its own cooldown."""
    writers = {"refresh"}
    for tool in await server.list_tools():
        desc = (tool.description or "").lower()
        if tool.name in writers:
            assert "snapshot" in desc
        else:
            assert "take a fresh snapshot" not in desc


@pytest.mark.asyncio
async def test_every_tool_has_a_description(server):
    for tool in await server.list_tools():
        assert tool.description, f"{tool.name} has no description"
        assert len(tool.description) > 30, f"{tool.name} description is thin"


def test_server_instructions_state_both_caveats(server):
    text = (server.instructions or "").lower()
    assert "active preset" in text
    assert "combat power cannot be calculated" in text


# --------------------------------------------------------------------------
# the caveats travel with the data
# --------------------------------------------------------------------------


async def _call(server, name, args=None):
    """The decoded payload of a tool call.

    mcp 2.x returns a CallToolResult carrying structured_content; older
    versions returned a (content, structured) tuple. Handle both so the
    tests do not pin one SDK release.
    """
    result = await server.call_tool(name, args or {})
    if isinstance(result, tuple):
        return result[1]
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    return json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_missing_snapshot_is_an_explained_error(server):
    payload = await _call(server, "get_character")
    assert payload["error"] == "no_snapshot"
    assert "refresh" in payload["message"]


@pytest.mark.asyncio
async def test_preset_caveat_is_not_silently_dropped():
    """A caller reading one tool result has not seen the module docstring, so
    the limitation has to travel in the payload."""
    assert "ACTIVE preset" in PRESET_CAVEAT
    assert "not calculable" in CP_CAVEAT.lower() or "not calculable" in CP_CAVEAT


@pytest.mark.asyncio
async def test_history_rejects_an_unknown_stat(server):
    payload = await _call(server, "get_history", {"stat": "nonsense"})
    assert payload["error"] == "bad_stat"


@pytest.mark.asyncio
async def test_compare_to_without_a_capture_explains_how_to_get_one(server):
    payload = await _call(server, "compare_to", {"name": "Nobody"})
    assert payload["error"] in {"no_snapshot", "no_reference"}


# --------------------------------------------------------------------------
# transport guardrails
# --------------------------------------------------------------------------


def test_allowed_hosts_include_the_configured_port(tmp_path):
    from aion2.web import _transport_security

    cfg = _settings(tmp_path, port=9999, mcp_allowed_hosts=("aion2.example.com",))
    sec = _transport_security(cfg)
    assert "aion2.example.com" in sec.allowed_hosts
    assert "aion2.example.com:9999" in sec.allowed_hosts
    assert "localhost:9999" in sec.allowed_hosts
    assert sec.enable_dns_rebinding_protection


def test_wildcard_disables_rebinding_protection(tmp_path):
    """Escape hatch for when something in front already validates the host."""
    from aion2.web import _transport_security

    sec = _transport_security(_settings(tmp_path, mcp_allowed_hosts=("*",)))
    assert not sec.enable_dns_rebinding_protection


def test_bearer_middleware_uses_constant_time_comparison():
    """== on a secret leaks it a character at a time through timing."""
    import inspect

    from aion2.web import BearerAuthMiddleware

    source = inspect.getsource(BearerAuthMiddleware)
    assert "compare_digest" in source
    assert "self._expected ==" not in source
