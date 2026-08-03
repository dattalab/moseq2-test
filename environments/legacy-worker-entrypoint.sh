#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${MOSEQ2_TEST_SAFE_GIT_DIRECTORY:-}" ]]; then
  case "$MOSEQ2_TEST_SAFE_GIT_DIRECTORY" in
    /*) ;;
    *) echo "safe Git directory must be absolute" >&2; exit 2 ;;
  esac
  /opt/moseq2/legacy/bin/git config --global --add safe.directory \
    "$MOSEQ2_TEST_SAFE_GIT_DIRECTORY"
fi

action_root=${MOSEQ2_TEST_ACTION_ROOT:-/opt/moseq2/framework-source}
if [[ ! -f "$action_root/pyproject.toml" ]] || [[ ! -f "$action_root/uv.lock" ]]; then
  echo "moseq2-test action source is incomplete: $action_root" >&2
  exit 2
fi

distribution_root=$(mktemp -d /tmp/moseq2-test-action-wheel.XXXXXX)
trap 'rm -rf "$distribution_root"' EXIT
uv build --offline --wheel --project "$action_root" --out-dir "$distribution_root"
wheel=$(find "$distribution_root" -maxdepth 1 -type f -name 'moseq2_test-*.whl' -print -quit)
if [[ -z "$wheel" ]]; then
  echo "moseq2-test action checkout produced no wheel" >&2
  exit 2
fi
uv pip install --offline --reinstall --no-deps \
  --python /opt/moseq2/controller/bin/python "$wheel"
exec /opt/moseq2/controller/bin/moseq2-test "$@"
