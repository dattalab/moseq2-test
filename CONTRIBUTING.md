# Contributing

Install development tools with `uv sync --all-extras --dev`, then run
`uv run pytest`, `uv run ruff check .`, and `uv build`. Never test a candidate
through an editable installation: use `moseq2-test candidates build` or pass a
wheel explicitly.

Package-owned unit and integration tests remain in their package repositories.
This repository owns orchestration, locks, shared fixtures, semantic policies,
cross-package profiles, known baseline failures, and reporting.

Do not add a newly discovered failure to `known-failures.yml` merely to obtain
a green result. Existing deterministic failures require an exact signature and
an unexpected pass is itself reviewable. Approved behavior changes use a
separate intentional-change manifest.

All pull requests should identify affected schema/profile versions, include a
negative test, and retain before/after certification evidence when the changed
layer is exercised by a baseline profile.

