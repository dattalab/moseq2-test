# Adding a package or suite

## Add or change a package

1. Confirm ownership, default branch, license/notice obligations, exact clean
   commit and tree, build inputs, import name, distribution name, console
   scripts, compiled modules, test command, fixtures, dependencies, and expected
   historical outcomes.
2. Add the source and wheel to new versioned locks. Do not mutate a released
   lock in place. Include source archive, sdist, wheel, and corresponding-source
   hashes where applicable.
3. Add declarative profile steps and package-impact selection. Add adapter code
   only when the operation cannot be represented by the profile.
4. Add positive and negative framework tests: candidate resolution, isolated
   installation, import location, dirty/editable rejection, fixture failure,
   command/JUnit parsing, and exact failure classification.
5. Run the package alone and the full baseline. Single-package outcomes must
   agree with the corresponding full-stack outcome.
6. After the central revision is accepted, add a thin caller workflow pinned by
   full SHA. Do not combine it with package code, metadata, or Travis removal.

Changes to `mattjj/pybasicbayes` or `mattjj/pyhsmm` require a separate ownership
or fork decision; Chunk 1 treats them only as exact central inputs.

## Add or change a suite

Define its public purpose, owner, participating packages, fixture sets, ordered
steps/dependencies, outputs, comparator policies, known failure behavior, and
CPU/memory/disk/timeout ceilings. Add or update the schema only when the current
declarative model is insufficient. A new profile needs:

- an inspection/listing contract and clear unavailable behavior before its
  implementation exists;
- deterministic synthetic tests for orchestration and failure paths;
- a clean installed-package integration run;
- exact fixtures and derivations with anonymous/offline proof;
- canonical JSON, Markdown, and JUnit evidence; and
- before/after baseline certification showing no unrelated change.

Never call a smaller suite by a larger milestone's name. In particular,
`pipeline-end-to-end` must remain unavailable until its full acceptance contract
is implemented and certified.
