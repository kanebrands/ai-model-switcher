#!/bin/sh
# Fixed-action Compose context change. Invoked only by gpu-control in docker:cli.
set -eu
cd /stack
action=${1:-}
service=${2:-}
tokens=${3:-}
case "$service" in
  llm) key=AI_CONTEXT_TOKENS ;;
  qwen4b) key=AI_QWEN4B_CONTEXT_TOKENS ;;
  huihui) key=AI_HUIHUI_CONTEXT_TOKENS ;;
  flint-iq4) key=AI_FLINT_IQ4_CONTEXT_TOKENS ;;
  flint-q3) key=AI_FLINT_Q3_CONTEXT_TOKENS ;;
  deepseek-v4-distill) key=AI_DEEPSEEK_CONTEXT_TOKENS ;;
  qwen38-ablit) key=AI_QWEN38_CONTEXT_TOKENS ;;
  gemma4-ablit) key=AI_GEMMA4_CONTEXT_TOKENS ;;
  vision) key=AI_VISION_CONTEXT_TOKENS ;;
  *) echo 'Unknown model service' >&2; exit 2 ;;
esac
case "$tokens" in ''|*[!0-9]*) echo 'Invalid context size' >&2; exit 2;; esac
[ "$tokens" -ge 4096 ] && [ "$tokens" -le 262144 ] || { echo 'Context out of range' >&2; exit 2; }
[ $((tokens % 1024)) -eq 0 ] || { echo 'Context must be a multiple of 1024' >&2; exit 2; }

compose() {
  docker compose --env-file .env --profile qwen4b --profile huihui --profile flint \
    --profile deepseek --profile qwen38 --profile gemma4 --profile vision "$@"
}

save_context() {
  tmp=$(mktemp /stack/.env.context.XXXXXX)
  trap 'rm -f "$tmp"' EXIT
  cp -p .env "$tmp"
  awk -v key="$key" -v value="$tokens" '
    BEGIN { seen=0 }
    index($0, key "=") == 1 { if (!seen) print key "=" value; seen=1; next }
    { print }
    END { if (!seen) print key "=" value }
  ' .env > "$tmp"
  mv "$tmp" .env
  trap - EXIT
}

case "$action" in
  apply)
    env "$key=$tokens" docker compose --env-file .env --profile qwen4b --profile huihui \
      --profile flint --profile deepseek --profile qwen38 --profile gemma4 --profile vision \
      up -d --no-deps --force-recreate --pull never "$service"
    ;;
  rollback)
    env "$key=$tokens" docker compose --env-file .env --profile qwen4b --profile huihui \
      --profile flint --profile deepseek --profile qwen38 --profile gemma4 --profile vision \
      up -d --no-deps --force-recreate --pull never "$service"
    save_context
    ;;
  commit)
    save_context
    ;;
  *) echo 'Invalid context action' >&2; exit 2 ;;
esac
