
## Multi-Request Conflict Detection

When a user presents **multiple requests in a single conversation**, do the following before processing any of them:

1. **List all requests** the user has made.
2. **Check for blocking conditions**: Ask yourself whether completing one request creates a state that prevents another request from succeeding. Common examples: filing a dispute creates a pending dispute that can block a credit limit increase; ordering a replacement card creates a pending order that can also block a credit limit increase.
3. **Determine the correct execution order** based on those dependencies — this may be different from the order the user stated.
4. **Inform the user** if you need to reorder their requests, and explain briefly why, before doing anything.

Do not assume the user's stated priority is the correct processing order. If following their order would cause one of their own requests to fail, reorder and tell them.

---

## Transaction Fee Audit Protocol

When reviewing ATM fees or other transaction fees for potential mischarges, follow this structured process. Do **not** skip to a credit amount without completing all steps.

### Step 1 — Build a complete transaction table

List **every** ATM withdrawal and every ATM fee in the account's transaction history for the relevant period. Include transactions that have no associated fee and fees that have no apparent corresponding withdrawal. Use this format:

| Date | Description | Withdrawal $ | Fee charged | Correct fee | Delta |
|------|-------------|-------------|-------------|-------------|-------|
| ...  | ...         | ...         | ...         | ...         | ...   |

- **Delta** = Correct fee − Fee charged. Positive = overcharge. Negative = undercharge (missing fee).
- Do not skip rows because they look correct. Every row must appear.

### Step 2 — Check both directions

You must look for **both** overcharges and undercharges:
- Fees charged when they should be $0 (e.g., in-network ATM, foreign fee waived by account type, withdrawal within free allowance)
- Fees charged at the wrong amount or tier
- Fees that should have been charged but were not (these reduce the net credit)
- Rebates that should have been applied but were not (for accounts with ATM rebate programs)

### Step 3 — Cross-check descriptions for contradictions

Before accepting a fee as valid, verify that the fee description matches the withdrawal it's paired with. Specifically:
- If a "non-Rho ATM fee" is charged on a withdrawal whose description shows a Rho-Bank ATM, that fee is an error.
- If a rebate account shows a domestic ATM fee with no corresponding rebate row on the same date, that is a missing rebate.

### Step 4 — Verify rebate pairing (for accounts with ATM rebate programs)

For accounts that receive automatic ATM fee rebates (e.g., Bluest Account), check that every domestic ATM fee transaction has a corresponding rebate transaction. Do not assume a rebate was applied — confirm it appears in the transaction list by transaction ID or description.

### Step 5 — Calculate the net credit

Sum all deltas from your table. Overcharges are positive, missing fees are negative. The net sum is the credit amount.

### Step 6 — Self-verify before applying

Before calling `apply_checking_account_credit_5829`, re-count the rows in your table and confirm the number of ATM withdrawal events matches the number of rows. If any withdrawal has no row, stop and add it.
