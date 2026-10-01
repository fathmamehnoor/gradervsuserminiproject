# Upstream provenance

Source: https://github.com/orvelte/agent-interp-envs (branch `main`), a fork of
https://github.com/gkroiz/agent-interp-envs (MIT, © 2026 Aditya Singh, Gerson Kroiz; see LICENSE).

Commit: `310c03fbafdbb16df9ee043a7d9dd18a6d8cd3ec` (cloned 2026-10-01).

## Copied verbatim (then edited where CHANGELOG.md says so)

| Upstream path | Here | Notes |
|---|---|---|
| `environments/precommit_hook/` (Dockerfile, README.md, agent.py, apply_patch.py, entrypoint.py, generate_variants.py, pyproject.toml, run_step.py, score.py, states.py, tools.py, src_0/, src_258/, src_602/) | same | agent.py, score.py, Dockerfile edited (see CHANGELOG) |
| `src/agent_interp_envs/{__init__,checkpoint,config,cot_judge,episode_end,print_helpers,runs,tool_calling,types}.py`, `py.typed` | same | unchanged |
| `src/agent_interp_envs/providers/{__init__,_keys,base,deepseek_v4_template,fireworks_completions_provider,fireworks_provider,mock_provider,openrouter_provider,rejection_provider}.py` | same | `__init__.py` registry pruned; `mock_provider.py` extended (scripted reasoning) |
| `pyproject.toml`, `uv.lock` | same | deps pruned, Modal moved to its own group, Docent pinned; lock regenerated |
| `LICENSE`, `.dockerignore`, `.python-version` | same | unchanged |
| `configs/precommit_hook/default.yaml`, `configs/precommit_hook/precommit_hook.yaml` | `configs/precommit_hook/upstream/` | reference copies, unchanged |
| `scripts/{run,run_common,env_registry,run_modal,resume,resume_modal,smoke_modal,gateway_sidecar}.py` | same | registry/runner trimmed to this environment; `condition` block passthrough added |
| `tests/environments/{test_privilege_separation,test_provider_kwargs_forwarding,test_workspace_is_agent_readable}.py` | same | lists trimmed to this environment |
| `tests/providers/test_deepseek_v4_template.py`, `tests/scripts/{test_env_registry,test_run_common}.py`, `tests/src/{test_checkpoint,test_context_limit_predicate,test_episode_end_cache,test_mock_provider,test_openrouter_provider_preferences,test_tool_output_cap}.py` | same | suite-specific assertions trimmed |

## Pruned (not copied), by tracing imports from the kept code

- every other environment under `environments/`, their configs under `configs/`, and their autoraters (`grade_rollouts.py`)
- `src/agent_interp_envs/grading.py` (Anthropic-based LLM autorater) and `src/agent_interp_envs/dataloaders/`
- `src/agent_interp_envs/runner.py` (shared loop; precommit_hook's agent.py has its own loop upstream and keeps it)
- providers: `anthropic_provider.py`, `openai_provider.py`, `minimax_provider.py`, `moonshot_provider.py`
- tests that only cover pruned code: `test_anthropic_prompt_cache.py`, `test_openai_provider.py`, `test_grading_cli.py`, `test_runner.py`, `test_interleaved_thinking.py` (live, imports every provider), the per-environment test configs/shell scripts
- `.github/`, `.claude/`, `.cache/`, upstream `README.md`/`CLAUDE.md`/`.env.template`/`.gitignore` (replaced)

Kept on purpose although not imported at module level: `rejection_provider.py` + `cot_judge.py`
(lazily imported by `create_provider` when `agent.rejection` is set) and
`fireworks_completions_provider.py`, for later resampling experiments.
