"""Tests for the Gateway Lambda-target dispatch contract.

Pins the VERIFIED contract facts: tool name lives in client_context.custom prefixed with
'{target}___', the event is a flat args map, client_context can be None, and dispatch routes
on the un-prefixed suffix.
"""
import pytest

from gov_debates.gateway.handler import (
    ToolDispatchError,
    bad_argument,
    dispatch,
    extract_tool_name,
    no_results,
)


class FakeClientContext:
    def __init__(self, custom):
        self.custom = custom


class FakeContext:
    def __init__(self, custom=None, has_client_context=True):
        self.client_context = FakeClientContext(custom or {}) if has_client_context else None


def _ctx(tool):
    return FakeContext(custom={"bedrockAgentCoreToolName": tool})


def test_extract_strips_target_prefix():
    assert extract_tool_name(_ctx("germany___search_debates")) == "search_debates"
    assert extract_tool_name(_ctx("uscongress___get_debate_text")) == "get_debate_text"


def test_extract_tolerates_unprefixed_name():
    assert extract_tool_name(_ctx("search_debates")) == "search_debates"


def test_extract_raises_when_client_context_none():
    with pytest.raises(ToolDispatchError):
        extract_tool_name(FakeContext(has_client_context=False))


def test_extract_raises_when_tool_name_missing():
    with pytest.raises(ToolDispatchError):
        extract_tool_name(FakeContext(custom={"other": "x"}))


def test_dispatch_routes_flat_event_to_tool():
    seen = {}

    def search(args):
        seen.update(args)
        return {"results": [], "total": 0}

    out = dispatch({"query": "Klima", "max_results": 5}, _ctx("germany___search_debates"),
                   {"search_debates": search})
    assert out == {"results": [], "total": 0}
    assert seen == {"query": "Klima", "max_results": 5}


def test_dispatch_unknown_tool_raises():
    with pytest.raises(ToolDispatchError):
        dispatch({}, _ctx("germany___nonexistent"), {"search_debates": lambda a: None})


def test_dispatch_non_mapping_event_becomes_empty_args():
    def tool(args):
        assert args == {}
        return {"results": []}

    assert dispatch(None, _ctx("x___tool"), {"tool": tool})["results"] == []


def test_bad_argument_is_soft_error_with_results_key():
    out = bad_argument("query is required", field="query")
    assert out["error"] == "bad_argument"
    assert out["results"] == [] and out["total"] == 0
    assert out["field"] == "query"


def test_no_results_is_valid_empty_envelope():
    out = no_results("de")
    assert out["results"] == [] and out["jurisdiction"] == "de" and out["truncated"] is False
