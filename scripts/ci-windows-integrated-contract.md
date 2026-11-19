# Windows integrated diagnostic contract and limits

This candidate is not a deployed gate and has not established a 30-minute
end-to-end bound. It does not by itself validate docs, ordinary example linking,
lint, other platforms, or equivalence to the unsplit workspace compiler graph.

## Pinned Cargo output evidence
nextest 0.9.150, commit d225b1daf362c1ca8e5e048132618c1a81c57a28:
https://github.com/nextest-rs/nextest/blob/d225b1daf362c1ca8e5e048132618c1a81c57a28/cargo-nextest/src/dispatch/core/base.rs#L588-L606
When JSON forwarding is requested, every Cargo stdout line is forwarded before
the binary-list builder processes it. Cargo process failure is then rejected.
The parser retains compiler-artifact records including cached records and requires
a successful build-finished event; other Cargo JSON event shapes may be present.

## Deliberately bounded evidence
Configured metadata selects targets but is not a compiler-unit graph. Artifact
records retain full target data (including source), profile, executable and output
filenames. Duplicate identities fail closed. The archive label check establishes
a unique compatible Cargo artifact record, NOT equality of executable bytes or
paths: nextest archive extraction remaps paths and workspace build wrappers can
generate additional binaries. That stronger binding still requires validation.
The cross-kind check compares feature-set collections for shared library labels.
It does not compare complete compiler-unit edges, host/target relationships,
build-script outputs or nonshared dependencies. Do not label it graph equivalence.

## Runtime acceptance
Consumer creates its proof only after nextest succeeds and Cargo.lock is unchanged.
Reconciliation rejects proofs lacking runtime_success. This is supplemental, not
a replacement for workflow job results. The eventual workflow MUST depend on
every producer and every runtime consumer and require all their results success.
Never run acceptance with always() alone or accept proof upload/presence as a pass.
A source-reviewed workflow enforcing those dependencies is still to be written.
