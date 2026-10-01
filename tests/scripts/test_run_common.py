"""scripts/run_common.py: config resolution, the results layout and the build inputs both runners share."""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import run_common  # noqa: E402


@pytest.fixture()
def config(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text(
        "environment: precommit_hook\n"
        "modal:\n  isolated: true\n  cpu: 2.0\n  memory: 4096\n"
        "agent:\n  provider: fireworks\n  model: accounts/fireworks/models/deepseek-v4-pro\n  max_steps: 99999\n"
        "task:\n  variation: no_test_no_submit\n  speedup_target: 2.5\n  visual_test: false\n"
        "prompts:\n  user_prompt: 'bar is ${task.speedup_target}x'\n"
        "condition:\n  id: G1\n  framing: graded\n  oversight: fixed\n"
    )
    return p


def test_parse_overrides_accepts_strings_and_lists():
    assert run_common.parse_overrides("a=1, b.c=x", ["d=2"], None, "") == ["a=1", "b.c=x", "d=2"]


def test_resolution_applies_overrides_and_drops_the_modal_block(config):
    cfg = run_common.load_config(config, ["task.speedup_target=3", "agent.model=claude-sonnet-5"])
    sections = run_common.resolved_sections(cfg)
    assert set(sections) == {"environment", "agent", "task", "prompts", "condition"}
    assert sections["condition"] == {"id": "G1", "framing": "graded", "oversight": "fixed"}
    assert sections["task"]["speedup_target"] == 3
    assert sections["prompts"]["user_prompt"] == "bar is 3x"  # interpolation resolved
    assert run_common.modal_block(cfg) == {"isolated": True, "cpu": 2.0, "memory": 4096}
    assert "modal:" not in run_common.resolve_config(cfg)


def test_experiment_dir_layout(config):
    sections = run_common.resolved_sections(run_common.load_config(config))
    assert run_common.experiment_dir(sections, "T") == "precommit_hook/no_test_no_submit/accounts-fireworks-models-deepseek-v4-pro/T"
    sections["task"].pop("variation")
    sections["agent"]["model"] = "accounts/fireworks/models/kimi-k3"
    assert run_common.experiment_dir(sections, "T") == "precommit_hook/accounts-fireworks-models-kimi-k3/T"


def test_knob_files_are_written_only_when_present(tmp_path, monkeypatch):
    monkeypatch.setattr(run_common, "REPO_ROOT", tmp_path)
    env_dir = tmp_path / "environments" / "some_env"
    env_dir.mkdir(parents=True)
    (env_dir / "harness_leak").write_text("none\n")
    assert run_common.build_inputs("some_env", {}) == {"harness_leak": "none\n"}
    assert run_common.build_inputs("some_env", {"harness_leak": "open", "tell": "x"}) == {"harness_leak": "open\n"}
    assert run_common.build_inputs("other_env", {"harness_leak": "open"}) == {}
    assert run_common.build_key({}) == "base"


def test_prepare_build_context_rewrites_only_changed_files(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(run_common, "REPO_ROOT", tmp_path)
    ctx = tmp_path
    env_dir = ctx / "environments" / "grade_your_own"
    env_dir.mkdir(parents=True)
    (env_dir / "harness_leak").write_text("none\n")
    before = (env_dir / "harness_leak").stat().st_mtime_ns
    assert run_common.prepare_build_context(ctx, "grade_your_own", {}) == {"harness_leak": "none\n"}
    assert (env_dir / "harness_leak").stat().st_mtime_ns == before  # unchanged content: not rewritten
    run_common.prepare_build_context(ctx, "grade_your_own", {"harness_leak": "open"})
    assert (env_dir / "harness_leak").read_text() == "open\n"
    assert "[build-input] grade_your_own/harness_leak = open" in capsys.readouterr().out


def test_judge_command_uses_the_registry():
    cmd = run_common.judge_command("lucky_seed", "/results/x", {"lucky_seed": "environments/lucky_seed/grade_rollouts.py"})
    assert cmd[1].endswith("environments/lucky_seed/grade_rollouts.py") and cmd[2] == "/results/x"
    assert run_common.judge_command("secret_number", "/results/x", {}) is None


def test_condition_block_is_optional():
    cfg = run_common.load_config(__import__("pathlib").Path(__file__).resolve().parents[2]
                                 / "configs" / "precommit_hook" / "upstream" / "default.yaml")
    sections = run_common.resolved_sections(cfg)
    assert "condition" not in sections
