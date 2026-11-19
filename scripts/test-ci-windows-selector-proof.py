"""Check actual single-run fixture proof; no claim of eight runtime shards."""
import importlib.util
from pathlib import Path
import sys

spec = importlib.util.spec_from_file_location("contract", Path(__file__).with_name("ci-windows-archive-contract.py"))
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
proof = c.read("pilot-proof/proof.json")
assert proof["config"] == sys.argv[1]
assert proof["target_kind"] == "integration" and proof["shard"] == "1"
assert proof["runtime_success"] is True
assert proof["selected"] == proof["expected"] == c.cases(c.inventory("pilot-current.json"), selected=True)
audit = c.read("pilot-proof/audit-digests.json")
for filename, key in (("compiler-units.json", "units_digest"),
                      ("configured-targets.json", "targets_digest"),
                      ("executable-digests.json", "executables_digest"),
                      ("parity-manifest.json", "parity_file_digest"),
                      ("inventory-normalized.json", "inventory_digest")):
    assert c.digest(Path("pilot-proof") / filename) == audit[key]
assert c.read("pilot-proof/executable-digests.json") == c.read("pilot-bundle/executable-digests.json")
print("SINGLE_RUNTIME_PROOF_BYTES_BOUND", sys.argv[1], len(proof["selected"]))

parity_manifest = c.read("pilot-proof/parity-manifest.json")
c.parity.validate_manifest(parity_manifest)
assert proof["parity_digest"] == c.parity.manifest_digest(parity_manifest)
print("MOCK_PARITY_PROOF_DIGEST_BOUND_NO_APPLICATION_EVIDENCE")
