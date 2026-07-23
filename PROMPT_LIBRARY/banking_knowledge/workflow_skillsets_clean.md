改
### Named Workflow Skillsets

Below is a fixed library of named workflows. Each entry names the situation that
triggers it, the required step *order*, and the specific traps this procedure is prone to. These supplement —
never override — the specific knowledge-base document for the situation at hand; when a
KB document gives an explicit procedure, follow it, but check it against the ordering
and traps called out here.

**Before you act on a multi-step request, silently ask: "which named skillset below (if
any) does this match?"** If one matches, treat its step list as the checklist for this
conversation, and re-derive "what's done, what's left" from it each time you're asked
— do not treat having read this once at the start as the same as having checked it.

---

**Skillset: dispute filing (credit card)**
*Triggers on*: a customer disputing a credit-card charge, or asking about provisional
credit / eligibility for a charge dispute.
*Sources*: `doc_credit_cards_credit_cards_(general)_014` (Filing a Credit Card
Transaction Dispute), `_015` (Provisional Credit Eligibility Guidelines), `_016`
(Checking User Dispute History).
1. Verify identity if not already verified this session.
2. Call `get_user_dispute_history_7291` (unlock+call) for this account *before*
   filing anything.
3. Determine `eligible_for_provisional_credit` — **all** of the following must hold,
   never inferred or guessed while a check is skipped:
   - account open ≥60 days;
   - `dispute_reason` is `unauthorized_fraudulent_charge`, `duplicate_charge`, or
     `goods_services_not_received` (this last one **only** if the purchase was made
     more than 30 days ago — within 30 days, not eligible, the merchant may still be
     processing delivery);
   - transaction amount ≥$25.00 and ≤ the card tier's provisional-credit max (entry
     $2500 / mid $5000 / premium $10000 / elite $15000 / invitation $25000);
   - fewer than 3 disputes filed in the past 12 months — **call
     `get_current_dispute_count` for this check**, as source document `_015`'s own
     checklist requires. **It is a standard tool: call it directly by name.** Do
     *not* pass it to `call_discoverable_agent_tool` — that wrapper only resolves
     *discoverable* tools, so handing it a standard tool returns an error. (An
     earlier revision of this entry misread exactly that error as proof the tool
     did not exist, and told you to derive the count by hand instead. That was
     wrong; corrected 2026-07-27.) Re-call it fresh before filing **each** dispute
     in a multi-dispute session: every dispute you file is written to the record
     immediately, so a count from earlier in the same conversation is stale;
   - for any reason other than `unauthorized_fraudulent_charge`, the customer
     contacted the merchant first.
4. File via `file_credit_card_transaction_dispute_4829`, setting `card_action` to
   `cancel_and_reissue` only if the card is actually being replaced (whether you
   already ordered the replacement or the cancellation is happening as part of this
   dispute), otherwise `keep_active`.
*Watch for*: a second request in the same session (a CLI request, a card
replacement) that depends on or conflicts with this one — resolve the dependency
before filing either.

**Skillset: card closure + retention**
*Triggers on*: a customer asking to close, cancel, or downgrade a credit card.
*Source*: `doc_credit_cards_credit_card_account_logistics_003` ("Internal: Credit Card
Retention Protocol") — this is that document's own 6-step tree, made explicit so a
branch can't be silently skipped or applied to the wrong card. **If a session touches
more than one card, run this ENTIRE tree separately for each card, one card fully
through its own Step 6 before moving to the next — a card that hits the
abuse-prevention exit at step 2 does not inherit step 3's reason-logging just
because another card in the same session went through it. A failure shape to avoid: running steps 1-5 for every card the customer named, but only ever placing
the actual Step 6 closure call for the first one — the later cards get checked and
discussed but never actually closed (or explicitly kept open). Checking a card is
not the same as resolving it. Before ending the conversation, go back through every
card the customer asked to close and confirm each one individually reached Step 6 —
not just the one you handled first.**

1. **Eligibility gate** — check these four **one at a time, in the order listed**. The
   moment any one of them fails, **stop the eligibility gate right there**: tell the
   customer what's blocking closure and end this check — do not also check the
   remaining items on this list, and do not continue to step 2 (or any later step).
   A failure on an earlier item makes the later items irrelevant; gathering that
   information anyway (e.g. also checking replacement orders after a dispute already
   blocked closure) is unnecessary extra work, not extra thoroughness.
   - Pending/active dispute on this account (check `get_user_dispute_history_7291`,
     unlock+call)? → **stop here**: tell the customer to resolve the dispute first.
   - Pending replacement card not yet received/activated (check
     `get_pending_replacement_orders_5765`, unlock+call)? → **stop here**: closure can't
     proceed until that's resolved.
   - Account open less than 60 days? → **stop here**: not eligible to close yet.
   - Outstanding balance on the account? → **stop here**: must be paid off first.
   - All four clear → continue to step 2.
2. **Abuse-prevention check (this branch is easy to get wrong)**: call `get_closure_reason_history_8293` for *this
   specific* card account. **If it returns a closure-reason record within the past
   12 months** → skip steps 3, 4, and 5 entirely for this card. Do **not** call
   `log_credit_card_closure_reason_4521` again for it. Go directly to step 6. **If
   it returns no such record** → continue to step 3.
3. **Log the reason** (only reached if step 2 found nothing): ask why the customer
   wants to close, then call `log_credit_card_closure_reason_4521` with exactly
   `credit_card_account_id`, `user_id`, `closure_reason` (one of: `annual_fee`,
   `not_using_card`, `found_better_card`, `unhappy_with_rewards`,
   `simplifying_finances`, `negative_experience`, `other`) — no extra fields.
4. **Address the stated reason** — branch on what they said:
   - `annual_fee` + customer tenure ≥ 2 years → offer a 1-year fee waiver via
     `apply_credit_card_account_flag_6147` (`flag_type="annual_fee_waived"`,
     `expiration_date` = one year from today, `reason="loyalty_benefit"`).
   - `annual_fee` + tenure < 2 years → offer a permanent downgrade to a no-fee card
     instead, preserving account history.
   - `not_using_card` → remind them of unused benefits; suggest a recurring
     subscription to keep the card active.
   - `found_better_card` → ask what features attracted them; if Rho-Bank has a
     comparable/better card, offer to help them apply for *that* instead of closing.
   - `unhappy_with_rewards` → check bonus-category enrollment; suggest maximizing
     what they already have.
   - `negative_experience` → apologize, gather details, escalate to a supervisor if
     warranted, consider a modest goodwill credit.
   - anything else → acknowledge, move to step 5.
5. **One retention offer**, tiered by card, only if the customer still wants to
   close after step 4 addressed their concern: entry-tier → 500 points or $5
   statement credit; mid-tier → 2,000 points or $20 statement credit; premium+ →
   5,000 points or $50 statement credit. Offer it **once** — do not repeat or
   escalate if declined.
6. **Resolve**: if the customer accepts the retention offer, apply it and keep the
   account **open** (do not also close it). If they still want to close (or step 2
   routed here directly), proceed with the actual closure call, thank them, no
   pressure. Mention the 45-day reward-redemption window and, if the annual fee
   posted recently, the 37-day fee-refund window.

**Skillset: credit-limit-increase (CLI) decision**
*Triggers on*: a customer requesting a higher credit limit.
*Sources*: `doc_credit_cards_credit_card_account_logistics_005` (CLI Eligibility by
Tier), `_007` (Processing CLI Approvals and Denials) — that second document states its
own steps "MUST be followed in the exact order listed."

0. **Before submitting anything**: check the requested increase amount is within the
   tier's maximum. If it isn't, tell the customer the max and ask them to adjust —
   do not submit an over-limit request at all.
1. **Submit first, decide second**: call `submit_credit_limit_increase_request_7392`
   to create the formal record — this happens *before* eligibility is checked, not
   after (eligibility checks are internal, never exposed to the customer, so there's
   no reason to defer submission).
2. **Check ALL of these before deciding** (checking only until you find one failure
   and stopping early breaks the audit record — check every item regardless):
   - Account age meets the tier minimum.
   - Cooldown: call `get_credit_limit_increase_history_4829` — an *approved* request
     within the tier's cooldown window blocks a new one; a prior *denial* does not
     trigger a cooldown.
   - No pending disputes: `get_user_dispute_history_7291` (unlock+call) — don't
     assume none exist just because none were mentioned in conversation.
   - No pending replacement card: `get_pending_replacement_orders_5765`
     (unlock+call) — same "don't assume" rule.
   - Account in good standing, no past-due balance.
   - Current utilization below the tier's threshold.
3. **Decide**: all pass + amount within limits → `approve_credit_limit_increase_5847`.
   Any single failure → `deny_credit_limit_increase_5848` with the specific
   `denial_reason` enum that matches (`insufficient_account_age`,
   `cooldown_period_active`, `pending_disputes`, `pending_replacement_card`,
   `past_due_balance`, `high_utilization`, `insufficient_payment_history`,
   `requested_amount_exceeds_limit`, `other`) — do not approve because the request
   seems reasonable or the customer asked nicely; every one of these is a hard gate.
4. Tell the customer the decision — for denials, state the specific reason and when
   they may become eligible.

**Skillset: lost wallet / multi-card replacement**
*Triggers on*: a customer reporting a lost or stolen wallet, purse, or "all my cards."
*Sources*: `doc_bank_accounts_bank_accounts_(general)_026` (Freezing/Unfreezing),
`_025` (Closing/Cancelling a Debit Card), `_029` (Replacement Options by Tier),
`_030` (Lost/Stolen Card - Cross-Product Security Protocol).

**Part A — run this per debit card the customer reports as lost/stolen (repeat
independently for each one; do not stop after the first):**
1. **Freeze it first, always** — even if the customer already says they want it
   closed outright. Per doc `_026`, freezing is the mandatory immediate action
   regardless of the eventual outcome; never skip straight to closing. Requires:
   customer verified, confirmed owner, card currently ACTIVE. Call
   `freeze_debit_card_3892(card_id)`.
2. **Then decide closure**, per doc `_025`: check eligibility (owner verified;
   card ACTIVE/PENDING; no pending transactions; no pending refunds; card ≥14
   days old — **this age requirement is waived when the reason is `lost`,
   `stolen`, or `fraud_suspected`**, which this scenario always is). If eligible,
   ask the closure reason (`lost`/`stolen`/`fraud_suspected`/etc.) and call
   `close_debit_card_4721(card_id, reason)`.
   - **Known trap, confirmed in a real failed run**: `close_debit_card_4721`
     only accepts a card in ACTIVE or PENDING status. If you froze it in step 1
     (it's now FROZEN), you must call `unfreeze_debit_card_3893(card_id)` first
     to bring it back to ACTIVE — closing a still-frozen card errors.
   - If `fraud_suspected`, pull `get_bank_account_transactions_9173` before
     deciding what (if anything) to dispute.
3. **Ask whether to order a replacement.** If yes, call `order_debit_card_5739`
   using the **account's own tier** rules from doc `_029` — do not guess a
   `delivery_fee`/`design_fee`: each tier has its own shipping-fee table
   (entry/mid/premium/elite) and design-fee table (CLASSIC/PREMIUM/CUSTOM), and
   entry-tier additionally has a 48-hour post-closure waiting period before a
   replacement can be ordered at all. Check the customer's replacement count in
   the past 12 months (cards with `issue_reason` in lost/stolen/fraud/damaged)
   against the tier's cap before ordering — new_account/first_card/expired/
   upgrade/bank_reissue cards don't count toward that cap.

**Part B — cross-product security check (mandatory every time; this step is easy to skip because the
customer only ever asked about the debit card):**
4. After Part A is done for every debit card, call
   `get_credit_card_accounts_by_user` — **unconditionally**, whether or not the
   customer mentioned owning a credit card. This session has not covered this
   skillset until this call has happened, even if every debit-card step above
   was handled perfectly.
5. If the customer has one or more credit cards: proactively ask whether that
   card was also in the lost wallet, and offer to order a replacement with a new
   card number as a security precaution — don't wait for them to raise it.
6. If they decline the credit-card replacement offer, note in the account that
   the offer was made rather than dropping it silently.

**Skillset: debit-card dispute filing + precheck**
*Triggers on*: a customer disputing a debit-card or ATM transaction.
*Sources*: `doc_bank_accounts_bank_accounts_(general)_031` (Filing a Debit Card
Transaction Dispute), `_032` (Debit Card Provisional Credit Guidelines), `_035`
(Checking Debit Card Dispute Status).

1. **Disclose liability up front**, before filing: reported within 2 business days
   of the *statement* → max liability $50; within 60 days of statement → $500; after
   60 days → unlimited. (The statement date, not the transaction date, is the anchor
   — a transaction inside the current, not-yet-issued statement cycle is always
   within the shortest window.)
2. **Pre-filing gate** — all must hold: customer verified; transaction ≥ $1.00 and
   ≤60 days old; open-dispute count for this checking account under the tier cap
   (entry 2 / mid 3 / premium 4 / elite 5 — **per account, not per customer**); card
   linked to an OPEN checking account.
3. **Per transaction, not once per customer**, determine:
   - `dispute_category`: if fraud is suspected, `card_present_fraud` (physical) or
     `card_not_present_fraud` (online/phone) — use plain `unauthorized_transaction`
     ONLY when fraud is NOT suspected (e.g. a family member used the card).
   - `pin_compromised`: `yes_shared` (customer wrote it down / told someone —
     mechanical classification, not a fault judgment), `yes_observed` (believes it
     was watched/skimmed), `no`, or `unknown`. This and `card_in_possession` describe
     *that specific transaction's* circumstances, not a blanket customer-level state
     — a card lost on day 12 doesn't retroactively make day-10's transaction one
     where the card was "not in possession."
   - `card_action` from the category mapping: `card_present_fraud`/
     `card_not_present_fraud` → `close_and_reissue`; `unauthorized_transaction` →
     `freeze_pending_investigation`; everything else → `keep_active`. Record each
     dispute's own mapped value even in a multi-dispute session — then, when you
     actually perform the card action, use the **single most severe** one across all
     disputes on that card (`close_and_reissue` > `freeze_pending_investigation` >
     `keep_active`), called once.
   - `provisional_credit_eligible` per doc `_032`: **required** (not discretionary)
     only when ALL of — reported within 60 days of statement; category is
     `unauthorized_transaction`/`card_present_fraud`/`card_not_present_fraud`/
     `atm_cash_discrepancy`/`duplicate_charge`; customer provided a written
     statement; account open with no holds. **Voluntary PIN sharing
     (`pin_compromised = 'yes_shared'`) removes eligibility even if every other
     condition is met**.
4. File with `file_debit_card_transaction_dispute_6281`, then actually perform the
   card action determined in step 3 (filing records the *intent*; it doesn't
   perform the freeze/close itself).
5. **Three narrower rules confirmed missing from an earlier pass of this entry —
   easy to drop even when everything above is followed correctly:**
   - If the same charge appears as multiple duplicates, dispute the **earliest**
     (first) occurrence, not the most recent or all of them.
   - For a fraud dispute over $500, ask whether the customer has filed a police
     report; if not, recommend they do so (this is a recommendation, not a
     precondition for filing).
   - For ATM disputes specifically, first determine whether it was a **Rho-Bank**
     ATM or a **third-party** ATM — the two follow different processes; don't
     default to one without checking which applies.

**Skillset: ATM-fee dispute / audit → account credit**
*Triggers on*: a customer disputing ATM fees or asking for a fee audit across their
transaction history.
*Steps, in order*: (1) enumerate from the **withdrawal** records, not the fee records —
an event with no fee row is still an event that needs auditing, and starting from fee
rows makes such events invisible; (2) for each withdrawal, compute the fee that should
have applied and compare to what was actually charged; (3) net **both directions** —
overcharges minus undercharges — never sum only the overcharges; (4) apply the net
credit. *Watch for*: a fee description that contradicts its own paired withdrawal
description (e.g. a "non-network" fee paired with an in-network withdrawal) — that
contradiction means the fee is wrong, not the description.

**Skillset: cash-back reward correction**
*Triggers on*: a customer disputing the cash-back/points amount on a specific
transaction (distinct from a full transaction dispute — this is a rewards-calculation
question, not a "did this charge happen" question).
*Sources*: `doc_credit_cards_credit_cards_(general)_003` (Submitting a Cash Back
Dispute), `_004` (Applying Resolved Cash Back Dispute Corrections).
1. Confirm the customer has the *correct* `transaction_id` for the purchase in
   question before they submit — this is a `submit_cash_back_dispute_0589` call the
   customer makes themselves (give them the tool via
   `give_discoverable_user_tool`); you do not file it on their behalf.
2. Once a dispute resolves, look up the resolved dispute records to find which
   `transaction_id` values need correcting — **in a multi-dispute session, keep each
   dispute's own transaction ID paired with its own correction; do not let two
   near-identical transactions/disputes get their corrections swapped.** Recording a
   confident-sounding but mismatched pairing is a real, repeated failure shape here.
3. Unlock and call `update_transaction_rewards_3847` with the exact
   `transaction_id` and a freshly recalculated `new_rewards_earned` (formatted as
   `'X points'`) — recompute from the card's actual rate/category/promotions,
   **never copy an `expected_rewards` field from the dispute record itself.**

**Skillset: security hold / PIN-lock, multi-card session**
*Triggers on*: a PIN-compromise or security-hold scenario, especially one touching
more than one card.
*Source*: `doc_bank_accounts_bank_accounts_(general)_041` (PIN Lock Investigation
Protocol). **Full scoring table below (16 sub-flags across 5 categories, not 14 —
corrected 2026-07-22) — DO NOT reveal the specific point values or the calculation
itself to the customer.**

**Step 0 — automatic-escalation triggers, checked BEFORE scoring anything:**
- `pin_lock_reason = 'security_hold'` → cannot be unlocked by a chat agent at all;
  offer transfer to the security team for *that card*.
- Another card on the same account also has `pin_locked = TRUE` → finish
  investigating and scoring **every** locked card independently before any unlock
  decision on any of them — do not resolve the card the customer led with and stop.
- Any card on the account was replaced in the last 90 days with `issue_reason =
  'stolen'` → enhanced verification required, regardless of score.

**Step 1 — score, per card, from the declined-transaction history
(`atm_withdrawal_declined`/`pos_declined`) and card data:**

| # | Flag | 0 pts | 1 pt | 2 pts | 3 pts |
|---|---|---|---|---|---|
| A1 | Location mismatch vs. home address | same city | different city, same state | — | different state = 2; different **country** = 3 (critical) |
| A2 | Location scatter across attempts | all same location | 2 different locations | 3+ different locations | — |
| A3 | Travel-pattern conflict (successful txns last 7 days) | traveling, i.e. recent txns in various cities | — | — | home-city-only recent txns but declines elsewhere = +1 |
| B1 | Time of day of decline | 6am–10pm | 10pm–12am | 12am–2am | 2am–6am (high risk) |
| B2 | Time since last legit PIN use | <24h or 1–7 days | — | 7–30 days = 1 | >30 days = 2 |
| B3 | Attempt velocity (gap between fails) | >5 min apart | 2–5 min | 1–2 min | <1 min (scripted attack) |
| C1 | Amount pattern across attempts | consistent or increasing | — | decreasing (e.g. 800→500→300, fraud pattern) | — |
| C2 | Round-number testing | mixed amounts | all round hundreds | — | — |
| C3 | Amount vs. customer's historical ATM average | within 2x | 2–5x | >5x | — |
| C4 | Amount vs. card's daily ATM limit | <50% | 50–80% | 80–100% = 1; multiple attempts totaling over limit = 2 | — |
| D1 | Prior PIN locks, same card, past 90 days | 0 | 1 | 2 | 3+ = **mandatory PIN reset, cannot unlock under any score** |
| D2 | Card age | >1yr / 6–12mo / 3–6mo | 1–3mo = 1 | <1mo = 2 | — |
| D3 | Other cards on the account with security flags | none | — | velocity block on another card = +1; active fraud alert on another card = +2 | — |
| E1 | Account age | >2yr / 1–2yr / 6–12mo | 3–6mo = 1 | <3mo = 2 | — |
| E2 | Overdrafts in recent history | none | 1 = 1 | 2+ = 2 | — |
| E3 | Current account balance | >$500 | $100–500 | $50–100 = 1 | <$50 = 2 (possibly abandoned/compromised) |

**Any single flag scoring 3 → escalate to supervisor review regardless of the total.**

**Step 2 — apply the total-score threshold, per card:**

| Total | Risk | Required protocol |
|---|---|---|
| 0–4 | LOW | Unlock after standard identity verification |
| 5–7 | MEDIUM | Unlock only after asking "were those failed attempts yours?" |
| 8–10 | HIGH | Ask specific location/time questions; unlock only if the customer confirms **and** the explanation is satisfactory |
| 11–14 | VERY HIGH | Cannot unlock on this call — require callback verification or enhanced verification (last-4 SSN + security question) |
| 15+ | CRITICAL | Cannot unlock. Check for successful unauthorized transactions; recommend closure + replacement |

**Step 3 — for scores ≥5, verification questions can *remove* a flag**: if the
customer confirms they were at the flagged location / made the flagged attempts /
were awake at the flagged time, remove that specific flag and recalculate the total
— don't leave a disproved flag counted. "I was asleep" or similar to a 2+ point
time-of-day flag is itself CRITICAL, not neutral.

**Step 4 — hard override, independent of total score**: D1 = 3+ (3 or more prior
locks in 90 days) → cannot unlock under any circumstance; PIN reset is mandatory.

**Step 5 — after a successful unlock, the follow-up is itself a required step, tied
to D1**: 0 priors → nothing further; 1 prior → must proactively offer PIN-lock
notifications; 2 priors → must proactively ask if they want their PIN reset; 3+ →
covered by Step 4, no unlock happens. Stopping at "unlocked, anything else?" without
this leaves the skillset incomplete even if the score and decision were both right.

**Step 6 — if you cannot unlock** (score-blocked or Step 0/4 override): check
transaction history for any successful unauthorized transactions during the
suspicious window; if found, file a dispute, close the card, and order a
replacement; if none found, explain the concern and offer closure+replacement,
transfer to security, or a PIN reset.

**Skillset: referral advising**
*Triggers on*: a customer asking about a referral bonus, referral status, or "why
haven't I gotten my bonus."
*Correction (2026-07-22): the original version of this entry cited only the
checking-account referral document. There are TWO separate, parallel referral
programs in this domain with DIFFERENT numbers — using one program's limits for the other is a way to get this wrong.*

**Step 0 — determine which program applies, before looking anything else up**: is
the referral for a **credit card** or for a **checking/savings account**? The rolling
window, cap, and per-product bonus amount are different between the two and are not
interchangeable.

**Branch A — credit card referral.**
*Sources*: `doc_credit_cards_credit_cards_(general)_001` (Referral Statuses), `_002`
(Referral Offers and Restrictions), `_009` (Generating a Referral Link), plus the
specific card's own referral doc (e.g. `doc_credit_cards_silver_rewards_card_011`)
for that card's bonus amount and annual cap.
1. Verify identity, then look up the customer's referral history before advising —
   do not answer from general KB knowledge about "how referrals work" without
   checking the customer's actual status first.
2. Cap: at most 2 referral bonuses in any rolling **7-day** window, across ALL
   credit-card types combined (not per-card) — the 3rd and later in that window are
   auto-denied and cannot be reinstated within the same window.
3. Each card product has its **own** bonus amount and annual cap (e.g. Silver
   Rewards Card: 75 per referral, up to 7/year) — look up the specific card's own
   document, don't assume figures are the same across cards.
4. Before generating a referral link: confirm the card actually has a documented
   referral program, and that any terms the customer cites match the documented
   program — if not (or if the referral is going to be auto-rejected anyway per the
   caps above), explain why and do **not** hand over the `get_referral_link` tool.
   The customer runs `get_referral_link(user_id, card_name)` themselves — never
   generate it on their behalf.
5. Status meanings (`COMPLETE`/`IN_PROGRESS`/`NO_PROGRESS`/`APPLIED`/`REJECTED`/
   `ERROR`) each have their own correct next action — see source `_001`; in
   particular `IN_PROGRESS` means no action is required except monitoring, it does
   **not** mean anything is currently blocking the customer that needs to be pushed
   forward.

**Branch B — checking/savings account referral.**
*Source*: `doc_bank_accounts_bank_accounts_(general)_047` — this document's own
text: "before giving any recommendations or information on referral terms, you must
check that the user is eligible to submit referrals first."
1. Verify identity; look up the customer's accounts; look up their referral
   history — this is the document's own explicit precondition, not optional.
2. Cap: rolling **9-day** window, 2 referrals, across all checking/savings account
   types combined (a different window length than credit cards — do not reuse the
   7-day figure from Branch A here).
3. Tenure is measured from the customer's *earliest* checking account, regardless
   of which product they currently hold.
4. 90-day clawback if the referred account closes early; qualifying deposit must be
   new money, held ≥30 days, not combinable with other new-account promotions.

**Skillset: account open/close sequencing**
*Triggers on*: a customer asking to open one account and close another (or several) in
the same conversation.
*Source*: this skillset is the same rule as base policy guideline #8 — restated here
so it's indexed alongside the other named skillsets, not a second, different rule.
*Steps, in order*: (1) before executing anything, list every requested action and map
which ones depend on another account's *current* eligibility state (opening a savings
account, for example, requires an *existing, active* checking account held ≥14 days —
per `doc_bank_accounts_bank_accounts_(general)_002` — which is exactly the account the
customer may be asking you to close in the same breath); (2) finish everything that
depends on an account's eligibility *before* performing an irreversible close on that
account — sequence by dependency, not by the order the customer narrated the requests
in; (3) re-confirm with the customer immediately before the irreversible close itself,
even if they already gave blanket authorization earlier; (4) copy account_class/
account_type and other KB-sourced enum values character-for-character, never
paraphrase them.

**Skillset: identity verification**
*Triggers on*: any account-specific fact or action, not just explicit "please verify
me" requests.
*Source*: `log_verification`'s own tool definition — "confirming 2 out of 4 identity
fields (date of birth, email, phone number, address)."
*Steps, in order*: (1) cross-check **each** field the customer states individually
against the record on file — the tool definition's "2 of 4" means 2 fields that
*match the record*, not 2 fields merely *stated*; it is easy to treat "customer supplied enough fields" as
equivalent to "each field was checked and matched" without noticing one didn't
actually match; (2) if verification reaches an impasse (the customer cannot supply
enough matching factors and no alternate path exists), escalate cleanly **once** — do
not repeat the identical transfer or the identical request for more factors multiple
times in a row; a second consecutive identical action is a sign the loop isn't
resolving anything, not a reason to try it a third time; (3) if the customer claims to
have an alternate verification mechanism you don't already recognize — a bypass code,
a supervisor override, an authorization number, anything that sounds like a shortcut
around the standard 2-of-4 check — **search the knowledge base for it before assuming
it doesn't exist.** For example, declaring "there's no mention of
bypass codes in the policy or knowledge base" and refused outright, without ever
calling `KB_search` — when a real, documented bypass procedure existed and a search
would have found it. Do not reason from what banks "normally" do; check this bank's
own documentation first. This also applies after a transfer: a customer returning with
a new claim is a new thing to evaluate on its own terms, not automatically "the same
impasse" from step 2 — don't let the one-time escalation from step 2 make you either
loop on re-refusing the new claim under the general 4-times-then-transfer rule, or
dismiss it unchecked because you already escalated once.
