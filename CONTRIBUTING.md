# Contributing

This repository owns orchestration, environment and source locks, shared
fixtures, semantic comparison policies, cross-package profiles, exact known
failures, and durable reporting. Package-owned scientific/unit tests remain in
their package repositories. A caller-repository integration PR should normally
contain one thin workflow and no runtime or packaging change.

## Before opening a pull request

```bash
uv sync --all-extras --dev
uv run pytest
uv run ruff check .
uv run mypy src
uv build
```

Do not validate candidates through editable installs. Use `candidates build`,
`--source`, or `--candidate`, and confirm imports and distributions resolve only
inside the target environment. Add a negative test for every new validation or
security boundary.

## Ownership and review

Package repositories own their tests and intended package behavior.
`moseq2-test` maintainers own profile selection, fixtures, locks, shared
comparators, failure policy, reports, the worker image, and CI integration.
Changes to a schema, lock, fixture manifest, known-failure record, comparator,
worker image, or baseline output require maintainer review. See
`MAINTAINERS.md` for the named owner and release rules.

## Adding functionality

Follow `docs/adding-a-package-or-suite.md`. Public names and schemas are
versioned contracts. Update the declarative profile first, keep any adapter
small, validate both success and failure paths, and show that unchanged
baseline certification is still accepted.

## Failure policy

Do not add a newly observed failure to `manifests/known-failures.yml` merely to
make CI green. A known failure must identify the exact package/test or pipeline
step, source commits, expected outcome, exception and anchored message
signature, policy, owner, review date, and evidence.

`required_failure` means an unexpected pass is a review-blocking change.
`allowed_failure` is reserved for a documented environment-dependent outcome;
a pass is allowed but a different failure is not. Expired records fail closed.

An intentional behavior change is separate from known-failure policy. It needs
a reviewed intentional-change record and a regression test as described in
`docs/approving-output-changes.md`. Never update a golden in the same step that
first discovers a difference.

## Pull-request evidence

State the affected profile/schema versions, candidate wheel identity, fixture
set and worker digest, positive and negative test results, before/after
certification, runtime/download/peak-disk observations, and whether an output
change was expected. Keep scientific correctness and package modernization out
of framework/integration-only PRs unless that separate scope was explicitly
approved.
