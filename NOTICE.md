# Notices and provenance

`moseq2-test` is distributed under the non-commercial research and academic
terms in `LICENSE.md`, matching the five MoSeq2 repositories at their locked
baseline revisions.

The legacy worker environment may aggregate the following separately licensed
projects:

- `pybasicbayes` at commit `61f65ad6c781288605ec5f7347efcc5dbd73c4fc`
  (MIT; `licenses/pybasicbayes-LICENSE-MIT`);
- `pyhsmm` at commit `4e739166746f92bfc968d281f2c1d31e3471409f`
  (MIT; `licenses/pyhsmm-LICENSE-MIT`); and
- `pyhsmm-autoregressive` at commit
  `2a4c73c08dcda959b9bac2f03a2b976dabbc37af` (GPL-2.0;
  `licenses/pyhsmm-autoregressive-LICENSE-GPL`). Its corresponding source is
  available at
  `https://github.com/dattalab/pyhsmm-autoregressive/tree/2a4c73c08dcda959b9bac2f03a2b976dabbc37af`.
- Git 2.43.0 from the exact conda-forge build
  `git-2.43.0-pl5321h709897a_1` (GPL-2.0-or-later and
  LGPL-2.1-or-later) is included solely to export candidate worktrees. Its
  corresponding upstream source is retained in the image as
  `git-2.43.0.tar.gz`; the exact build recipe is available at
  `https://github.com/conda-forge/git-feedstock/tree/e8101698475ea5a34d0bb5cfa116711c3cfd0734`.
- The separately locked candidate-build prefix includes GCC/G++ 11.4.0 and
  GNU binutils 2.40 (GPL with the GCC Runtime Library Exception where
  applicable), plus libxcrypt 4.4.36 (LGPL-2.1-or-later). Their exact binary
  artifacts are recorded in
  `environments/legacy-build-toolchain-linux-64.lock.yml`; corresponding
  upstream sources are available from the GNU GCC and binutils release
  archives and the libxcrypt 4.4.36 release.

Fixture provenance, citations, object-level terms, and hashes live in the
versioned fixture manifests. The obsolete bucket-root MoSeq2 EULA is not the
governing document for the `moseq2-test/` S3 prefix.
