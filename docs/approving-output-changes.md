# Reviewing failures and approving output changes

## Triage order

Start with `summary.md`, then use `run.json` to identify `failure_stage`, the
candidate/worker/fixture identities, failed commands, semantic comparisons, and
known-failure results. Decide whether the observation is infrastructure,
candidate packaging, an existing exact historical failure, an unexpected pass,
an unknown regression, or an intentionally requested behavior change.

Do not repair or waive a scientific difference as part of packaging-only work.
Retain the evidence and open a separately scoped review.

## Known failures

A record is acceptable only if package or step, baseline commits, outcome,
exception, anchored message signature, policy, owner, review date, and evidence
all match. `required_failure` must still occur; `allowed_failure` may pass only
for its documented environmental reason. A new message, exception, location,
or expired record is unknown and fails.

## Intentional changes

An approved intentional-change record contains a unique ID, old and new
expectations, affected artifact kinds, issue/PR, rationale, and regression test.
Review is two-stage:

1. reproduce and explain the difference without changing the golden;
2. approve the behavior and regression test, then update the intentional-change
   record/golden in a distinct reviewed commit or PR.

Run `compare artifact` with the named policy and intentional-change file, then
rerun the affected profile and full baseline certification. `expected-change`
is accepted only for the declared artifact kinds; unrelated differences remain
failures. Retain both old and new comparison JSON and candidate identities.
