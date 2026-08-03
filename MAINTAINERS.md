# Maintainers and review rules

## Named ownership

- Primary maintainer and release owner: John Jacoby (`@JRJacoby`).
- Scientific-behavior approval: the Datta Lab project owner or a person they
  explicitly delegate in the relevant issue/PR.
- Package-owned tests and behavior: maintainers of the affected MoSeq2 package.

Use the public GitHub issue tracker for bugs and planned work. Use GitHub's
private security-advisory interface for vulnerabilities; do not disclose
secrets, signed URLs, restricted paths, or unreviewed vulnerable details in a
public issue.

## Review rules

All changes require pull-request review. The primary maintainer owns the whole
tree through CODEOWNERS. A second qualified review is required for releases and
for changes to licenses/notices, schemas, source/wheel/environment locks,
fixture manifests/publication, trusted pickle handling, worker images,
known-failure or intentional-change records, semantic comparator tolerances,
baseline outputs, GitHub trust boundaries, and caller Action behavior.

Baseline and caller checks begin informational. Making a check required needs a
separate stability review after repeated green runs. No automation in this
repository merges caller PRs, publishes PyPI, changes scientific behavior, or
runs public-fork code on O2.

## Repository settings

Keep issues and private vulnerability reporting enabled. Protect `main` once
the initial implementation PR series is stable: require a pull request,
CODEOWNERS review, resolved conversations, and the framework CI check; forbid
force pushes and deletion. Add baseline certification as required only after
its stability review. Limit release and package publication environments to the
release owner and explicitly delegated maintainers.
