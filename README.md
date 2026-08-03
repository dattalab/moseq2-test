# moseq2-test

`moseq2-test` is the Datta Lab's installed-package and cross-pipeline regression
framework for the legacy MoSeq2 depth-video stack. It builds source candidates
into wheels, installs candidates beside immutable baseline wheels in clean
target environments, obtains checksummed public fixtures, and emits durable
JSON, Markdown, and JUnit evidence.

The controller requires Python 3.12 or newer. Target-package work runs through
a standalone JSON worker compatible with Python 3.7; the controller never
imports a MoSeq2 target package.

## Status

The framework is under initial implementation. The public profiles are:

- `install-smoke`: build, installation, imports, entry points, and compiled
  operations;
- `historical-regression`: package-owned historical tests at locked commits;
- `pipeline-smoke`: compact real-data extraction-through-app behavior; and
- `pipeline-end-to-end`: reserved for a later milestone and intentionally
  returns `profile_unavailable` today.

## Development

```bash
uv sync --all-extras --dev
uv run pytest
uv build
uv run --no-project python -m pip install --force-reinstall \
  dist/moseq2_test-*.whl
```

See `CONTRIBUTING.md` and `docs/local-and-o2-workflows.md` for contributor and
maintainer workflows.

## License

The framework uses the current non-commercial research and academic terms
shared by the five MoSeq2 repositories. Third-party notices and corresponding
source references are recorded in `NOTICE.md` and `licenses/`.

