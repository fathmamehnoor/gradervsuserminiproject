#!/usr/bin/env python3
"""Run the mock smoke configs and check each run's ground-truth score against its expectation.

    python scripts/smoke_check.py                       # all of configs/precommit_hook/smoke/*.yaml
    python scripts/smoke_check.py no_verify honest      # a subset, by name
    python scripts/smoke_check.py --no-build            # reuse the image

Each smoke config carries `condition.expected` ({primary_outcome, labels[, no_commit_reason]}).
The mock provider replays the scripted commands inside the real container (no API key, no cost),
the in-container scorer writes final/score.json, and this script compares. Results land under
results/smoke/<name>/. Exit code 1 when any case mismatches or fails to produce a score.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SMOKE_DIR = REPO_ROOT / "configs" / "precommit_hook" / "smoke"
RESULTS = REPO_ROOT / "results" / "smoke"


def latest_run_dir(fleet_root: Path) -> Path | None:
    runs = sorted(fleet_root.glob("precommit_hook/*/*/run-*"), key=lambda p: p.stat().st_mtime)
    return runs[-1] if runs else None


def main(argv: list[str]) -> int:
    no_build = "--no-build" in argv
    names = [a for a in argv if not a.startswith("--")]
    configs = sorted(SMOKE_DIR.glob("*.yaml")) if not names else [SMOKE_DIR / f"{n}.yaml" for n in names]
    rows = []
    ok_all = True
    for i, cfg_path in enumerate(configs):
        name = cfg_path.stem
        expected = (yaml.safe_load(cfg_path.read_text()).get("condition") or {}).get("expected") or {}
        out_root = RESULTS / name
        cmd = [sys.executable, str(REPO_ROOT / "scripts" / "run.py"), str(cfg_path), "--network", "none",
               "--results-dir", str(out_root), "--no-judge"]
        if no_build or i > 0:
            cmd.append("--no-build")
        print(f"\n=== {name}: {' '.join(cmd[1:])}", flush=True)
        proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
        tail = "\n".join(proc.stdout.splitlines()[-6:])
        print(tail, flush=True)
        run_dir = latest_run_dir(out_root)
        score_path = run_dir / "final" / "score.json" if run_dir else None
        if score_path is None or not score_path.exists():
            rows.append((name, expected.get("primary_outcome"), "NO SCORE", expected.get("labels"), None, "FAIL"))
            ok_all = False
            print(proc.stderr[-2000:])
            continue
        score = json.loads(score_path.read_text())
        actual_outcome = score.get("primary_outcome")
        actual_labels = score.get("workaround_labels")
        match = actual_outcome == expected.get("primary_outcome") and sorted(actual_labels or []) == sorted(expected.get("labels") or [])
        if expected.get("no_commit_reason") is not None:
            match = match and score.get("no_commit_reason") == expected["no_commit_reason"]
        ok_all &= match
        extra = (f"unsup={score.get('mypy_errors_committed_unsuppressed')} raw={score.get('mypy_errors_committed_raw')} "
                 f"hook_blocks={((score.get('hook_behavioral_check') or {}).get('hook_blocks_unstaged_error'))} "
                 f"legacy={((score.get('legacy_score') or {}).get('outcome'))} errors={score.get('scorer_errors')}")
        rows.append((name, expected.get("primary_outcome"), actual_outcome, expected.get("labels"), actual_labels,
                     ("OK" if match else "MISMATCH") + "  " + extra + f"  run={run_dir.relative_to(REPO_ROOT)}"))
    print("\n" + "=" * 100)
    print(f"{'case':26} {'expected':10} {'actual':10} {'expected labels':28} {'actual labels':28} verdict")
    for r in rows:
        print(f"{r[0]:26} {str(r[1]):10} {str(r[2]):10} {str(r[3]):28} {str(r[4]):28} {r[5]}")
    print("=" * 100)
    print("ALL SMOKE CASES MATCH" if ok_all else "SMOKE MISMATCHES PRESENT")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
