# moseq2-test

`moseq2-test` is the Datta Lab's installed-package and cross-pipeline regression
framework for the legacy MoSeq2 depth-video stack. It turns a clean source
checkout into a wheel, installs that wheel beside exact baseline wheels, runs
package and compact real-data tests, and retains machine-readable evidence.

The controller requires Python 3.12 or newer. MoSeq2 code executes only through
a standalone Python-3.7-compatible worker; the controller never imports a target
package. Hosted CI uses the public worker image at the immutable digest recorded
in `environments/legacy-worker.lock.yml`.

## What the profiles mean

| Profile | Public purpose | Current contract |
|---|---|---|
| `install-smoke` | Prove a complete locked stack plus candidate is genuinely installed | 47 build, import, distribution, CLI, and compiled-operation checks |
| `historical-regression` | Run package-owned tests at the eight sealed source commits | 264 outcomes with exact known-failure policy |
| `pipeline-smoke` | Exercise a compact real-data path from extraction through app behavior | 28 steps, semantic artifact comparisons, and named legacy failures |
| `pipeline-end-to-end` | Future full recording-to-results acceptance test | Defined but deliberately unavailable in this milestone |

The old names “Gate A–D” appear only in migration history. The profile names
above are the supported contributor interface.

## Install the controller

For repository development:

```bash
git clone https://github.com/dattalab/moseq2-test.git
cd moseq2-test
uv sync --all-extras --dev
uv run moseq2-test --version
```

For an already-built artifact:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install /path/to/moseq2_test-0.1.0-py3-none-any.whl
.venv/bin/moseq2-test --version
```

Direct CLI runs need a Python 3.7 target reconstructed from the checked-in
locks. Set `MOSEQ2_LEGACY_PYTHON` to its interpreter. GitHub callers normally
use the central Action, which supplies the locked worker automatically.

## Test a local source checkout

The source is exported from Git, built as a wheel in a clean build directory,
and installed into a disposable target. Clean commits are required by default.

```bash
uv run moseq2-test \
  --target-python "$MOSEQ2_LEGACY_PYTHON" \
  --workspace /tmp/moseq2-test-work \
  --output-dir runs \
  run install-smoke \
  --source moseq2-extract=/path/to/moseq2-extract
```

Run its package-owned tests and smallest downstream slice:

```bash
uv run moseq2-test --target-python "$MOSEQ2_LEGACY_PYTHON" \
  run historical-regression \
  --package moseq2-extract \
  --source moseq2-extract=/path/to/moseq2-extract

uv run moseq2-test --target-python "$MOSEQ2_LEGACY_PYTHON" \
  run pipeline-smoke \
  --source moseq2-extract=/path/to/moseq2-extract \
  --through pca
```

## Test an existing wheel

A wheel override uses the sealed test snapshot unless `--test-source` is
provided. This separates the code under test from the test checkout.

```bash
uv run moseq2-test --target-python "$MOSEQ2_LEGACY_PYTHON" \
  run install-smoke \
  --candidate moseq2-extract=/path/to/moseq2_extract-1.2.0-py3-none-any.whl

uv run moseq2-test --target-python "$MOSEQ2_LEGACY_PYTHON" \
  run historical-regression \
  --package moseq2-extract \
  --candidate moseq2-extract=/path/to/moseq2_extract-1.2.0-py3-none-any.whl \
  --test-source moseq2-extract=/path/to/moseq2-extract
```

## Test coordinated changes from multiple repositories

Repeat `--source` or `--candidate`. Unchanged packages always come from the
explicit baseline wheel lock.

```bash
uv run moseq2-test --target-python "$MOSEQ2_LEGACY_PYTHON" \
  run pipeline-smoke \
  --source moseq2-extract=/work/moseq2-extract \
  --source moseq2-pca=/work/moseq2-pca \
  --through pca
```

## Download once and run offline

Fixtures are anonymous public HTTPS objects addressed by SHA-256. Archives are
expanded by a traversal-safe extractor into a read-only cache.

```bash
uv run moseq2-test --cache-dir /path/to/cache \
  data fetch --profile historical-regression --extract
uv run moseq2-test --cache-dir /path/to/cache \
  data fetch --profile pipeline-smoke --extract
uv run moseq2-test --cache-dir /path/to/cache \
  data verify --fixture-set historical-v1 --fixture-set pipeline-smoke-v1

uv run moseq2-test --offline --cache-dir /path/to/cache \
  --target-python "$MOSEQ2_LEGACY_PYTHON" \
  run pipeline-smoke
```

On O2, add `--mirror /path/to/read-only/mirror` to `data fetch` when a trusted
local mirror is available. The resulting object bytes and run records are the
same as public retrieval.

## Use from a package repository

Pin the Action by a reviewed 40-character commit, never by `main` or a tag:

```yaml
permissions:
  contents: read

jobs:
  moseq2-test:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
        with:
          fetch-depth: 0
          persist-credentials: false
      - uses: dattalab/moseq2-test@<REVIEWED_40_CHARACTER_COMMIT>
        with:
          package: moseq2-extract
          source: ${{ github.workspace }}
          tier: pull-request
```

The Action pulls the worker anonymously by digest, downloads trusted fixtures,
then removes network access and mounts the candidate, framework, and cache
read-only while tests run. It uploads candidate identity and result records even
when a test fails.

## Read a result

Every run directory contains canonical `run.json`, its `summary.md` and
`junit.xml` projections, resolved configuration, manifests, and logs. Start
with the summary, then inspect classifications and provenance in JSON:

```bash
less runs/<RUN_ID>/summary.md
jq '{status, failure_stage, commands, comparisons}' runs/<RUN_ID>/run.json
uv run moseq2-test report runs/<RUN_ID>
uv run moseq2-test compare run \
  --expected-run runs/reference \
  --actual-run runs/candidate
```

Exit code `0` means the recorded contract was accepted. A nonzero result is not
waived by a broad xfail: only an exact, reviewed known-failure record can be
accepted.

## Documentation

- [Architecture](docs/architecture.md)
- [Local, CI, and O2 workflows](docs/local-and-o2-workflows.md)
- [Adding a package or suite](docs/adding-a-package-or-suite.md)
- [Reviewing failures and output changes](docs/approving-output-changes.md)
- [Fixture data governance](docs/data-governance.md)
- [Release procedure](docs/release-procedure.md)
- [Future end-to-end boundary](docs/pipeline-end-to-end.md)
- [Maintainers and review rules](MAINTAINERS.md)

## License and security

The framework uses the current non-commercial research and academic terms in
`LICENSE.md`. Third-party notices and corresponding source references are in
`NOTICE.md` and `licenses/`. Report vulnerabilities using GitHub's private
security-advisory interface; see `SECURITY.md`.
