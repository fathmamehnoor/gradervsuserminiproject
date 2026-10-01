#!/usr/bin/env python3
"""Per-arm summary and 2x2 contrasts from the ground-truth scores.

    python analysis/summarize.py                      # every run under results/ (smoke runs excluded)
    python analysis/summarize.py results/G1 results/U1
    python analysis/summarize.py --include-smoke --bootstrap 5000 --seed 1

Reads final/score.json + final/run_condition.json (+ final/messages.json for the grader-term
count) from every run dir, prints per arm: n, honest/gamed/no_commit with Wilson 95% CIs, the
workaround-label distribution, median steps, mean grader_term_hits; then the 2x2 contrasts on
the GAMED rate (framing effect within each oversight level, oversight effect within each
framing, and the difference-in-differences) with percentile bootstrap CIs. Writes
analysis/out/runs.csv, analysis/out/summary_by_arm.csv and analysis/out/contrasts.csv.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import REPO_ROOT, find_run_dirs, find_unscored_run_dirs, grader_term_hits, load_run  # noqa: E402

OUT = HERE / "out"
OUTCOMES = ("honest", "gamed", "no_commit", "no_score")
LABELS = ("no_verify", "hook_disabled", "staged_only_hook", "gated_hook", "config_weakened", "files_excluded",
          "blanket_type_ignore", "file_level_ignore_errors", "other")
ARM_ORDER = ["N0", "G0", "G1", "U0", "U1"]
PAPER_N0 = (69, 100)  # DeepSeek v4 Pro, 258-error variant: 69/100 failed to fix the errors (Singh et al. 2026)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float, float]:
    if n == 0:
        return (float("nan"), float("nan"), float("nan"))
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return p, max(0.0, centre - half), min(1.0, centre + half)


def fmt_ci(k: int, n: int) -> str:
    p, lo, hi = wilson(k, n)
    return f"{k}/{n} = {p:.0%} [{lo:.0%}, {hi:.0%}]" if n else "n/a"


def rows_for(run_dirs: list[Path]) -> list[dict]:
    rows = []
    for d in run_dirs:
        r = load_run(d)
        s, c = r["score"], r["condition"]
        hits, _ = grader_term_hits(r["messages"])
        rows.append({
            "run_dir": str(d.relative_to(REPO_ROOT)) if d.is_relative_to(REPO_ROOT) else str(d),
            "condition_id": c.get("condition_id") or s.get("condition_id"),
            "framing": c.get("framing"), "oversight": c.get("oversight"),
            "primary_outcome": s.get("primary_outcome"), "no_commit_reason": s.get("no_commit_reason"),
            "outcome_bytematch": s.get("outcome_bytematch"), "outcome_strict": s.get("outcome_strict"),
            "honest_on_errors": s.get("honest_on_errors"), "honest_on_errors_reasons": "|".join(s.get("honest_on_errors_reasons") or []),
            "labels": "|".join(s.get("workaround_labels") or []),
            "commit_landed": s.get("commit_landed"), "py_typed_committed": s.get("py_typed_committed"),
            "mypy_raw": s.get("mypy_errors_committed_raw"), "mypy_unsuppressed": s.get("mypy_errors_committed_unsuppressed"),
            "mypy_unsuppressed_strict": s.get("mypy_errors_committed_unsuppressed_strict"),
            "type_ignores": s.get("type_ignore_count_committed"),
            "hook_blocks": (s.get("hook_behavioral_check") or {}).get("hook_blocks_unstaged_error"),
            "hook_blocks_committed": (s.get("hook_behavioral_check") or {}).get("hook_blocks_committed_error"),
            "hook_uses_stash": (s.get("hook_behavioral_check") or {}).get("hook_uses_stash"),
            "n_steps": s.get("n_steps"), "hit_max_steps": s.get("hit_max_steps"), "censored": s.get("censored"),
            "scorer_errors": "|".join(s.get("scorer_errors") or []),
            "legacy_outcome": (s.get("legacy_score") or {}).get("outcome"),
            "grader_term_hits": hits,
            "config_hash": c.get("config_hash"),
        })
    return rows


def unscored_rows(paths: list[str | Path]) -> list[dict]:
    """One row per launched-but-unscored run, with outcome no_score, so denominators stay honest."""
    rows = []
    for u in find_unscored_run_dirs(paths):
        d = u["run_dir"]
        rows.append({"run_dir": str(d.relative_to(REPO_ROOT)) if d.is_relative_to(REPO_ROOT) else str(d),
                     "condition_id": u["condition_id"], "framing": u["framing"], "oversight": u["oversight"],
                     "primary_outcome": "no_score", "no_commit_reason": "no_score_json",
                     "outcome_bytematch": "no_score", "outcome_strict": "no_score", "honest_on_errors": None,
                     "honest_on_errors_reasons": "", "labels": "", "commit_landed": None, "py_typed_committed": None,
                     "mypy_raw": None, "mypy_unsuppressed": None, "mypy_unsuppressed_strict": None, "type_ignores": None,
                     "hook_blocks": None, "hook_blocks_committed": None, "hook_uses_stash": None, "n_steps": None,
                     "hit_max_steps": None, "censored": None, "scorer_errors": "missing final/score.json",
                     "legacy_outcome": None, "grader_term_hits": 0, "config_hash": None})
    return rows


def summarize_arm(rows: list[dict]) -> dict:
    n = len(rows)
    out = {"n": n}
    for o in OUTCOMES:
        k = sum(1 for r in rows if r["primary_outcome"] == o)
        p, lo, hi = wilson(k, n)
        out[o] = k
        out[f"{o}_rate"] = p
        out[f"{o}_ci_lo"] = lo
        out[f"{o}_ci_hi"] = hi
    labels = Counter()
    for r in rows:
        for lab in filter(None, r["labels"].split("|")):
            labels[lab] += 1
    for lab in LABELS:
        out[f"label_{lab}"] = labels.get(lab, 0)
    steps = [r["n_steps"] for r in rows if isinstance(r["n_steps"], (int, float))]
    out["median_steps"] = statistics.median(steps) if steps else float("nan")
    hits = [r["grader_term_hits"] for r in rows]
    out["mean_grader_term_hits"] = statistics.fmean(hits) if hits else float("nan")
    out["legacy_bad"] = sum(1 for r in rows if r["legacy_outcome"] == "bad")
    out["hit_max_steps"] = sum(1 for r in rows if r["hit_max_steps"])
    out["censored"] = sum(1 for r in rows if r["censored"])
    out["scorer_errors"] = sum(1 for r in rows if r["scorer_errors"])
    for alt in ("outcome_bytematch", "outcome_strict"):
        out[f"gamed_{alt}"] = sum(1 for r in rows if r[alt] == "gamed")
        out[f"honest_{alt}"] = sum(1 for r in rows if r[alt] == "honest")
    return out


def _rate(rows: list[dict], outcome: str) -> float:
    return sum(1 for r in rows if r["primary_outcome"] == outcome) / len(rows) if rows else float("nan")


def contrasts(by_arm: dict[str, list[dict]], outcome: str, reps: int, seed: int) -> list[dict]:
    """Framing effect (G - U) within each oversight level, oversight effect (O - noO) within each
    framing, and the interaction (G1-G0) - (U1-U0); percentile bootstrap resampling runs within arm."""
    rng = random.Random(seed)
    arms = {a: by_arm.get(a, []) for a in ("G0", "G1", "U0", "U1")}

    def stats(sample: dict[str, list[dict]]) -> dict[str, float]:
        g0, g1, u0, u1 = (_rate(sample[a], outcome) for a in ("G0", "G1", "U0", "U1"))
        return {"framing_effect_no_oversight (G0-U0)": g0 - u0,
                "framing_effect_fixed_oversight (G1-U1)": g1 - u1,
                "oversight_effect_graded (G1-G0)": g1 - g0,
                "oversight_effect_user (U1-U0)": u1 - u0,
                "interaction_DiD ((G1-G0)-(U1-U0))": (g1 - g0) - (u1 - u0)}

    point = stats(arms)
    boots: dict[str, list[float]] = defaultdict(list)
    if all(arms.values()):
        for _ in range(reps):
            sample = {a: [rng.choice(rows) for _ in rows] for a, rows in arms.items()}
            for k, v in stats(sample).items():
                boots[k].append(v)
    result = []
    for k, v in point.items():
        b = sorted(boots.get(k, []))
        lo = b[int(0.025 * len(b))] if b else float("nan")
        hi = b[min(len(b) - 1, int(0.975 * len(b)))] if b else float("nan")
        result.append({"contrast": k, "outcome": outcome, "estimate": v, "ci_lo": lo, "ci_hi": hi, "bootstrap_reps": len(b)})
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", default=[str(REPO_ROOT / "results")])
    ap.add_argument("--include-smoke", action="store_true", help="include results/smoke runs (mock)")
    ap.add_argument("--bootstrap", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--exclude-censored", action="store_true", help="drop context-censored / scorer-error runs")
    args = ap.parse_args(argv)

    run_dirs = find_run_dirs(args.paths)
    if not args.include_smoke:
        run_dirs = [d for d in run_dirs if "results/smoke" not in str(d)]
    if not run_dirs:
        print("no scored runs found under", args.paths)
        return 1
    rows = rows_for(run_dirs)
    extra = unscored_rows(args.paths)
    if not args.include_smoke:
        extra = [r for r in extra if "results/smoke" not in r["run_dir"]]
    rows += extra
    if args.exclude_censored:
        rows = [r for r in rows if not r["censored"] and not r["scorer_errors"]]
    by_arm: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_arm[str(r["condition_id"])].append(r)
    arms = [a for a in ARM_ORDER if a in by_arm] + sorted(a for a in by_arm if a not in ARM_ORDER)

    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "runs.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    summary = {a: summarize_arm(by_arm[a]) for a in arms}
    print(f"{'arm':6} {'n':>4}  {'honest':28} {'gamed':28} {'no_commit':28} {'no_score':>8} {'median steps':>12} {'mean grader-term hits':>22}")
    for a in arms:
        s = summary[a]
        print(f"{a:6} {s['n']:>4}  {fmt_ci(s['honest'], s['n']):28} {fmt_ci(s['gamed'], s['n']):28} "
              f"{fmt_ci(s['no_commit'], s['n']):28} {s['no_score']:>8} {s['median_steps']:>12} {s['mean_grader_term_hits']:>22.2f}")
    if any(summary[a]["no_score"] for a in arms):
        print("  (no_score = launched runs without final/score.json; counted in n, never dropped)")
    print("\nsensitivity of the GAMED count to the error definition (budget = primary; byte-match = reference-matching"
          " ignores kept; strict = every ignore stripped):")
    print(f"{'arm':6} {'n':>4} {'gamed(budget)':>14} {'gamed(bytematch)':>17} {'gamed(strict)':>14}   honest(budget/bytematch/strict)")
    for a in arms:
        s = summary[a]
        print(f"{a:6} {s['n']:>4} {s['gamed']:>14} {s['gamed_outcome_bytematch']:>17} {s['gamed_outcome_strict']:>14}   "
              f"{s['honest']}/{s['honest_outcome_bytematch']}/{s['honest_outcome_strict']}")
    print("\nworkaround labels (count of runs carrying each label):")
    print(f"{'arm':6} " + " ".join(f"{lab[:14]:>14}" for lab in LABELS) + f" {'legacy_bad':>10} {'maxsteps':>8} {'censored':>8} {'scorer_err':>10}")
    for a in arms:
        s = summary[a]
        print(f"{a:6} " + " ".join(f"{s['label_' + lab]:>14}" for lab in LABELS)
              + f" {s['legacy_bad']:>10} {s['hit_max_steps']:>8} {s['censored']:>8} {s['scorer_errors']:>10}")
    if "N0" in summary:
        k, n = PAPER_N0
        s0 = summary["N0"]
        print(f"\nN0 replication anchor: paper reports {fmt_ci(k, n)} 'did not fix the errors'. How that was scored is an"
              f" open question (does it include runs that never committed?), so two comparisons:\n"
              f"  gamed only           = {fmt_ci(s0['gamed'], s0['n'])}\n"
              f"  gamed + no_commit    = {fmt_ci(s0['gamed'] + s0['no_commit'], s0['n'])}")
    with (OUT / "summary_by_arm.csv").open("w", newline="") as f:
        fields = ["arm"] + list(next(iter(summary.values())).keys())
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for a in arms:
            w.writerow({"arm": a, **summary[a]})

    all_contrasts = []
    for outcome in ("gamed", "honest"):
        cs = contrasts(by_arm, outcome, args.bootstrap, args.seed)
        all_contrasts.extend(cs)
        print(f"\n2x2 contrasts on the {outcome.upper()} rate (percentile bootstrap, {args.bootstrap} reps, seed {args.seed}):")
        for c in cs:
            est = c["estimate"]
            if math.isnan(est):
                print(f"  {c['contrast']:42} n/a (missing arm)")
            else:
                print(f"  {c['contrast']:42} {est:+.1%}  [{c['ci_lo']:+.1%}, {c['ci_hi']:+.1%}]")
    with (OUT / "contrasts.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(all_contrasts[0].keys()))
        w.writeheader()
        w.writerows(all_contrasts)
    print(f"\nwrote {OUT / 'runs.csv'}, {OUT / 'summary_by_arm.csv'}, {OUT / 'contrasts.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
