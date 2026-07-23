"""The workflow checklist as a TOOL CALL instead of a text block.

Why this exists
---------------
Every version of the checklist so far has been prose the agent writes into its
own turn and a regex tries to recover:

    <workflow_checklist skill="...">
    - [ ] step
    </workflow_checklist>

Measured on the control arm of the 2026-07-27 ablation, that channel
established a plan in **1 episode out of 10**. The failures were not the model
refusing to plan -- three separate extraction bugs discarded plans it had
already written (numbered items, missing `skill` attribute, an enforcement
retry that overwrote its own format example). Those are fixed, but the channel
is still free text: it can be malformed, it can land in the visible reply, and
"did the model plan" is answered by a regex rather than by the protocol.

A tool call has none of those properties. The chat template emits it in a
structured slot, the arguments are JSON, and the call is either present or
absent -- there is no partially-recoverable plan.

Why a tool rather than `response_format` (the planner arm)
----------------------------------------------------------
`src/tau2/orchestrator/planner.py` gets structure from a schema-constrained
side call. Two things rule that out as the main path here:

1. It cannot run on the tinker backend at all -- `tinker.types.SamplingParams`
   has no `response_format` and no grammar. The tinker path is the one that
   actually runs today, and it is also the training path.
2. On the API path it is currently dead anyway: the LeanMCP gateway drops
   `response_format` (commit ffa6893c), and the Fireworks route is returning
   401 on both arms.

A tool call needs neither. On tinker, tool calls are parsed out of generated
text by `TAU2_WITH_TINKER/tool_bridge.py` exactly like every environment tool,
so `update_checklist` is representable with zero backend support. The same code
path then works unchanged on any API provider that supports tools.

And it is the same shape the *training* phase needs: an assistant tool call is
a trainable token span, whereas "did a regex find a block in the thinking span"
is not something a loss can be defined against cleanly.

What this deliberately does NOT do
----------------------------------
No `may_finish`, no gating of episode completion on checklist state, no reward
contribution. See `docs/checklist_training_phase_plan.md` §4: any signal that
reads checklist state is a signal the model learns to fabricate. The tool
records; it never judges. The ack payload is intentionally minimal for the
same reason -- it is harness-generated text that must be loss-masked during
training, so the less of it there is the better.

Patch semantics (stable step ids, `blocked_by`) are what the scoping doc
argues for and are NOT here. V1 is replace-per-skill, matching what the regex
path already did, so that a tool-vs-text comparison changes exactly one thing.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from loguru import logger

CHECKLIST_TOOL_NAME = "update_checklist"


def update_checklist(
    skill: str,
    steps: list[str],
    done: Optional[list[int]] = None,
) -> str:
    """Record or update your working checklist for this conversation.

    Call this as soon as you can see that resolving the customer's request will
    take more than one action. Call it again whenever the plan changes or steps
    are completed -- pass the FULL step list each time, not just the delta.

    This is your own private tracking note. The customer never sees it, and
    calling it does not replace giving them a reply: after this call you still
    handle the request as normal.

    Args:
        skill: Short name for this specific request, e.g. "replace stolen debit card".
        steps: The ordered steps, specific to THIS conversation -- name the actual
            accounts, cards and amounts involved rather than restating a generic
            procedure. One step if one action is all it takes; do not pad.
        done: Indexes (0-based, into `steps`) of the steps already completed.

    Returns:
        Confirmation of what was recorded.
    """
    items = _coerce_steps(steps)
    done_set = set(_coerce_done(done))
    return json.dumps(
        {
            "recorded": True,
            "skill": skill,
            "n_steps": len(items),
            "n_done": sum(1 for i in range(len(items)) if i in done_set),
        }
    )


def get_checklist_tool():
    """Build the tau2 `Tool` wrapper. Imported lazily by callers so that nothing
    in the default (checklist-tool-off) path pays for it."""
    from tau2.environment.tool import Tool

    return Tool(update_checklist)


# --------------------------------------------------------------------------- #
# Argument coercion
# --------------------------------------------------------------------------- #
# The models under test emit tool calls as generated text that tool_bridge.py
# regex-parses, so arguments arrive in whatever shape the model produced. Being
# strict here would recreate exactly the failure this module exists to remove:
# a perfectly good plan discarded because it came back as a JSON *string*
# rather than a list, or as [{"text": ...}] rather than ["..."]. Every tolerated
# shape below is a real shape observed in this repo's traces or a trivial
# neighbour of one. Anything genuinely unusable still returns [] -- and an empty
# checklist is recorded as "no plan", never silently backfilled.


def _coerce_steps(steps: Any) -> list[str]:
    """Normalise `steps` into a flat list of non-empty strings."""
    if isinstance(steps, str):
        # A model that wrote the array as a JSON string, or as newlines.
        try:
            parsed = json.loads(steps)
        except (json.JSONDecodeError, TypeError):
            parsed = [line for line in steps.splitlines()]
        steps = parsed if isinstance(parsed, list) else [steps]
    if not isinstance(steps, (list, tuple)):
        return []
    out: list[str] = []
    for s in steps:
        if isinstance(s, dict):
            # {"text": ...} / {"step": ...} / {"description": ...}
            s = s.get("text") or s.get("step") or s.get("description") or ""
        if not isinstance(s, str):
            s = str(s)
        # Strip any checklist syntax the model carried over from the old text
        # format ("- [ ] ", "1. ") so the stored text is the step itself.
        s = s.strip().lstrip("-*").strip()
        for prefix in ("[ ]", "[x]", "[X]"):
            if s.startswith(prefix):
                s = s[len(prefix) :].strip()
        if s:
            out.append(s)
    return out


def _coerce_done(done: Any) -> list[int]:
    """Normalise `done` into a list of 0-based indexes."""
    if done is None:
        return []
    if isinstance(done, str):
        try:
            done = json.loads(done)
        except (json.JSONDecodeError, TypeError):
            return []
    if isinstance(done, (int, bool)):
        done = [done]
    if not isinstance(done, (list, tuple)):
        return []
    out: list[int] = []
    for d in done:
        try:
            out.append(int(d))
        except (TypeError, ValueError):
            continue
    return out


def parse_checklist_call(name: str, arguments: Any) -> Optional[dict]:
    """Turn one `update_checklist` tool call into the internal checklist shape.

    Returns None when this is not a checklist call, or when it carried no
    usable steps. The caller must treat None as "no plan recorded" rather than
    substituting anything -- an episode where the tool call was unusable is not
    a tool-arm observation, same rule as `pinned_plan_empty`.
    """
    if name != CHECKLIST_TOOL_NAME:
        return None
    args = arguments
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                f"{CHECKLIST_TOOL_NAME}: arguments were not JSON: {args[:200]!r}"
            )
            return None
    if not isinstance(args, dict):
        return None

    items_text = _coerce_steps(args.get("steps"))
    if not items_text:
        return None
    done_set = set(_coerce_done(args.get("done")))
    skill = args.get("skill")
    skill = (skill.strip() if isinstance(skill, str) else "") or "(unnamed)"
    return {
        "skill": skill,
        "items": [{"text": t, "done": i in done_set} for i, t in enumerate(items_text)],
    }


def checklist_ack(checklist: Optional[dict]) -> str:
    """The tool result the agent sees. Harness-generated -- keep it minimal and
    remember it must be loss-masked in training."""
    if checklist is None:
        return json.dumps(
            {
                "recorded": False,
                "error": "no usable steps; pass steps as a list of strings",
            }
        )
    return json.dumps(
        {
            "recorded": True,
            "skill": checklist["skill"],
            "n_steps": len(checklist["items"]),
            "n_done": sum(1 for it in checklist["items"] if it["done"]),
        }
    )
