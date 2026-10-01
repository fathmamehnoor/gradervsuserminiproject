#!/usr/bin/env python3
"""
Agent for the precommit_hook task.

Tests agent behavior in a realistic software engineering scenario where
shortcuts (--no-verify, chmod, staged-only hooks, blanket ignores, ...) are
available but the instructed approach requires fixing the type errors.

Changes from upstream (see UPSTREAM.md / CHANGELOG.md):
- the provider is built with ``checkpoint.provider_kwargs`` so every
  ``agent.*`` knob (reasoning_effort, mock_script, temperature, ...) reaches
  ``create_provider`` on the fresh path exactly as on the resume path;
- a context-window overflow censors the episode (final/censored.json) instead
  of crashing it, as ``agent_interp_envs.runner`` does;
- when the loop ends — by the agent stopping, by max_steps, by a censor or by
  an exception — ``score.finalize`` computes the ground-truth score from the
  real final repo state and writes final/{score.json, run_condition.json,
  telemetry.json, messages.json} under the root-only /opt/output.

Nothing the agent sees changes: same prompts, same tools, same shell.
"""

import json
import sys
from pathlib import Path

from dotenv import load_dotenv
from run_step import print_final_results, run_step
from score import finalize
from states import CodingAgentState, dump, init_fs_tracking, restore
from tools import get_tools

from agent_interp_envs.checkpoint import CHECKPOINT_ROOT, provider_kwargs, step_dir
from agent_interp_envs.config import load_config
from agent_interp_envs.print_helpers import print_step_header
from agent_interp_envs.providers import create_provider
from agent_interp_envs.runs import CENSOR_MARKER, is_context_limit_error

load_dotenv()


def main() -> None:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "/opt/config.yaml"
    config = load_config(config_path)

    state = None
    provider = None
    crashed = False
    try:
        if Path("/opt/checkpoint").exists():
            state, provider = restore(config, Path("/opt/checkpoint"))
            state.step += 1
        else:
            state = CodingAgentState()
            state.variant = config.get("task", {}).get("variation", "unknown")
            init_fs_tracking()

            prompts = config.get("prompts") or {}
            provider = create_provider(
                messages=[
                    {"role": "system", "content": prompts.get("system_prompt", "")},
                    {"role": "user", "content": prompts.get("user_prompt", "")},
                ],
                tools=get_tools(config),
                **provider_kwargs(config),
            )

        provider.print_history()

        max_steps = config.get("agent", {}).get("max_steps", 30)
        while state.step < max_steps:
            print_step_header(state.step)

            try:
                should_quit = run_step(provider, state, config)
            except Exception as exc:
                if not is_context_limit_error(exc):
                    raise
                # The model's context window overflowed: no final decision is
                # possible. Mark the run censored and let finalize record what is on disk.
                print("\nCONTEXT LIMIT REACHED — episode censored (no final decision)")
                final = CHECKPOINT_ROOT / "final"
                final.mkdir(parents=True, exist_ok=True)
                (final / CENSOR_MARKER).write_text(json.dumps(
                    {"step": state.step, "error": str(exc)[:500]}, indent=1) + "\n")
                dump(state, provider, step_dir(state.step))
                break
            dump(state, provider, step_dir(state.step))
            state.step += 1

            if should_quit:
                break

        if not state.task_completed and state.step >= max_steps:
            print_final_results(state, False)
    except BaseException:
        crashed = True
        raise
    finally:
        if state is not None and provider is not None:
            finalize(state, provider, config, crashed=crashed)


if __name__ == "__main__":
    main()
