"""Argument-coercion tests for the `update_checklist` harness tool.

Everything here is pure parsing -- no model, no network, no environment. The
point is the failure mode this tool exists to remove: a plan the model actually
wrote being discarded by the layer that reads it. The regex channel lost 109
good steps that way in a single 10-episode arm (numbered items instead of
checkboxes, and a missing `skill` attribute). These tests pin down that the
tool channel tolerates the equivalent shape variation in tool arguments.

    .venv/bin/python -m pytest tests/test_checklist_tool.py -q
"""

import json

from tau2.orchestrator.checklist_tool import (
    CHECKLIST_TOOL_NAME,
    checklist_ack,
    parse_checklist_call,
)


def _steps(parsed):
    return [it["text"] for it in parsed["items"]]


def test_plain_list():
    got = parse_checklist_call(
        CHECKLIST_TOOL_NAME, {"skill": "replace card", "steps": ["a", "b"]}
    )
    assert _steps(got) == ["a", "b"]
    assert got["skill"] == "replace card"
    assert [it["done"] for it in got["items"]] == [False, False]


def test_arguments_as_json_string():
    """Several backends hand arguments through as an unparsed string."""
    got = parse_checklist_call(
        CHECKLIST_TOOL_NAME, json.dumps({"skill": "s", "steps": ["a"]})
    )
    assert _steps(got) == ["a"]


def test_steps_as_json_string():
    got = parse_checklist_call(
        CHECKLIST_TOOL_NAME, {"skill": "s", "steps": '["a", "b"]'}
    )
    assert _steps(got) == ["a", "b"]


def test_steps_as_newline_text():
    got = parse_checklist_call(
        CHECKLIST_TOOL_NAME, {"skill": "s", "steps": "1. verify id\n2. file dispute"}
    )
    assert _steps(got) == ["1. verify id", "2. file dispute"]


def test_steps_as_objects():
    got = parse_checklist_call(
        CHECKLIST_TOOL_NAME,
        {"skill": "s", "steps": [{"text": "a"}, {"step": "b"}, {"description": "c"}]},
    )
    assert _steps(got) == ["a", "b", "c"]


def test_old_checkbox_syntax_is_stripped():
    """A model carrying the old text format into the tool must not end up with
    '- [ ] ' embedded in the recorded step text."""
    got = parse_checklist_call(
        CHECKLIST_TOOL_NAME, {"skill": "s", "steps": ["- [ ] a", "- [x] b", "- c"]}
    )
    assert _steps(got) == ["a", "b", "c"]


def test_done_indexes():
    got = parse_checklist_call(
        CHECKLIST_TOOL_NAME, {"skill": "s", "steps": ["a", "b", "c"], "done": [0, 2]}
    )
    assert [it["done"] for it in got["items"]] == [True, False, True]


def test_done_as_json_string_and_scalar():
    got = parse_checklist_call(
        CHECKLIST_TOOL_NAME, {"skill": "s", "steps": ["a", "b"], "done": "[1]"}
    )
    assert [it["done"] for it in got["items"]] == [False, True]
    got = parse_checklist_call(
        CHECKLIST_TOOL_NAME, {"skill": "s", "steps": ["a", "b"], "done": 0}
    )
    assert [it["done"] for it in got["items"]] == [True, False]


def test_missing_skill_gets_placeholder():
    got = parse_checklist_call(CHECKLIST_TOOL_NAME, {"steps": ["a"]})
    assert got["skill"] == "(unnamed)"


def test_unusable_calls_return_none():
    """None means 'no plan recorded'. It must never be backfilled -- an episode
    whose tool call was unusable is not a tool-arm observation."""
    assert (
        parse_checklist_call(CHECKLIST_TOOL_NAME, {"skill": "s", "steps": []}) is None
    )
    assert parse_checklist_call(CHECKLIST_TOOL_NAME, {"skill": "s"}) is None
    assert parse_checklist_call(CHECKLIST_TOOL_NAME, "not json at all") is None
    assert parse_checklist_call("some_other_tool", {"steps": ["a"]}) is None


def test_ack_is_small_and_carries_no_judgment():
    """The ack is harness-generated text the model conditions on and that must
    be loss-masked in training, so it stays minimal -- and it must never carry
    a may_finish / completeness verdict (training-plan doc §4)."""
    parsed = parse_checklist_call(CHECKLIST_TOOL_NAME, {"skill": "s", "steps": ["a"]})
    ack = json.loads(checklist_ack(parsed))
    assert ack == {"recorded": True, "skill": "s", "n_steps": 1, "n_done": 0}
    assert json.loads(checklist_ack(None))["recorded"] is False
