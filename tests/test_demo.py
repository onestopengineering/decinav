"""The demo builder runs the real workflow end to end and yields every freshness state."""

import json
from pathlib import Path
import subprocess
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_demo_builder_produces_a_valid_repository_with_every_freshness_state(tmp_path: Path) -> None:
    target = tmp_path / "parcel"
    result = subprocess.run(
        [sys.executable, str(REPOSITORY_ROOT / "scripts" / "build_demo.py"), str(target)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Validated 12 ADR(s)." in result.stdout

    context = subprocess.run(
        [sys.executable, "-m", "decinav", "context", "--format", "json"],
        cwd=target, capture_output=True, text=True, timeout=60,
    )
    records = json.loads(context.stdout)["records"]
    states = {record["id"]: record["freshness"]["state"] for record in records}
    assert states["ADR-0007"] == "sources-changed"
    assert {states[i] for i in ("ADR-0002", "ADR-0004", "ADR-0009")} == {"aging"}
    assert sum(state == "fresh" for state in states.values()) == 8
    assert {record["status"] for record in records} == {"baseline", "accepted", "proposed"}
    assert (target / ".decinav" / "last-impact.json").is_file()

    again = subprocess.run(
        [sys.executable, str(REPOSITORY_ROOT / "scripts" / "build_demo.py"), str(target)],
        capture_output=True, text=True, timeout=60,
    )
    assert again.returncode != 0 and "refusing to overwrite" in (again.stdout + again.stderr)
