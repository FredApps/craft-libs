"""Report exact nextest inventory; no test executions or filtered equivalence claims."""
import json
from pathlib import Path
import sys

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
suites = data["rust-suites"]
if not suites or data["test-count"] <= 0:
    raise SystemExit("empty archive inventory")
print("TEST_COUNT", data["test-count"])
for binary_id, suite in sorted(suites.items()):
    cases = suite["testcases"]
    print("BINARY", binary_id, "tests", len(cases), "ignored", sum(case["ignored"] for case in cases.values()))
    for name, case in sorted(cases.items()):
        print("CASE", binary_id, name, "ignored", case["ignored"])
