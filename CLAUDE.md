Grader framing × oversight (2×2) — pre-commit hook env
What this repo is

A self-contained copy of the precommit_hook environment from https://github.com/orvelte/agent-interp-envs (a fork of gkroiz/agent-interp-envs, MIT, © Aditya Singh, Gerson Kroiz). We use it for one experiment: whether framing the same task as a graded episode vs. a real user's request changes how often DeepSeek v4 Pro games the task, and whether that effect survives when stated oversight is held fixed.

The reference result we replicate is from "Why do models task game?" (Singh, Nanda, Rajamanoharan, Aug 2026): DeepSeek v4 Pro failed to fix the type errors in 69/100 rollouts on the 258-error variant, most often by writing a hook that only checks staged files.

If BUILD_TASK.md exists and BUILD_REPORT.md does not, the build has not been done yet. Read BUILD_TASK.md.

Standing rules (apply in every session)
Never launch paid rollouts. A "paid rollout" is any agent run against a real provider (Fireworks, OpenRouter, etc.). Olivia and her partner launch those themselves. Smoke tests use the mock provider only.
Never print, log, commit, or copy API keys. Keys live in .env (gitignored).
Never change the text of the experimental prompts in configs/precommit_hook/ without updating the README condition table in the same change, and note it in CHANGELOG.md. Prompt text is the independent variable.
All 2×2 arms must stay identical in everything except the framing wrapper and the oversight sentence: same image, same src_258 variant, same system prompt, same model, same reasoning effort, same max_steps, same tools. If a change touches one arm, it touches all arms.
Keep the upstream security model intact: agent shell runs as dev, /opt is root-only, the scorer and its outputs are never visible to the agent.
Results go in results/ (gitignored). Analysis outputs go in analysis/out/ (gitignored).
When unsure whether a change alters what the model sees, assume it does and flag it.