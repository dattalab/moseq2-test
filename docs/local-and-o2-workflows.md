# Local, CI, and O2 workflows

## Local contributor loop

Run framework unit tests with Python 3.12, then test the changed package as an
installed wheel against a locked Python 3.7 target. Use node-local or `/tmp`
workspace and output roots; keep durable reports outside disposable scratch if
they are needed for review.

For ordinary package work, run `install-smoke`, that package's
`historical-regression`, and the smallest affected `pipeline-smoke` slice.
Before a release or baseline change, run the complete baseline matrix. The
README contains exact source, wheel, multi-repository, data, and report commands.

## GitHub pull requests

Caller workflows run on `ubuntu-24.04`, use `contents: read`, do not receive
secrets, pin checkout and `dattalab/moseq2-test` by full SHA, and upload reports
even on failure. The Action performs fixture retrieval before removing network
access. Never use `pull_request_target` to test a candidate and never point a
public pull request at a self-hosted runner.

## O2 maintainer runs

O2 is a manual trusted execution location, not a public-code CI backend. Review
and check out the exact central and candidate commits first. Use the same CLI,
locks, manifests, candidate wheels, cache verification, and report format as
hosted CI. Prefer a verified read-only mirror with `data fetch --mirror`; prove
its objects match public SHA-256 values, then use `--offline` for the run.

Choose a scratch root with sufficient free space and keep cache, candidate,
workspace, and results separate. `moseq2-test doctor --ci` applies the hosted
disk preflight. No profile silently submits SLURM work: compact pipeline tests
use local Dask, and any allocation/submission is an explicit operator decision
outside `moseq2-test`. Copy the final run/certification directory to durable
project storage before scratch cleanup.

## Reconstruct the accepted baseline

1. Check out the reviewed central commit and read the exact OCI digest from
   `environments/legacy-worker.lock.yml`.
2. Pull that digest anonymously and verify the pulled `RepoDigest` is exact.
3. Fetch and safely expand `historical-v1` and `pipeline-smoke-v1`; verify the
   cache, then make it read-only.
4. Run `baseline certify` with the locked target Python, both fixture sets, a
   fresh workspace, and no candidate overrides.
5. Repeat from a fresh workspace with `--offline` and network disabled.
6. Compare the canonical semantics of the three profile run directories and
   retain certification JSON, summary, JUnit, logs, metrics, and comparisons.

An accepted certification has all 25 requirements passed and preserves the
47-check install, 264-outcome history, and 28-step compact-pipeline contracts.
