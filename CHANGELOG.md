# Changelog

## 0.1.0 - Unreleased

- Establish the public controller, schemas, worker protocol, fixture registry,
  clean installed-package runners, semantic comparison policies, reports, and
  baseline profiles.
- Preserve historical classifier-download tests under network isolation by
  resolving their two exact legacy URLs to separately hash-verified public
  fixtures; unknown URLs fail closed.
- Compare cold and offline-warm run semantics after removing only disposable
  sandbox paths, container hostnames, execution timings, and raw hashes whose
  artifact comparators have already evaluated their semantic content.
