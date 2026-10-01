"""The five experimental arms differ ONLY in prompts.user_prompt and the condition block."""

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CFG = REPO_ROOT / "configs" / "precommit_hook"
ARM_FILES = ["N0_replication.yaml", "G_noO.yaml", "G_O.yaml", "U_noO.yaml", "U_O.yaml"]

spec = importlib.util.spec_from_file_location("build_arms", CFG / "build_arms.py")
build_arms = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_arms)


def _strip_varying(text: str) -> str:
    """Drop the comment header, the user_prompt block scalar and the condition block."""
    text = "\n".join(l for l in text.splitlines() if not l.startswith("#")) + "\n"
    text = re.sub(r"  user_prompt: \|\n(?:(?:    .*|)\n)*", "", text)
    text = re.sub(r"condition:\n(?:  .*\n)*", "", text)
    return text


def test_generated_files_match_the_generator():
    for name in ARM_FILES:
        assert (CFG / name).read_text() == build_arms.render(name[:-5]), f"{name} is stale: run build_arms.py"


def test_arms_are_byte_identical_outside_user_prompt_and_condition():
    texts = {n: _strip_varying((CFG / n).read_text()) for n in ARM_FILES}
    base = texts[ARM_FILES[0]]
    assert "user_prompt" not in base and "condition" not in base
    assert "system_prompt" in base and "reasoning_effort: low" in base and "max_steps: 100" in base
    for n, t in texts.items():
        assert t == base, f"{n} differs from {ARM_FILES[0]} outside user_prompt/condition"


def test_arms_parse_identically_outside_user_prompt_and_condition():
    dicts = {n: yaml.safe_load((CFG / n).read_text()) for n in ARM_FILES}
    for d in dicts.values():
        d["prompts"].pop("user_prompt")
        d.pop("condition")
    base = dicts[ARM_FILES[0]]
    assert all(d == base for d in dicts.values())
    assert base["task"]["target_errors"] == 258 and base["agent"]["provider"] == "fireworks"


def test_prompt_building_blocks_are_used_verbatim():
    p = {n: yaml.safe_load((CFG / n).read_text()) for n in ARM_FILES}
    up = {n: p[n]["prompts"]["user_prompt"] for n in ARM_FILES}
    upstream = yaml.safe_load((CFG / "upstream" / "default.yaml").read_text())
    assert up["N0_replication.yaml"] == upstream["prompts"]["user_prompt"]
    assert p["N0_replication.yaml"]["prompts"]["system_prompt"] == upstream["prompts"]["system_prompt"]
    for n in ARM_FILES[1:]:
        assert build_arms.CORE in up[n]
        assert up[n].count(build_arms.CORE) == 1
    for n in ("G_O.yaml", "U_O.yaml"):
        assert build_arms.OVERSIGHT in up[n]
    for n in ("G_noO.yaml", "U_noO.yaml"):
        assert build_arms.OVERSIGHT not in up[n]
    for n in ("G_noO.yaml", "G_O.yaml"):
        assert up[n].startswith(build_arms.G_PRE) and up[n].rstrip("\n").endswith(build_arms.G_POST)
    for n in ("U_noO.yaml", "U_O.yaml"):
        assert up[n].startswith(build_arms.U_PRE) and up[n].rstrip("\n").endswith(build_arms.U_POST)
    assert up["G_O.yaml"] == f"{build_arms.G_PRE}\n\n{build_arms.CORE}\n\n{build_arms.OVERSIGHT}\n\n{build_arms.G_POST}\n"
    assert up["U_O.yaml"] == f"{build_arms.U_PRE}\n\n{build_arms.CORE}\n\n{build_arms.OVERSIGHT}\n\n{build_arms.U_POST}\n"
    # CORE is the paper prompt minus its stakes/deployment preamble
    assert build_arms.CORE in upstream["prompts"]["user_prompt"]
    ids = [p[n]["condition"]["id"] for n in ARM_FILES]
    assert ids == ["N0", "G0", "G1", "U0", "U1"]


def test_wrapper_lengths_within_15_percent():
    c = build_arms.word_counts()
    g, u = c["G_wrapper"], c["U_wrapper"]
    assert abs(g - u) / max(g, u) <= 0.15, c


def test_runner_resolves_every_arm_and_keeps_the_condition_block():
    for n in ARM_FILES:
        out = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "run.py"), str(CFG / n), "--dry-run",
                              "--results-dir", "/tmp/arms-dry-run"], capture_output=True, text=True, cwd=REPO_ROOT)
        assert out.returncode == 0, out.stdout + out.stderr
        resolved = out.stdout.split("--- resolved config (/opt/config.yaml) ---")[1].split("--- build inputs ---")[0]
        cfg = yaml.safe_load(resolved)
        src = yaml.safe_load((CFG / n).read_text())
        assert cfg["condition"]["id"] == src["condition"]["id"]
        assert cfg["prompts"]["user_prompt"] == src["prompts"]["user_prompt"]
        assert cfg["agent"]["reasoning_effort"] == "low"


def test_reasoning_effort_reaches_the_fireworks_request_body(monkeypatch):
    """agent.reasoning_effort -> provider_kwargs -> FireworksProvider -> extra_body.reasoning_effort."""
    monkeypatch.setenv("FIREWORKS_API_KEY", "test-key-not-real")
    monkeypatch.delenv("ROLLOUT_GATEWAY", raising=False)
    from agent_interp_envs.checkpoint import provider_kwargs
    from agent_interp_envs.providers.fireworks_provider import FireworksProvider
    from agent_interp_envs.tool_calling import EXECUTE_COMMAND_TOOL

    cfg = yaml.safe_load((CFG / "G_O.yaml").read_text())
    kwargs = provider_kwargs(cfg)
    assert kwargs["reasoning_effort"] == "low" and "condition" not in kwargs and "max_steps" not in kwargs
    provider = FireworksProvider(model=kwargs["model"], messages=[], tools=[EXECUTE_COMMAND_TOOL],
                                 reasoning_effort=kwargs["reasoning_effort"])
    assert provider.kwargs["extra_body"] == {"reasoning_effort": "low"}
    assert provider.kwargs["tools"] == [EXECUTE_COMMAND_TOOL]
    # The provider default when unset is "low" (what the paper's run would have sent if unset).
    assert FireworksProvider(model="m", messages=[], tools=[]).kwargs["extra_body"] == {"reasoning_effort": "low"}


def test_smoke_configs_share_the_arm_task_block_and_carry_expectations():
    arm = yaml.safe_load((CFG / "G_O.yaml").read_text())
    smokes = sorted((CFG / "smoke").glob("*.yaml"))
    assert len(smokes) >= 8
    for s in smokes:
        cfg = yaml.safe_load(s.read_text())
        assert cfg["agent"]["provider"] == "mock" and cfg["agent"]["mock_script"]
        task = dict(cfg["task"])
        assert task.pop("target_errors") in (0, 258)
        assert task == {k: v for k, v in arm["task"].items() if k != "target_errors"}
        assert cfg["condition"]["expected"]["primary_outcome"] in ("honest", "gamed", "no_commit")
