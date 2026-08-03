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
- Build `pyhsmm` and `pyhsmm-autoregressive` candidates offline by staging the
  exact hash-locked Eigen 3.3.7 headers into the disposable source export; the
  package checkout remains read-only and build logs retain the input identity.
- Build compiled candidates with a separate, hash-locked GCC/G++ 11.4 prefix;
  compiler provenance is logged and the frozen Python 3.7 runtime is unchanged.
- Require the worker publication workflow to build, install, and import a real
  Python 3.7 C++ extension before an image can be published.
