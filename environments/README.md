# Legacy worker environment

The target runtime is reconstructed from the checked-in Conda, pip, external
source, and baseline-wheel locks. The standalone worker remains compatible
with Python 3.7 and communicates with the modern controller through protocol
version 1 JSON files.

The public image is built only by the trusted tagged workflow and consumed by
immutable digest. Process execution against an independently reconstructed
environment remains the reproducible fallback.

The worker supply chain is split into independently reviewable locks:

- `legacy-conda-linux-64.lock.yml` preserves the 55-package Chunk 0 Conda
  export with SHA-256 and MD5 for each exact artifact.
- `legacy-container-runtime-linux-64.lock.yml` adds 13 container-only X11/GLib
  libraries that the O2 process environment obtained from its host plus one
  Git client used to export candidate worktrees. It does not replace any
  captured Conda package.
- `legacy-build-toolchain-linux-64.lock.yml` adds an independently resolved,
  20-package GCC/G++ 11.4 prefix used only for offline candidate-wheel builds,
  including the `crypt.h` compatibility header required by Python 3.7.
  Its compiler path and sysroot are selected explicitly, so historical Python
  linker flags cannot leak host libraries into candidate wheels; it never
  replaces or solves into the frozen Python 3.7 runtime prefix.
- `legacy-pip-py37-linux-x86-64.lock.yml` records all 125 non-MoSeq pip
  distributions. Nine releases available only as source archives are retained
  as hash-locked wheels together with their corresponding-source identities.
- `legacy-test-tools.lock.yml`, `external-sources.lock.yml`, and the baseline
  wheel lock identify the historical runner, Eigen, Git corresponding source,
  and eight target packages.
- `legacy-worker-base-images.lock.yml` pins all three build stages by OCI index
  and linux/amd64 manifest digest.
- `legacy-worker-inputs.lock.yml` is the notice/provenance and public-object
  inventory consumed by the image build.

An anonymous external builder prepares the network-free Docker context with:

```bash
uv run python environments/prepare_legacy_worker_context.py \
  --output build/worker-inputs
```

`publish-legacy-worker.yml` builds the image from that verified context, runs
an actual Python 3.7 C++ extension build/import through the locked toolchain,
the installed-package baseline certification, generates an SPDX SBOM, retains
a Grype scan, publishes only from its trusted release environment, and creates
GitHub/registry provenance. The resulting digest is recorded separately after
the first successful public build; no caller is permitted to use a mutable
image tag.
