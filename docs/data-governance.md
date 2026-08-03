# Fixture data governance

## Public source and identity

Canonical objects live under the lab-controlled public HTTPS prefix
`https://moseq-data.s3.amazonaws.com/moseq2-test/v1/objects/sha256/`.
Keys contain the SHA-256; manifests also record logical ID, filename, exact
bytes, provenance URL, publication decision, trust class, terms, license and
citation. The optional O2 mirror is a transport optimization, not a different
source of truth.

Anonymous users may read known object URLs. Public listing, writing,
overwriting, and deletion are not part of the interface. No AWS identity,
cookie, HMS network, or signed URL is required by ordinary users or CI.

## Cache and retention

Cache hits are reverified by size and SHA-256. Downloads use per-object locks,
temporary files, atomic replacement, and create read-only objects. Safe archive
expansions are derived below the content hash and may be discarded/recreated.
Run workspaces and reports never modify cached inputs. The public prefix is the
durable baseline store; CI caches are disposable, and GitHub result artifacts
follow the retention configured in their workflow.

Trusted pickle objects may be deserialized only inside the isolated legacy
worker after their manifest trust class and hash are verified. The controller
must not deserialize them.

## Publishing or adding data

1. Confirm project-owner publication approval, terms, provenance, license,
   citation, trust class, exact size/hash, and whether a derivation is required.
2. Add a new versioned manifest entry and tests. Do not replace bytes behind an
   existing ID/hash or broaden an object's clearance.
3. Run `moseq2-test data publish --fixture-set NAME --source-root PATH --dry-run`.
4. In a separately authorized maintainer operation, rerun with `--execute`.
   Publication uses a content-hash key and create-only semantics.
5. Verify anonymous HTTPS read-back, exact bytes, disabled anonymous listing and
   writes, public and mirror parity, and complete offline operation.
6. Retain the publication record and rerun baseline certification. Do not fetch
   the historical 20/48-session full archives in normal profiles.

Deleting or replacing a published baseline object is a breaking governance
event. Publish a new hash/manifest version and retain the old object while any
supported lock references it.
