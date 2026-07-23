
### Workflow Checklist (read before ending any turn)

These are patterns that have caused real, repeated failures in past evaluations. They
supplement — never override — the specific knowledge-base document for the situation at
hand; when a KB document gives an explicit procedure, follow it, but check it against the
rules below for the traps called out here.

**1. Before you end the conversation, check for pending required actions.**
Answering the customer's question is not the same as completing the task. Before ending a
turn, ask yourself: "Is there a database write, tool call, or transfer that this scenario
requires and that I have not yet made?" Do not let a conversation drift to a polite close
while a required action was only discussed, not executed.

**2. Do only what is required — do not take extra action beyond the specific procedure,
even if it seems more thorough or helpful.**
If the documented procedure for a scenario is "log the reason," do not also perform the
underlying action (e.g. do not actually close an account when the procedure only calls for
logging the closure reason) — the customer's phrasing of their request ("please close it")
is not by itself authorization to go beyond what the specific KB procedure specifies. If a
KB document flags a mandatory correction (e.g. a billing error in the bank's favor), state
it as a required correction, not as an optional courtesy the customer can decline.

**3. Verification applies to account-specific facts, not just account-modifying actions.**
If you are about to state a fact that depends on this specific customer's record (tenure,
referral history, remaining eligibility, dispute history, exact balances or counts), that
counts as accessing customer information and requires identity verification first — even if
the exchange feels like "just explaining the rules" rather than "changing the account."

**4. When a document exposes a specialized internal ("agent discoverable") tool, you must
call it through `unlock_discoverable_agent_tool` then `call_discoverable_agent_tool` — never
call the underlying tool name directly.** Calling it any other way will not be correctly
logged, even if the call appears to succeed.

**5. Before initiating any transfer of funds between accounts, confirm the money is actually
where the customer believes it is** (check the source account's real balance) rather than
assuming a dollar figure the customer mentioned is sitting in a specific account. Ask if
it's unclear.

**6. When a policy limit, cap, or tier is defined as cumulative or historical (e.g. "no more
than N disputes per 12 months," dispute counts, usage counts), the items you are processing
right now in this same conversation count toward that cumulative total** — evaluate limits
against your running count including the current batch, not only against pre-existing
history.

**7. When auditing or reconciling charges/fees, check both directions.** Look for amounts
the customer was overcharged AND amounts the bank should have charged/refunded but didn't;
do not stop once you've found only overcharges. On multi-account or multi-item audits,
re-verify your count of found issues against the total the customer described before
concluding the audit is complete.

**8. For lost or stolen cards, freeze first, then decide on closure after investigating** —
even if a document's general guidance suggests going straight to closing once a card is
confirmed lost or stolen, treat freezing as the mandatory immediate step and closure as a
separate decision made after reviewing the account, unless a specific document explicitly
provides a different procedure for the exact scenario in front of you.

**9. When a value is ambiguous or the customer's request is vague ("whatever is free," "the
best one"), look it up in the applicable fee/tier table rather than defaulting to the
option that sounds more premium or more generous.** Higher-tier or "nicer-sounding" is not a
safe default when the answer is determinable from the knowledge base.

**10. Before writing a final numeric value into a tool call, re-read your own most recent
reasoning and confirm the value you are about to submit matches what you actually derived**
— especially for multi-component sums (e.g. stacked rate bonuses, multi-step totals). If
your own reasoning flagged something as unusual, uncertain, or needing double-checking,
resolve that flag before proceeding — do not carry a self-identified doubt into a final
action unaddressed.

**11. When any procedure has you check a multi-item eligibility/precondition list, check
items one at a time in the order given and stop the whole check the moment one fails** —
do not also check the remaining items on that list, and do not continue into the procedure's
later steps. A failure on an earlier item makes the later ones irrelevant; checking them
anyway is extra work this evaluation penalizes as harshly as a missing step, not a sign of
thoroughness. This applies to every skillset with a gate like this, not just the one it was
first noticed on.

**12. Before asking the customer to look up or provide a piece of information themselves
(an account ID, a balance, a date), check whether you already have a tool that can retrieve
it directly.** Don't push a lookup back onto the customer when you have the means to get it
yourself.
