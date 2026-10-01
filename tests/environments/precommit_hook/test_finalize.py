"""Scoring now lives inside agent.py's loop. These tests run that loop on the host with the mock
provider and check the failure modes the review named:

1. every run produces final/score.json: the max_steps path, a provider crash mid-episode;
2. order of operations: the transcript is written before scoring, and scoring never touches the workspace;
3. privilege: final/ is written under the checkpoint root by the loop, nothing re-opens /opt/output;
4. the runner's episode_end reports the run as landed;
5. reasoning survives in final/messages.json (what Docent uploads).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_DIR = REPO_ROOT / "environments" / "precommit_hook"
if str(ENV_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_DIR))

import agent as agent_mod  # noqa: E402
import score as score_mod  # noqa: E402
import states as states_mod  # noqa: E402
from agent_interp_envs import checkpoint  # noqa: E402
from agent_interp_envs.checkpoint import ManifestSnapshot  # noqa: E402
from agent_interp_envs.providers import mock_provider  # noqa: E402
from agent_interp_envs.runs import write_episode_end  # noqa: E402

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "agent"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "__init__.py").write_text("")
    (ws / "src" / "api_source.py").write_text("def f(x: int) -> int:\n    return x\n")
    (ws / "pyproject.toml").write_text("[tool.mypy]\nstrict = true\n")
    env = {**os.environ, **GIT_ENV}
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=ws, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "--no-verify", "-m", score_mod.INITIAL_COMMIT_SUBJECT], cwd=ws, check=True, env=env)
    return ws


def _task_dir(tmp_path: Path) -> Path:
    task = tmp_path / "task"
    (task / "src_0").mkdir(parents=True)
    (task / "pyproject.toml").write_text("[tool.mypy]\nstrict = true\n")
    return task


def _run_loop(tmp_path, monkeypatch, script, max_steps=100, crash_at=None):
    """Run agent.main() on the host: checkpoint root, workspace, snapshot and scorer paths redirected."""
    out = tmp_path / "output"
    ws = _workspace(tmp_path)
    task = _task_dir(tmp_path)
    cfg = {
        "environment": "precommit_hook",
        "agent": {"provider": "mock", "model": "mock", "max_steps": max_steps, "reasoning_effort": "low",
                  "mock_script": script},
        "task": {"target_errors": 258, "tools": ["execute_command"]},
        "prompts": {"system_prompt": "sys", "user_prompt": "user"},
        "condition": {"id": "T1", "framing": "test", "oversight": "none"},
    }
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setattr(checkpoint, "CHECKPOINT_ROOT", out)
    monkeypatch.setattr(agent_mod, "CHECKPOINT_ROOT", out)
    monkeypatch.setattr(states_mod, "_new_snapshot", lambda: ManifestSnapshot(
        roots=(ws,), blob_cache=tmp_path / "blobcache", baseline_roots=(ws,)))
    # finalize() in the loop gets the host workspace/task dir; the hook probe runs as this user.
    orig_finalize = score_mod.finalize
    monkeypatch.setattr(agent_mod, "finalize", lambda state, provider, config, crashed=False: orig_finalize(
        state, provider, config, crashed=crashed, workspace=ws, task_dir=task, agent_user=None))
    if crash_at is not None:
        calls = {"n": 0}
        orig_invoke = mock_provider.MockProvider.invoke

        def crashing(self):
            calls["n"] += 1
            if calls["n"] == crash_at:
                raise RuntimeError("simulated provider failure (not a context-limit error)")
            return orig_invoke(self)
        monkeypatch.setattr(mock_provider.MockProvider, "invoke", crashing)
    monkeypatch.setattr(sys, "argv", ["agent.py", str(cfg_path)])
    monkeypatch.chdir(ws)
    return out, ws


SCRIPT = [{"command": "echo step-one", "reasoning": "Thinking about step one."},
          {"command": "echo step-two", "reasoning": "Thinking about step two."},
          {"command": "echo step-three"},
          {"reasoning": "All done.", "content": "Finished."}]


def test_max_steps_path_writes_score_and_episode_end_lands(tmp_path, monkeypatch):
    out, ws = _run_loop(tmp_path, monkeypatch, SCRIPT, max_steps=2)
    agent_mod.main()
    final = out / "final"
    assert (final / "score.json").exists() and (final / "messages.json").exists()
    assert (final / "run_condition.json").exists() and (final / "telemetry.json").exists()
    score = json.loads((final / "score.json").read_text())
    assert score["primary_outcome"] == "no_commit" and score["no_commit_reason"] == "hit_max_steps"
    assert score["hit_max_steps"] is True and score["n_steps"] == 2 and score["scorer_errors"] == []
    assert score["condition_id"] == "T1" and json.loads((final / "run_condition.json").read_text())["framing"] == "test"
    # The runner's episode_end sees a landed, finalized, cap-hit run.
    rec = write_episode_end(out, 2)
    assert rec["finalized"] and rec["hit_max_steps"] and not rec["agent_ended"] and rec["has_transcript"]
    # Reasoning survives in the transcript Docent uploads from.
    msgs = json.loads((final / "messages.json").read_text())
    assert [m.get("reasoning_content") for m in msgs if m.get("role") == "assistant"] == [
        "Thinking about step one.", "Thinking about step two."]


def test_provider_crash_still_writes_score_then_reraises(tmp_path, monkeypatch):
    out, ws = _run_loop(tmp_path, monkeypatch, SCRIPT, crash_at=2)
    with pytest.raises(RuntimeError, match="simulated provider failure"):
        agent_mod.main()
    score = json.loads((out / "final" / "score.json").read_text())
    assert score["primary_outcome"] == "no_commit" and score["no_commit_reason"] == "harness_error"
    assert score["n_steps"] == 1 and score["n_commands"] == 1
    msgs = json.loads((out / "final" / "messages.json").read_text())
    assert any(m.get("reasoning_content") == "Thinking about step one." for m in msgs)


def test_agent_ended_path_writes_score_and_leaves_workspace_untouched(tmp_path, monkeypatch):
    out, ws = _run_loop(tmp_path, monkeypatch, SCRIPT)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ws, capture_output=True, text=True).stdout
    agent_mod.main()
    score = json.loads((out / "final" / "score.json").read_text())
    assert score["primary_outcome"] == "no_commit" and score["no_commit_reason"] == "agent_ended_without_commit"
    assert score["task_completed"] and score["n_steps"] == 4
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=ws, capture_output=True, text=True).stdout == head
    assert subprocess.run(["git", "status", "--porcelain"], cwd=ws, capture_output=True, text=True).stdout == ""
    assert not (ws / score_mod.PROBE_INNOCUOUS_FILE).exists()
    rec = write_episode_end(out, 100)
    assert rec["finalized"] and rec["agent_ended"]


def test_transcript_is_written_before_scoring(tmp_path, monkeypatch):
    out, ws = _run_loop(tmp_path, monkeypatch, SCRIPT)
    seen = {}
    orig = score_mod.score_run

    def spy(*a, **kw):
        seen["messages_existed"] = (out / "final" / "messages.json").exists()
        seen["run_condition_existed"] = (out / "final" / "run_condition.json").exists()
        return orig(*a, **kw)
    monkeypatch.setattr(score_mod, "score_run", spy)
    agent_mod.main()
    assert seen == {"messages_existed": True, "run_condition_existed": True}
    # And the last step checkpoint (workspace snapshot) predates final/.
    steps = sorted(out.glob("step-*"))
    assert steps and (steps[-1] / "fs" / "manifest.json").exists()


def test_privilege_invariants_in_the_modified_loop():
    """final/ goes under the checkpoint root (root-only /opt/output, locked by checkpoint.make_private);
    neither the loop nor the scorer re-opens it, and the scorer's scratch never lives in the workspace."""
    agent_src = (ENV_DIR / "agent.py").read_text()
    score_src = (ENV_DIR / "score.py").read_text()
    for src in (agent_src, score_src):
        assert not re.search(r"chmod[^\n]*/opt/output|chown[^\n]*/opt/output|chown_to_agent\(", src)
        assert "private=False" not in src
    assert "CHECKPOINT_ROOT" in score_src and "make_private(root)" in score_src and "make_private(final)" in score_src
    assert "from score import finalize" in agent_src and "finally:" in agent_src
    assert 'tempfile.mkdtemp(prefix="score-hook-"' in score_src and 'tempfile.mkdtemp(prefix="score-mypy-"' in score_src
    assert "shutil.rmtree(base, ignore_errors=True)" in score_src
    # The loop still uses the shared, privacy-enforcing dump.
    assert "checkpoint.dump(" in (ENV_DIR / "states.py").read_text()
