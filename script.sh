#!/usr/bin/env bash
# Observathon end-to-end runner: selfcheck -> sim -> score -> summary.
#
#   ./script.sh                       # public phase (default)
#   ./script.sh --private             # private sim + score
#   ./script.sh --practice            # practice sim only (no scorer)
#   ./script.sh --private -c 12       # private, concurrency 12
#   ./script.sh --users 200 --turns 12
#
# Behaviour:
#   * Loads .env so OPENAI_API_KEY (and friends) reach the sim binary.
#   * Refuses to start if no LLM credential is set.
#   * Backs up the previous telemetry_events.jsonl and clears it.
#   * Runs selfcheck; aborts if it fails.
#   * Picks the sim/scorer binary matching the phase (looks in bin/<phase>/
#     first, then falls back to the loose binaries at the repo root).
#   * Runs the scorer (skipped for practice) and pretty-prints score.json.
#   * Prints a telemetry summary: p50/p95 wall, retries, PII, detected faults.

set -euo pipefail

PHASE="public"
CONCURRENCY=8
USERS=""
TURNS=""
TEAM="${TEAM:-s1}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --private)   PHASE="private"; shift ;;
        --public)    PHASE="public";  shift ;;
        --practice)  PHASE="practice"; shift ;;
        -c|--concurrency) CONCURRENCY="$2"; shift 2 ;;
        --users)     USERS="$2"; shift 2 ;;
        --turns)     TURNS="$2"; shift 2 ;;
        --team)      TEAM="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,16p' "$0"; exit 0 ;;
        *)
            echo "unknown arg: $1 (use --private | --practice | -c N | --users N | --turns N | --team X)" >&2
            exit 2 ;;
    esac
done

cd "$(dirname "$0")"

# ---- 1. load .env -----------------------------------------------------------
if [[ -f .env ]]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

if [[ -z "${OPENAI_API_KEY:-}" && -z "${LOCAL_BASE_URL:-}" && -z "${ANTHROPIC_API_KEY:-}" ]]; then
    echo "ERROR: no LLM credential found. Set OPENAI_API_KEY in .env or LOCAL_BASE_URL." >&2
    exit 2
fi

# ---- 2. resolve binaries ----------------------------------------------------
case "$PHASE" in
    practice)
        SIM_BIN="bin/practice/observathon-sim"
        SCORE_BIN=""
        ROOT_SIM="./observathon-sim"
        ROOT_SCORE=""
        ;;
    public)
        SIM_BIN="bin/public/observathon-sim"
        SCORE_BIN="bin/public/observathon-score"
        ROOT_SIM="./observathon-sim"
        ROOT_SCORE="./observathon-score"
        ;;
    private)
        SIM_BIN="bin/private/observathon-sim"
        SCORE_BIN="bin/private/observathon-score"
        ROOT_SIM="./observathon-private-sim"
        ROOT_SCORE="./observathon-private-score"
        ;;
esac

[[ ! -x "$SIM_BIN"   && -x "$ROOT_SIM"   ]] && SIM_BIN="$ROOT_SIM"
[[ -n "$SCORE_BIN" && ! -x "$SCORE_BIN" && -n "$ROOT_SCORE" && -x "$ROOT_SCORE" ]] && SCORE_BIN="$ROOT_SCORE"

if [[ ! -x "$SIM_BIN" ]]; then
    echo "ERROR: sim binary not found for phase '$PHASE' (tried $SIM_BIN and $ROOT_SIM)." >&2
    exit 2
fi

echo "phase=$PHASE  sim=$SIM_BIN  score=${SCORE_BIN:-<none>}  team=$TEAM  concurrency=$CONCURRENCY"
echo

# ---- 3. selfcheck -----------------------------------------------------------
echo "==> selfcheck"
python harness/selfcheck.py
echo

# ---- 4. archive previous telemetry, then clear -----------------------------
TELEMETRY="solution/telemetry_events.jsonl"
if [[ -f "$TELEMETRY" && -s "$TELEMETRY" ]]; then
    mv "$TELEMETRY" "${TELEMETRY%.jsonl}.prev.jsonl"
fi
: > "$TELEMETRY"

# ---- 5. macOS Gatekeeper (idempotent, silent on fail) ----------------------
xattr -dr com.apple.quarantine "$SIM_BIN" 2>/dev/null || true
[[ -n "$SCORE_BIN" ]] && xattr -dr com.apple.quarantine "$SCORE_BIN" 2>/dev/null || true

# ---- 6. run the simulator ---------------------------------------------------
SIM_ARGS=(--config solution/config.json
          --wrapper solution/wrapper.py
          --out run_output.json
          --concurrency "$CONCURRENCY")
[[ -n "$USERS" ]] && SIM_ARGS+=(--users "$USERS")
[[ -n "$TURNS" ]] && SIM_ARGS+=(--turns "$TURNS")

echo "==> sim :: $SIM_BIN ${SIM_ARGS[*]}"
START_TS=$(date +%s)
set +e
"$SIM_BIN" "${SIM_ARGS[@]}"
SIM_EXIT=$?
set -e
echo "    sim exit=$SIM_EXIT wall=$(( $(date +%s) - START_TS ))s"
echo

# ---- 7. score (skip for practice) -------------------------------------------
if [[ -n "$SCORE_BIN" && -x "$SCORE_BIN" ]]; then
    echo "==> score :: team=$TEAM"
    "$SCORE_BIN" --run run_output.json --findings solution/findings.json \
                 --team "$TEAM" --out score.json
    echo
    echo "==> score.json"
    python -m json.tool score.json
    echo
else
    echo "(practice or no scorer binary -- skipping scoring step)"
fi

# ---- 8. telemetry summary ---------------------------------------------------
echo "==> telemetry summary (solution/telemetry_events.jsonl)"
python3 <<'PY'
import json
from collections import Counter
path = "solution/telemetry_events.jsonl"
events = []
with open(path, encoding="utf-8") as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except Exception:
            pass
if not events:
    print("no events"); raise SystemExit
walls = sorted(int(e.get("wall_ms") or 0) for e in events)
def pct(p):
    if not walls: return 0
    return walls[min(len(walls)-1, int(len(walls)*p))]
def s(key): return sum(int(e.get(key) or 0) for e in events)
print(f"  n={len(events)}")
print(f"  wall_ms  p50={pct(0.50)}  p95={pct(0.95)}  p99={pct(0.99)}  max={walls[-1] if walls else 0}")
print(f"  retries={s('retry_count')}  pii_found={s('pii_found')}  injection_masked={s('injection_masked')}")
print(f"  cache_hits={sum(1 for e in events if e.get('cache_hit'))}")
print("  status:", dict(Counter(e.get('status') for e in events)))
faults = Counter(f for e in events for f in (e.get("detected_faults") or []))
print("  detected_faults:", dict(faults))
errs = Counter((e.get('last_error') or '')[:90] for e in events if e.get('last_error'))
if errs:
    print("  top errors:")
    for k, v in errs.most_common(3):
        print(f"    {v}x  {k}")
PY
