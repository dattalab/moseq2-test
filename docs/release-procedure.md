# Release procedure

The named release owner is John Jacoby (`@JRJacoby`). A different maintainer may
perform a release only after the owner records that delegation in the release
issue. PyPI publication is not part of the initial implementation and must not
occur until the first usable tagged release is separately approved.

## Prepare

1. Open a release issue listing the intended version, central commit, schema and
   profile versions, worker digest, fixture manifests, caller Action pin, and
   known-failure review dates.
2. Confirm CODEOWNERS review, a clean tree, current notices/corresponding-source
   references, and no unrelated package/scientific change.
3. Run framework tests, lint, type checks, sdist/wheel build, wheel installation
   smoke, schema verification, and a deliberate negative case.
4. Obtain three consecutive accepted hosted cold/warm baseline certifications
   at the intended bytes. Verify the public worker digest and GitHub/registry
   provenance, retained SPDX SBOM, and vulnerability scan.
5. Verify anonymous retrieval of every fixture, offline rerun, cache/source
   immutability, and resource ceilings. Record runtime/download/peak disk.
6. Update changelog and handoff docs. Have a second maintainer reproduce the
   documented baseline without the release owner's shell history.

## Publish

Create a signed or GitHub-attested release from the reviewed commit. Tags are
human release identities; caller workflows continue pinning full commits and
the worker continues pinning an OCI digest. If PyPI publication is approved,
publish the already-certified sdist/wheel through a trusted environment with
provenance, then install those exact public artifacts and rerun smoke tests.

Do not rebuild release artifacts after certification, overwrite fixture keys,
retag a different worker image, merge caller PRs automatically, or promote an
informational caller check to required without its stability review.

## After release

Verify release checksums and anonymous installation, update supported caller
pins through separate thin PRs, retain certification/publication evidence, and
schedule review of worker vulnerabilities and known failures. A failed
verification stops promotion; publish a new version rather than replacing
released bytes.
