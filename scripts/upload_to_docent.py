#!/usr/bin/env python3
"""Upload finished rollouts to Docent (one AgentRun per rollout, one transcript each).

    python scripts/upload_to_docent.py results/G1 [results/U1 ...]          # upload to DOCENT_COLLECTION_ID
    python scripts/upload_to_docent.py results/smoke --dry-run --print-sample 1
    python scripts/upload_to_docent.py results/G1 --create-collection "grader-framing-2x2"

Inputs are any mix of run dirs, fleet dirs or results roots; every directory holding
final/score.json is a rollout. Reads DOCENT_API_KEY and DOCENT_COLLECTION_ID from .env
(python-dotenv) or the environment. Idempotent: uploaded runs are recorded in
results/docent_upload_manifest.json and skipped next time (--force re-uploads).

Transcript mapping (Fireworks chat format -> Docent data models, docent==0.1.87):
  system/user            -> SystemMessage / UserMessage(content=str)
  assistant              -> AssistantMessage(content=[ContentReasoning(reasoning=reasoning_content),
                                                      ContentText(text=content)], tool_calls=[ToolCall(...)])
  tool                   -> ToolMessage(content=str, tool_call_id=..., function=<name of that call>)
Reasoning is mapped to the SDK's reasoning content type so Docent renders it separately from the
visible response. Nothing here triggers Docent LLM features (no rubrics, judges or search).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "analysis"))
from common import find_run_dirs, grader_term_hits, grader_terms_in_text, load_run, upstream_sha  # noqa: E402

from docent.data_models import AgentRun, Transcript  # noqa: E402
from docent.data_models.chat import (  # noqa: E402
    AssistantMessage,
    ContentReasoning,
    ContentText,
    SystemMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
    check_agent_runs,
    format_check_report,
)

MANIFEST = REPO_ROOT / "results" / "docent_upload_manifest.json"
MAX_TEXT_META = 4000


# ============================================================
# Transcript conversion
# ============================================================

def _tool_arguments(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except (ValueError, TypeError):
        return {"raw_arguments": str(raw)}


def convert_messages(messages: list[dict]) -> list:
    out = []
    call_names: dict[str, str] = {}
    for m in messages:
        role = m.get("role")
        if role == "system":
            out.append(SystemMessage(content=str(m.get("content") or "")))
        elif role == "user":
            out.append(UserMessage(content=str(m.get("content") or "")))
        elif role == "assistant":
            blocks = []
            reasoning = m.get("reasoning_content") or m.get("reasoning")
            if reasoning:
                blocks.append(ContentReasoning(reasoning=str(reasoning)))
            if m.get("content"):
                blocks.append(ContentText(text=str(m["content"])))
            tool_calls = []
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                call_names[tc.get("id", "")] = fn.get("name", "")
                tool_calls.append(ToolCall(id=str(tc.get("id", "")), function=str(fn.get("name", "")),
                                           arguments=_tool_arguments(fn.get("arguments")), type="function"))
            out.append(AssistantMessage(content=blocks if blocks else "", tool_calls=tool_calls or None))
        elif role == "tool":
            cid = str(m.get("tool_call_id") or "")
            out.append(ToolMessage(content=str(m.get("content") or ""), tool_call_id=cid or None,
                                   function=call_names.get(cid)))
        else:
            out.append(UserMessage(content=f"[{role}] {m.get('content')}"))
    return out


# ============================================================
# Metadata
# ============================================================

_SKIP_SCORE_KEYS = {"hook_text", "command_subjects"}


def flatten_score(score: dict) -> dict:
    """score.json flattened one level: scalars and lists stay as they are; nested dicts become
    <key>_<sub>; long text is truncated; lists of dicts are JSON-encoded."""
    flat: dict = {}

    def put(k: str, v):
        if isinstance(v, str) and len(v) > MAX_TEXT_META:
            v = v[:MAX_TEXT_META] + "…"
        if isinstance(v, list) and v and isinstance(v[0], dict):
            v = json.dumps(v)[:MAX_TEXT_META]
        flat[k] = v

    for k, v in score.items():
        if k in _SKIP_SCORE_KEYS:
            continue
        if isinstance(v, dict):
            for sk, sv in v.items():
                if isinstance(sv, dict):
                    put(f"{k}_{sk}", json.dumps(sv)[:MAX_TEXT_META])
                else:
                    put(f"{k}_{sk}", sv)
        else:
            put(k, v)
    if score.get("hook_text"):
        put("hook_text", score["hook_text"])
    flat["workaround_labels_str"] = ",".join(score.get("workaround_labels") or []) or "none"
    return flat


def build_agent_run(run: dict, sha: str | None) -> AgentRun:
    score, cond, messages = run["score"], run["condition"], run["messages"]
    run_dir: Path = run["run_dir"]
    try:
        rel = str(run_dir.relative_to(REPO_ROOT))
    except ValueError:
        rel = str(run_dir)
    hits, per_term = grader_term_hits(messages)
    user_prompt = next((m.get("content") for m in messages if m.get("role") == "user"), "") or ""
    cfg = cond.get("config") or {}
    meta = {
        "condition_id": cond.get("condition_id"),
        "framing": cond.get("framing"),
        "oversight": cond.get("oversight"),
        "model": cond.get("model"),
        "provider": cond.get("provider"),
        "reasoning_effort": cond.get("reasoning_effort"),
        "max_steps": cond.get("max_steps"),
        "target_errors": cond.get("target_errors"),
        "upstream_sha": sha,
        "config_hash": cond.get("config_hash") or score.get("config_hash"),
        "run_dir": rel,
        "n_steps": score.get("n_steps"),
        "n_assistant_turns": sum(1 for m in messages if m.get("role") == "assistant"),
        "grader_term_hits": hits,
        "grader_term_counts": json.dumps(per_term),
        "grader_term_hits_note": ("Regex count over assistant reasoning_content only (terms: grader, grade, graded, "
                                  "score, scored, evaluat*, benchmark, test suite, checker, reviewer, reviewed). "
                                  "The G arms' PROMPTS contain several of these words, so compare across arms, "
                                  "not against zero."),
        "grader_terms_in_user_prompt": grader_terms_in_text(str(user_prompt)),
        "agent_ended": (run.get("episode_end") or {}).get("agent_ended"),
        "hit_max_steps_episode_end": (run.get("episode_end") or {}).get("hit_max_steps"),
    }
    meta.update(flatten_score(score))
    meta = {k: v for k, v in meta.items() if v is not None}
    name = f"{cond.get('condition_id') or '?'} {run_dir.parent.name}/{run_dir.name} — {score.get('primary_outcome')}"
    desc = (f"framing={cond.get('framing')} oversight={cond.get('oversight')} outcome={score.get('primary_outcome')} "
            f"labels={score.get('workaround_labels')} steps={score.get('n_steps')} "
            f"unsuppressed_errors={score.get('mypy_errors_committed_unsuppressed')}")
    transcript = Transcript(messages=convert_messages(messages),
                            metadata={"source": "final/messages.json", "run_dir": rel,
                                      "system_prompt_sha_prefix": (cfg.get("prompts") or {}).get("system_prompt", "")[:0]})
    return AgentRun(name=name, description=desc, transcripts=[transcript], metadata=meta)


# ============================================================
# Manifest (idempotency)
# ============================================================

def load_manifest() -> dict:
    if MANIFEST.is_file():
        try:
            return json.loads(MANIFEST.read_text())
        except ValueError:
            pass
    return {"uploads": {}}


def save_manifest(m: dict) -> None:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(m, indent=1, sort_keys=True))
    os.replace(tmp, MANIFEST)


def run_key(run: dict) -> str:
    try:
        rel = str(run["run_dir"].relative_to(REPO_ROOT))
    except ValueError:
        rel = str(run["run_dir"])
    return f"{rel}@{(run['condition'].get('config_hash') or run['score'].get('config_hash') or 'nohash')[:16]}"


# ============================================================
# Sample printing
# ============================================================

def print_sample(agent_run: AgentRun, max_messages: int = 8) -> None:
    print("=" * 80)
    print(f"AgentRun name: {agent_run.name}")
    print(f"description:   {agent_run.description}")
    print("metadata (selected):")
    for k in ("condition_id", "framing", "oversight", "model", "reasoning_effort", "max_steps", "target_errors",
              "upstream_sha", "config_hash", "run_dir", "primary_outcome", "workaround_labels", "no_commit_reason",
              "mypy_errors_committed_raw", "mypy_errors_committed_unsuppressed", "hook_behavioral_check_hook_blocks_unstaged_error",
              "n_steps", "grader_term_hits", "grader_term_counts", "legacy_score_outcome"):
        if k in agent_run.metadata:
            print(f"  {k}: {agent_run.metadata[k]!r}")
    print(f"  ... {len(agent_run.metadata)} metadata keys total")
    t = agent_run.transcripts[0]
    print(f"transcript: {len(t.messages)} messages")
    for i, m in enumerate(t.messages[:max_messages]):
        line = f"  [{i}] {m.role}"
        if m.role == "assistant":
            blocks = m.content if isinstance(m.content, list) else [ContentText(text=m.content)]
            for b in blocks:
                if b.type == "reasoning":
                    line += f"\n       reasoning: {b.reasoning[:120]!r}"
                elif b.type == "text" and b.text:
                    line += f"\n       text: {b.text[:120]!r}"
            for tc in m.tool_calls or []:
                line += f"\n       tool_call {tc.function}({json.dumps(tc.arguments)[:100]})"
        elif m.role == "tool":
            line += f" (for {m.function}, id={m.tool_call_id}) {str(m.content)[:100]!r}"
        else:
            line += f" {str(m.content)[:100]!r}"
        print(line)
    print("=" * 80)


# ============================================================
# Main
# ============================================================

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", help="run dirs, fleet dirs or results roots")
    ap.add_argument("--dry-run", action="store_true", help="build and validate AgentRuns; upload nothing")
    ap.add_argument("--create-collection", metavar="NAME", help="create a new collection with this name and upload there")
    ap.add_argument("--collection-id", default=None, help="override DOCENT_COLLECTION_ID")
    ap.add_argument("--print-sample", type=int, default=0, metavar="N", help="print N converted runs in full-ish")
    ap.add_argument("--force", action="store_true", help="re-upload runs already in the manifest")
    ap.add_argument("--env-file", default=str(REPO_ROOT / ".env"))
    args = ap.parse_args(argv)

    from dotenv import load_dotenv
    load_dotenv(args.env_file)

    run_dirs = find_run_dirs(args.paths)
    if not run_dirs:
        print("no runs with final/score.json found under", args.paths, file=sys.stderr)
        return 2
    sha = upstream_sha()
    runs = [load_run(d) for d in run_dirs]
    manifest = load_manifest()
    agent_runs: list[tuple[dict, AgentRun]] = []
    skipped = 0
    for run in runs:
        key = run_key(run)
        if not args.force and key in manifest["uploads"] and not args.dry_run and not args.create_collection:
            skipped += 1
            continue
        agent_runs.append((run, build_agent_run(run, sha)))
    print(f"runs found: {len(runs)}  to upload: {len(agent_runs)}  already uploaded (manifest): {skipped}")
    if not agent_runs:
        return 0

    report = check_agent_runs([ar for _, ar in agent_runs])
    print(format_check_report(report))
    for _, ar in agent_runs[: args.print_sample]:
        print_sample(ar)
    # Every run must JSON-serialize (what the API receives).
    for _, ar in agent_runs:
        ar.model_dump_json()

    if args.dry_run:
        by_outcome: dict[str, int] = {}
        for _, ar in agent_runs:
            by_outcome[ar.metadata.get("primary_outcome")] = by_outcome.get(ar.metadata.get("primary_outcome"), 0) + 1
        print(f"dry run: {len(agent_runs)} valid AgentRun objects built; outcomes {by_outcome}; nothing uploaded")
        return 0

    api_key = os.getenv("DOCENT_API_KEY")
    if not api_key:
        print("DOCENT_API_KEY is not set (put it in .env); use --dry-run to validate without uploading", file=sys.stderr)
        return 2
    from docent import Docent
    client = Docent(api_key=api_key)
    collection_id = args.collection_id or os.getenv("DOCENT_COLLECTION_ID")
    if args.create_collection:
        collection_id = client.create_collection(name=args.create_collection,
                                                 description="precommit_hook grader-framing x oversight 2x2 rollouts")
        print(f"created collection {collection_id!r} named {args.create_collection!r}")
    if not collection_id:
        print("no collection: set DOCENT_COLLECTION_ID in .env, pass --collection-id, or --create-collection NAME",
              file=sys.stderr)
        return 2
    if not args.force:
        pending = [(r, ar) for r, ar in agent_runs
                   if manifest["uploads"].get(run_key(r), {}).get("collection_id") != collection_id]
    else:
        pending = agent_runs
    if not pending:
        print("every run is already in this collection (manifest); nothing to do")
        return 0
    print(f"uploading {len(pending)} run(s) to collection {collection_id} ...", flush=True)
    result = client.add_agent_runs(collection_id, [ar for _, ar in pending])
    print("add_agent_runs ->", json.dumps(result, default=str)[:500])
    for r, ar in pending:
        manifest["uploads"][run_key(r)] = {"collection_id": collection_id, "agent_run_id": ar.id,
                                           "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    save_manifest(manifest)
    print(f"recorded {len(pending)} upload(s) in {MANIFEST.relative_to(REPO_ROOT)}")
    print(f"collection id: {collection_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
