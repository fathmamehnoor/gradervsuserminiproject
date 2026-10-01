#!/usr/bin/env python3
"""Per-run and per-arm token usage and cost, from the [provider-usage] lines in rollout.log.

    python analysis/cost.py                                   # every run under results/ (smoke runs excluded)
    python analysis/cost.py results/N0 --plan 150             # project the cost of 150 more runs
    python analysis/cost.py --price-in 0.66 --price-cached 0.022 --price-out 1.98   # $/M tokens

Both the OpenRouter and the Fireworks provider print one usage line per API call. OpenRouter lines
carry the billed cost (cost=, USD), which is used as-is. Fireworks lines carry tokens only; give
--price-* (USD per million tokens) to price them. Cached input is billed at the cached rate,
the rest of the input at the input rate.

Run cost depends strongly on the number of steps (every turn resends the whole history), so
--plan prints two projections: mean cost x N and, more conservatively, max cost x N.
Writes analysis/out/cost_runs.csv.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import REPO_ROOT  # noqa: E402

OUT = HERE / "out"
USAGE_RE = re.compile(r"\[provider-usage\](.*)")
FIELD_RE = re.compile(r"(\w+)=(\S+)")


def _num(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "None") else None
    except ValueError:
        return None


def parse_usage(log_text: str) -> list[dict]:
    """One dict per [provider-usage] line: in, cache_read, out, reasoning (ints), cost (float|None), served_by."""
    calls = []
    for m in USAGE_RE.finditer(log_text):
        f = dict(FIELD_RE.findall(m.group(1)))
        calls.append({
            "in": int(_num(f.get("in")) or 0),
            "cache_read": int(_num(f.get("cache_read")) or 0),
            "out": int(_num(f.get("out")) or 0),
            "reasoning": int(_num(f.get("reasoning")) or 0),
            "cost": _num(f.get("cost")),
            "served_by": f.get("served_by"),
        })
    return calls


def run_cost(calls: list[dict], prices: dict | None) -> tuple[float | None, str]:
    """Billed cost if every call reports one, else the cost from --price-* if given."""
    if calls and all(c["cost"] is not None for c in calls):
        return sum(c["cost"] for c in calls), "reported"
    if prices:
        total = 0.0
        for c in calls:
            fresh = max(c["in"] - c["cache_read"], 0)
            total += (fresh * prices["in"] + c["cache_read"] * prices["cached"] + c["out"] * prices["out"]) / 1e6
        return total, "priced"
    return None, "unpriced"


def arm_of(run_dir: Path) -> str:
    """condition_id from final/run_condition.json, else the results/<ARM>/ path component."""
    cond = run_dir / "final" / "run_condition.json"
    if cond.is_file():
        try:
            return json.loads(cond.read_text()).get("condition_id") or "?"
        except json.JSONDecodeError:
            pass
    parts = run_dir.parts
    i = parts.index("results") if "results" in parts else -1
    return parts[i + 1] if 0 <= i < len(parts) - 1 else "?"


def find_logs(paths: list[str], include_smoke: bool) -> list[Path]:
    logs: set[Path] = set()
    for p in map(Path, paths):
        if p.is_file() and p.name == "rollout.log":
            logs.add(p.resolve())
        elif p.is_dir():
            logs.update(x.resolve() for x in p.glob("**/rollout.log"))
    if not include_smoke:
        logs = {x for x in logs if "smoke" not in x.parts}
    return sorted(logs)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", default=[str(REPO_ROOT / "results")])
    ap.add_argument("--include-smoke", action="store_true", help="include results/smoke runs (mock)")
    ap.add_argument("--price-in", type=float, help="USD per million uncached input tokens")
    ap.add_argument("--price-cached", type=float, help="USD per million cached input tokens")
    ap.add_argument("--price-out", type=float, help="USD per million output tokens")
    ap.add_argument("--plan", type=int, help="project the cost of this many further runs")
    args = ap.parse_args(argv)

    given = [args.price_in, args.price_cached, args.price_out]
    if any(v is not None for v in given) and not all(v is not None for v in given):
        ap.error("give all three of --price-in, --price-cached, --price-out")
    prices = {"in": args.price_in, "cached": args.price_cached, "out": args.price_out} if all(
        v is not None for v in given) else None

    rows = []
    for log in find_logs(args.paths, args.include_smoke):
        calls = parse_usage(log.read_text(errors="replace"))
        if not calls:
            continue
        cost, how = run_cost(calls, prices)
        tin = sum(c["in"] for c in calls)
        tcache = sum(c["cache_read"] for c in calls)
        rows.append({
            "arm": arm_of(log.parent), "run_dir": str(log.parent), "calls": len(calls),
            "in": tin, "cache_read": tcache, "cached_share": round(tcache / tin, 3) if tin else 0.0,
            "out": sum(c["out"] for c in calls), "reasoning": sum(c["reasoning"] for c in calls),
            "cost_usd": None if cost is None else round(cost, 4), "cost_source": how,
            "served_by": ",".join(sorted({c["served_by"] for c in calls if c["served_by"]})),
        })

    if not rows:
        print("no [provider-usage] lines found under:", " ".join(args.paths))
        return 1

    print(f"{'arm':<4} {'calls':>5} {'in':>10} {'cached':>7} {'out':>8} {'cost $':>8}  served_by  run")
    for r in rows:
        cost = "n/a" if r["cost_usd"] is None else f"{r['cost_usd']:.3f}"
        rd = Path(r["run_dir"])
        rd = rd.relative_to(REPO_ROOT) if rd.is_relative_to(REPO_ROOT) else rd
        print(f"{r['arm']:<4} {r['calls']:>5} {r['in']:>10} {r['cached_share']:>7.0%} {r['out']:>8} {cost:>8}  "
              f"{r['served_by'] or '-'}  {rd}")

    priced = [r for r in rows if r["cost_usd"] is not None]
    if len(priced) < len(rows):
        print(f"\n{len(rows) - len(priced)} run(s) have no billed cost; pass --price-in/--price-cached/--price-out to price them.")
    if priced:
        by_arm: dict[str, list[float]] = defaultdict(list)
        for r in priced:
            by_arm[r["arm"]].append(r["cost_usd"])
        print(f"\n{'arm':<4} {'n':>3} {'mean $':>8} {'max $':>8} {'total $':>9}")
        for arm in sorted(by_arm):
            c = by_arm[arm]
            print(f"{arm:<4} {len(c):>3} {statistics.mean(c):>8.3f} {max(c):>8.3f} {sum(c):>9.3f}")
        allc = [r["cost_usd"] for r in priced]
        print(f"{'all':<4} {len(allc):>3} {statistics.mean(allc):>8.3f} {max(allc):>8.3f} {sum(allc):>9.3f}")
        if args.plan:
            print(f"\nprojection for {args.plan} more runs: ~${statistics.mean(allc) * args.plan:,.2f} at the mean, "
                  f"~${max(allc) * args.plan:,.2f} at the max (based on {len(allc)} run(s); step counts vary a lot)")

    OUT.mkdir(exist_ok=True)
    with open(OUT / "cost_runs.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"\nwrote {OUT / 'cost_runs.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
