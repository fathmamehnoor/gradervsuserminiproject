#!/usr/bin/env bash
# Launch COUNT rollouts of one experimental arm locally in Docker.
#
#   scripts/launch_arm.sh ARM COUNT [--max-concurrent N] [--dry-run] [extra scripts/run.py args]
#
#   ARM   one of N0 G0 G1 U0 U1 (or a config stem: N0_replication, G_noO, G_O, U_noO, U_O)
#   COUNT number of rollouts
#
# Results go to results/<condition_id>/precommit_hook/<model>/<timestamp>/run-N/ and the
# matching Docent upload command is printed at the end. --dry-run resolves the config and
# prints the plan without building or launching anything (no cost).
#
# --max-concurrent defaults to 8. Rationale (from the code and comments in this repo):
# the Fireworks provider notes that "Fireworks limits are per-model and shared across every
# concurrent rollout on that model, so bursts are the norm on a fleet run", and retries a
# 429 for up to 30 minutes with jittered backoff; the completions provider notes sustained
# 429/503 bursts under concurrent fleets; upstream's own Modal example uses --max-concurrent 10.
# 8 keeps a pilot well inside that envelope; raise it if your account's limits allow.
#
# THIS SCRIPT LAUNCHES PAID ROLLOUTS. Olivia / her partner run it; the build agent never does.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

usage() { sed -n '2,20p' "$0"; exit 2; }
[ $# -ge 2 ] || usage
ARM="$1"; COUNT="$2"; shift 2

case "$ARM" in
  N0|N0_replication) CFG=configs/precommit_hook/N0_replication.yaml; CID=N0 ;;
  G0|G_noO)          CFG=configs/precommit_hook/G_noO.yaml;          CID=G0 ;;
  G1|G_O)            CFG=configs/precommit_hook/G_O.yaml;            CID=G1 ;;
  U0|U_noO)          CFG=configs/precommit_hook/U_noO.yaml;          CID=U0 ;;
  U1|U_O)            CFG=configs/precommit_hook/U_O.yaml;            CID=U1 ;;
  *) echo "unknown arm '$ARM' (N0 G0 G1 U0 U1)"; exit 2 ;;
esac

MAX_CONCURRENT=8
DRY_RUN=""
EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    --max-concurrent) MAX_CONCURRENT="$2"; shift 2 ;;
    --max-concurrent=*) MAX_CONCURRENT="${1#*=}"; shift ;;
    --dry-run) DRY_RUN="--dry-run"; shift ;;
    *) EXTRA+=("$1"); shift ;;
  esac
done

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x "$REPO_ROOT/.venv/bin/python" ]; then PY="$REPO_ROOT/.venv/bin/python"; else PY="python3"; fi
fi

RESULTS_DIR="$REPO_ROOT/results/$CID"
if [ -z "$DRY_RUN" ] && { [ ! -f .env ] || ! grep -q '^FIREWORKS_API_KEY=.\+' .env; }; then
  echo "WARNING: .env has no FIREWORKS_API_KEY; the rollouts will fail at provider construction." >&2
fi

echo "arm=$CID config=$CFG count=$COUNT max_concurrent=$MAX_CONCURRENT results=$RESULTS_DIR ${DRY_RUN:+(dry run)}"
"$PY" scripts/run.py "$CFG" --count "$COUNT" --max-concurrent "$MAX_CONCURRENT" --results-dir "$RESULTS_DIR" \
  --no-judge $DRY_RUN ${EXTRA[@]+"${EXTRA[@]}"}

echo
echo "Next:"
echo "  $PY scripts/upload_to_docent.py $RESULTS_DIR            # upload to DOCENT_COLLECTION_ID (idempotent)"
echo "  $PY analysis/summarize.py                               # per-arm table + 2x2 contrasts"
