#!/bin/sh
# Fixed-action Compose slot change. Invoked only by gpu-control in docker:cli.
set -eu
cd /stack
action=${1:-}
service=${2:-}
slots=${3:-}
case "$service" in
  cpu-tiny) key=AI_CPU_TINY_PARALLEL_SLOTS ;;
  cpu-4b) key=AI_CPU_4B_PARALLEL_SLOTS ;;
  llm) key=AI_CODER_PARALLEL_SLOTS ;;
  qwen4b) key=AI_QWEN4B_PARALLEL_SLOTS ;;
  huihui) key=AI_HUIHUI_PARALLEL_SLOTS ;;
  flint-iq4) key=AI_FLINT_IQ4_PARALLEL_SLOTS ;;
  flint-q3) key=AI_FLINT_Q3_PARALLEL_SLOTS ;;
  deepseek-v4-distill) key=AI_DEEPSEEK_PARALLEL_SLOTS ;;
  qwen38-ablit) key=AI_QWEN38_PARALLEL_SLOTS ;;
  gemma4-ablit) key=AI_GEMMA4_PARALLEL_SLOTS ;;
  vision) key=AI_VISION_PARALLEL_SLOTS ;;
  *) echo 'Unknown text model service' >&2; exit 2 ;;
esac
case "$slots" in ''|*[!0-9]*) echo 'Invalid slot count' >&2; exit 2 ;; esac
[ "$slots" -ge 1 ] && [ "$slots" -le 8 ] || { echo 'Slot count out of range' >&2; exit 2; }

save_slots() {
  tmp=$(mktemp /stack/.env.parallel.XXXXXX)
  trap 'rm -f "$tmp"' EXIT
  cp -p .env "$tmp"
  awk -v key="$key" -v value="$slots" '
    BEGIN { seen=0 }
    index($0,key "=")==1 { if (!seen) print key "=" value; seen=1; next }
    { print }
    END { if (!seen) print key "=" value }
  ' .env > "$tmp"
  mv "$tmp" .env
  trap - EXIT
}

compose() {
  env "$key=$slots" docker compose --env-file .env --profile coder --profile cpu-tiny --profile cpu-4b --profile qwen4b --profile huihui \
    --profile flint --profile deepseek --profile qwen38 --profile gemma4 --profile vision \
    up -d --no-deps --force-recreate --pull never "$service"
}
case "$action" in
  apply) compose ;;
  rollback) compose; save_slots ;;
  commit) save_slots ;;
  *) echo 'Invalid slot action' >&2; exit 2 ;;
esac
