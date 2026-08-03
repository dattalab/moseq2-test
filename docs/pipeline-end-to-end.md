# `pipeline-end-to-end` boundary

`pipeline-end-to-end` is intentionally `implemented: false`. Its owner is the
DattaLab `moseq2-test` maintainers, with the Datta Lab project owner approving
scope and resources. The CLI must report `profile_unavailable`; it must never
silently run `pipeline-smoke` under this name.

A later milestone must define and certify a complete recording-to-final-results
workflow using representative depth recordings, production-scale configuration,
explicit classifier/model identities, stage restart behavior, semantic outputs,
bounded runtime/storage, exact failure policy, and a trusted execution location.
That work must answer how much real data is sufficient, which outputs are
scientifically meaningful, and what nondeterminism is acceptable. Those are
scientific-correctness decisions deliberately excluded from packaging Chunk 1.

Until that review is complete, `pipeline-smoke` is the compact regression
contract and must not be described as exhaustive end-to-end validation.
