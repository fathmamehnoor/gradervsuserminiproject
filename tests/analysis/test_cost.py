"""analysis/cost.py: billed cost from OpenRouter lines, --price-* for Fireworks lines, per-arm totals."""

import csv
import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("cost", REPO_ROOT / "analysis" / "cost.py")
cost = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cost)

OR_LINE = ("[provider-usage] t=1 model=deepseek/deepseek-v4-pro-0813 in={i} cache_read={c} cache_write=0 out={o} "
           "rl_in_rem=None rl_out_rem=None reasoning=10 cost={cost} served_by=DeepSeek")
FW_LINE = ("[provider-usage] t=1 model=accounts/fireworks/models/x in={i} cache_read={c} cache_write=0 out={o} "
           "rl_in_rem=None rl_out_rem=None")


def _run(root: Path, arm: str, lines: list[str], condition_file: bool = True) -> Path:
    run = root / "results" / arm / "precommit_hook" / "m" / "ts" / "run-1"
    (run / "final").mkdir(parents=True)
    (run / "rollout.log").write_text("STEP 0\n" + "\n".join(lines) + "\nFINAL\n")
    if condition_file:
        (run / "final" / "run_condition.json").write_text(json.dumps({"condition_id": arm}))
    return run


def test_reported_cost_is_summed(tmp_path):
    calls = cost.parse_usage("\n".join([OR_LINE.format(i=1000, c=0, o=50, cost=0.002),
                                        OR_LINE.format(i=2000, c=1000, o=50, cost=0.001)]))
    assert cost.run_cost(calls, None) == (pytest.approx(0.003), "reported")


def test_fireworks_lines_need_prices():
    calls = cost.parse_usage(FW_LINE.format(i=1_000_000, c=600_000, o=100_000))
    assert cost.run_cost(calls, None) == (None, "unpriced")
    priced, how = cost.run_cost(calls, {"in": 1.0, "cached": 0.1, "out": 2.0})
    # 400k fresh * $1/M + 600k cached * $0.1/M + 100k out * $2/M
    assert how == "priced" and priced == pytest.approx(0.4 + 0.06 + 0.2)


def test_main_groups_by_arm_and_writes_csv(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cost, "OUT", tmp_path / "out")
    _run(tmp_path, "N0", [OR_LINE.format(i=1000, c=0, o=50, cost=0.01)])
    _run(tmp_path, "G0", [OR_LINE.format(i=1000, c=900, o=50, cost=0.03)], condition_file=False)
    (tmp_path / "results" / "smoke" / "x" / "run-1").mkdir(parents=True)
    (tmp_path / "results" / "smoke" / "x" / "run-1" / "rollout.log").write_text(OR_LINE.format(i=1, c=0, o=1, cost=9))

    assert cost.main([str(tmp_path / "results"), "--plan", "10"]) == 0
    out = capsys.readouterr().out
    assert "projection for 10 more runs: ~$0.20 at the mean, ~$0.30 at the max" in out
    rows = list(csv.DictReader(open(tmp_path / "out" / "cost_runs.csv")))
    assert sorted(r["arm"] for r in rows) == ["G0", "N0"]  # smoke excluded; G0 found from the path
    assert {r["arm"]: r["cached_share"] for r in rows} == {"N0": "0.0", "G0": "0.9"}


def test_partial_prices_rejected(tmp_path):
    with pytest.raises(SystemExit):
        cost.main([str(tmp_path), "--price-in", "1"])
