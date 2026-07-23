"""Structured-output planner: elicit the checklist as schema-constrained JSON.

Why this exists
---------------
Every previous attempt asked the agent to emit a `<workflow_checklist>` block
inside its normal reply and then recovered it with a regex. That approach failed
three different ways in one week, all of them in the extraction layer rather
than the model: the enforcement retry overwrote the SystemMessage holding the
format example, the parser demanded a `skill="..."` attribute the model often
omitted, and it demanded `- [ ]` items while the model wrote `1.`. Blocks that
were perfectly good plans were discarded, and the harness then "enforced"
retries against episodes that had already complied.

The fix for a brittle extractor is not a better regex. It is to stop extracting.
A schema-constrained call cannot return a malformed plan: either the provider
returns JSON matching the schema or it errors. There is no partial-credit
failure mode where a good plan is silently thrown away.

Why a SEPARATE call rather than constraining the agent's own turn
------------------------------------------------------------------
`response_format` constrains the WHOLE response. The agent's turn has to be free
to emit a tool call or customer-facing prose, so it cannot also be JSON. Making
the plan its own call keeps the main conversation untouched: the planner runs,
returns a plan, and the plan is injected through the existing plan-pinning path
(orchestrator.PINNED_PLAN_TEMPLATE) exactly like the null and oracle arms. That
also makes the arms directly comparable -- same delivery, same echo, only the
plan's source differs.

Backend requirements
--------------------
Needs a provider that actually supports constrained decoding. Verified via
litellm 2026-07-27: `fireworks_ai` reports `response_format`, `tools` and
`tool_choice` among its supported params. The tinker path does NOT --
`tinker.types.SamplingParams` carries only max_tokens/seed/stop/temperature/
top_k/top_p, and tool calls there are regex-parsed from generated text
(TAU2_WITH_TINKER/tool_bridge.py:197-203). So this module is meaningless on
tinker and the caller must point it at an API-served model.

The same weights are served in different precisions (tinker BF16 vs Fireworks
NVFP4), so a Fireworks run is NOT comparable to stored tinker numbers. Any
experiment using this planner needs its own unconstrained baseline on the same
backend.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from loguru import logger

#: JSON Schema for a workflow plan. Deliberately small: a name plus ordered
#: steps, each with the tool the model expects to use. `expected_tool` is not
#: decoration -- it is what makes a self-authored plan comparable to the gold
#: action sequence without an LLM judge (see the scoping doc's section 7.3), and
#: asking for it costs nothing once the response is schema-constrained anyway.
PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "skill": {
            "type": "string",
            "description": "Short name for this specific request.",
        },
        "steps": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "One concrete step, specific to THIS conversation.",
                    },
                    "expected_tool": {
                        "type": ["string", "null"],
                        "description": (
                            "The tool you expect to call for this step, or null "
                            "if the step is a question to the customer."
                        ),
                    },
                },
                "required": ["text", "expected_tool"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["skill", "steps"],
    "additionalProperties": False,
}

RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {"name": "workflow_plan", "strict": True, "schema": PLAN_SCHEMA},
}

PLANNER_SYSTEM_PROMPT = (
    "You are the planning component of a customer-service agent. You do NOT "
    "talk to the customer and you do NOT call tools. Your only job is to read "
    "the conversation so far and produce the checklist the agent should work "
    "through to resolve the customer's request.\n\n"
    "Write steps that are specific to THIS conversation -- name the actual "
    "accounts, cards and amounts involved rather than restating a generic "
    "procedure. Order them the way they must actually be performed. If the "
    "request needs only one action, one step is the correct answer; do not pad."
)


@dataclass
class PlannerResult:
    """Outcome of one planner call.

    ``ok`` false means no plan was produced. The caller must NOT fall back to
    some other plan source in that case -- an episode whose planner failed is
    not a planner-arm episode, and silently substituting would relabel it. Record
    it and exclude it, the same way the oracle arm excludes empty pins.
    """

    ok: bool
    skill: Optional[str] = None
    steps: Optional[list[dict]] = None
    error: Optional[str] = None
    raw: Optional[str] = None

    def as_checklist(self) -> list[dict]:
        """Shape the plan the way Orchestrator._workflow_checklists expects, so
        it flows through the existing pinning/echo path unchanged."""
        if not self.ok or not self.steps:
            return []
        return [
            {
                "skill": self.skill or "(unnamed)",
                "items": [
                    {"text": s.get("text", ""), "done": False}
                    for s in self.steps
                    if s.get("text")
                ],
            }
        ]


def _render_conversation(messages: list, max_chars: int = 12000) -> str:
    """Flatten the trajectory for the planner.

    Keeps the TAIL when truncating: the planner is deciding what to do next, and
    the most recent turns carry the request being served. Tool RESULTS are
    dropped -- they are the bulk of the tokens and the planner is choosing steps,
    not reading data. Dropping them is also what keeps this call cheap enough to
    run every turn.
    """
    parts: list[str] = []
    for m in messages:
        role = getattr(m, "role", None) or (
            m.get("role") if isinstance(m, dict) else None
        )
        if role not in ("user", "assistant"):
            continue
        content = getattr(m, "content", None) or (
            m.get("content") if isinstance(m, dict) else None
        )
        calls = getattr(m, "tool_calls", None) or (
            m.get("tool_calls") if isinstance(m, dict) else None
        )
        who = "CUSTOMER" if role == "user" else "AGENT"
        if content:
            parts.append(f"{who}: {content}")
        if calls:
            names = []
            for c in calls:
                n = getattr(c, "name", None) or (
                    c.get("name") if isinstance(c, dict) else None
                )
                if n:
                    names.append(n)
            if names:
                parts.append(f"{who} called: {', '.join(names)}")
    text = "\n".join(parts)
    return text[-max_chars:] if len(text) > max_chars else text


def make_plan(
    messages: list,
    *,
    model: str,
    domain_policy: Optional[str] = None,
    temperature: float = 0.0,
    max_tokens: int = 1200,
    extra_llm_args: Optional[dict] = None,
) -> PlannerResult:
    """One schema-constrained planning call.

    Never raises: a planner failure must not take down the episode, because the
    comparison it feeds needs the episode's outcome either way. Failures come
    back as ``ok=False`` with the reason attached so the scorer can exclude them
    explicitly rather than have them disappear into a fallback.
    """
    import litellm

    convo = _render_conversation(messages)
    if not convo.strip():
        return PlannerResult(ok=False, error="empty_conversation")

    user_block = (
        (f"Agent policy (for context):\n{domain_policy}\n\n" if domain_policy else "")
        + f"Conversation so far:\n{convo}\n\n"
        "Produce the checklist for resolving the customer's request."
    )

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": user_block},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": RESPONSE_FORMAT,
    }
    kwargs.update(extra_llm_args or {})

    try:
        resp = litellm.completion(**kwargs)
        raw = resp.choices[0].message.content or ""
    except Exception as e:  # noqa: BLE001 -- see docstring
        logger.warning(f"planner call failed: {type(e).__name__}: {e}")
        return PlannerResult(ok=False, error=f"{type(e).__name__}: {e}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        # Should be impossible under a strict schema. If it happens, the
        # provider is not honouring response_format -- which is exactly the
        # silent failure this whole module exists to avoid, so it is surfaced
        # loudly rather than repaired.
        logger.warning(f"planner returned non-JSON despite response_format: {e}")
        return PlannerResult(ok=False, error=f"non_json: {e}", raw=raw[:500])

    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        return PlannerResult(ok=False, error="no_steps", raw=raw[:500])

    return PlannerResult(
        ok=True,
        skill=(data.get("skill") or "").strip() or None,
        steps=[s for s in steps if isinstance(s, dict) and s.get("text")],
        raw=raw[:2000],
    )
