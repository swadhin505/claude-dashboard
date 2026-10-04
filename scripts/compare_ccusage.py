"""Manual, offline cross-check. Requires an explicitly supplied ccusage executable.

Usage: python scripts/compare_ccusage.py --binary /path/to/ccusage.exe
Only synthetic fixture copies are scanned. No dependency installation or network I/O.
This is a comparison, not a claim that another tool defines our expected results.
"""

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from claude_metrics.config import Settings
from claude_metrics.ingestion.scanner import scan
from claude_metrics.pricing.calculator import PricingPolicy
from claude_metrics.pricing.receipts import apply_pricing
from claude_metrics.reports import ReportFilter, overview
from claude_metrics.storage import connect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    project = Path(__file__).resolve().parents[1]
    cases = json.loads((project / "tests/golden/claude-cases.json").read_text())["cases"]
    version = subprocess.run(
        [str(binary), "--version"], capture_output=True, text=True, check=True, timeout=15
    ).stdout.strip()
    print(json.dumps({"ccusage_version": version, "mode": "calculate", "offline": True}))
    with tempfile.TemporaryDirectory(prefix="claude-metrics-comparison-") as temporary:
        for case in cases:
            base = Path(temporary) / case["id"]
            root = base / "claude/projects"
            for name in case["files"]:
                target = root / "fixture" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(project / "tests/fixtures/claude" / name, target)
            config = base / "ccusage.json"
            config.write_text("{}", encoding="utf-8")
            env = dict(
                os.environ,
                CLAUDE_CONFIG_DIR=str(root.parent),
                XDG_CACHE_HOME=str(base / "cache"),
                CCUSAGE_OFFLINE="1",
                NO_COLOR="1",
            )
            process = subprocess.run(
                [
                    str(binary),
                    "claude",
                    "daily",
                    "--offline",
                    "--json",
                    "--mode",
                    "calculate",
                    "--single-thread",
                    "--timezone",
                    "Asia/Kolkata",
                    "--config",
                    str(config),
                ],
                cwd=base,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            other = json.loads(process.stdout)
            settings = Settings(base / "ours", (root,), "Asia/Kolkata")
            scan(settings)
            apply_pricing(settings, policy=PricingPolicy(assume_provider="anthropic"))
            with connect(settings.database, readonly=True) as conn:
                ours = overview(conn, ReportFilter("Asia/Kolkata"))["total"]
            print(
                json.dumps(
                    {
                        "case": case["id"],
                        "ours": {
                            k: ours[k]
                            for k in (
                                "requests",
                                "known_tokens",
                                "known_cost_nanos",
                                "cost_status",
                                "unpriced_tokens",
                                "unpriced_units",
                            )
                        },
                        "ccusage_totals": other.get("totals", other),
                    },
                    sort_keys=True,
                )
            )


if __name__ == "__main__":
    main()
