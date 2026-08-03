# Architecture

## Trust and process boundaries

The Python 3.12+ controller parses manifests, prepares sandboxes, compares
artifacts, applies failure policy, and writes reports. It does not import MoSeq2.
Target operations run in a Python-3.7-compatible worker via versioned JSON
request and response files. GitHub CI runs that worker in the public OCI image
whose exact digest is checked into `environments/legacy-worker.lock.yml`.

The Action has three trust phases:

1. trusted preparation may anonymously fetch and checksum fixtures;
2. the candidate source is exported and built into a wheel without network;
3. suites run without network, with source, framework, cache, and candidate
   inputs mounted read-only and only isolated workspace/results roots writable.

Public-fork code runs only on disposable GitHub-hosted runners with read-only
repository permission and no secrets. It never runs through
`pull_request_target` or an O2/self-hosted runner.

## Contracts

`manifests/sources` and `manifests/wheels` identify the eight baseline commits
and wheel bytes. Fixture manifests map logical IDs to public content-addressed
objects, provenance, terms, trust, and extraction limits. Profiles define
packages, fixture sets, resource ceilings, ordered steps, expected artifacts,
and comparison policies. JSON schemas make these files public versioned APIs.

A candidate set replaces one or more locked wheels by canonical package name.
Every other package remains the exact locked baseline. Source candidates are
exported from Git, built in clean directories, installed as wheels, and paired
with an isolated test snapshot. Editable links, escaped `.pth` entries, source
imports, dirty source, hash mismatches, and duplicate candidates fail closed.

The historical `pyhsmm` and `pyhsmm-autoregressive` setup scripts otherwise
download Eigen during a build. For those two candidates only, the controller
verifies the independently locked Eigen 3.3.7 archive and safely stages its
headers into the disposable Git export before invoking the package's unchanged
build. The source checkout stays read-only, network access stays disabled, and
the external input identity is retained in the candidate build log.

Compiled candidates use the separately hash-locked GCC/G++ 11.4 prefix. The
controller selects its explicit compiler wrappers only during source builds and
records that selection in the build log. The frozen Python 3.7 runtime remains
a distinct prefix, so adding build capability does not re-solve or mutate the
baseline oracle.

## Data and sandboxes

The fixture cache is content addressed by SHA-256. Downloads are locked and
atomic; size and hash are checked before a cache hit is usable. ZIP/TAR
extraction rejects traversal, links, special files, and declared size/member
ceilings. Expanded inputs are read-only. Each build, installation, historical
test snapshot, pipeline run, and result directory has a distinct root.

Two sealed `moseq2-extract` tests exercise its historical classifier-download
entry points. Before that suite starts, the controller maps only the two exact
legacy HTTPS URLs to their separately declared and verified cache objects. A
Python-3.7-compatible `sitecustomize` adapter copies the exact bytes with the
normal `urlretrieve` interface; it rejects any undeclared URL. The mapping,
object IDs, sizes, hashes, and adapter hash are retained in run provenance.
This preserves the package-owned test content while keeping target execution
network-disabled.

## Reports

`run.json` is canonical. Markdown and JUnit are deterministic projections and
may be regenerated. Records include candidate/source identities, locks,
fixtures, commands, return codes, classifications, comparisons, environment,
known-failure matches, retained outputs, and provenance. Partial records are
written for setup, infrastructure, test, and comparison failures.

Cross-run comparison retains outcome classifications, structured semantic
differences, package identities, locks, fixtures, and stable environment facts.
It excludes execution timings, disposable sandbox paths, container hostnames,
and raw artifact hashes after the typed artifact comparator has already judged
their normalized semantics. This permits independent cold and offline-warm
runs to compare equal without concealing a result or scientific-output change.

## Central and caller repositories

The central repository owns all test machinery. Each DattaLab package caller
contains only a GitHub workflow pinned to a full central commit. This keeps
framework fixes reviewable in one place and prevents six drifting copies of
fixture or failure logic.
