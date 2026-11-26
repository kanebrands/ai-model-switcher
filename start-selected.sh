#!/usr/bin/env bash
# Start only the workloads explicitly selected at installation.
set -Eeuo pipefail
DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$DIR"
[[ -s .env ]] || { echo 'Missing .env' >&2; exit 1; }
compose() { sudo docker compose --env-file .env "$@"; }
all_profiles=(--profile coder --profile cpu-tiny --profile cpu-4b --profile qwen4b
              --profile huihui --profile flint --profile deepseek --profile qwen38
              --profile gemma4 --profile vision --profile media)
all_services=(llm cpu-tiny cpu-4b qwen4b huihui flint-iq4 flint-q3
              deepseek-v4-distill qwen38-ablit gemma4-ablit vision comfyui)

service_for() {
  case "$1" in
    cpu-tiny) profile=cpu-tiny; service=cpu-tiny; alias=local-cpu-tiny ;;
    cpu-4b) profile=cpu-4b; service=cpu-4b; alias=local-cpu-4b ;;
    llm) profile=coder; service=llm; alias=local-coder ;;
    qwen4b) profile=qwen4b; service=qwen4b; alias=local-qwen4b ;;
    huihui) profile=huihui; service=huihui; alias=local-huihui ;;
    flint-iq4) profile=flint; service=flint-iq4; alias=local-flint-iq4 ;;
    flint-q3) profile=flint; service=flint-q3; alias=local-flint-q3 ;;
    deepseek) profile=deepseek; service=deepseek-v4-distill; alias=local-deepseek-v4-distill ;;
    qwen38) profile=qwen38; service=qwen38-ablit; alias=local-qwen38-ablit ;;
    gemma4) profile=gemma4; service=gemma4-ablit; alias=local-gemma4-ablit ;;
    vision) profile=vision; service=vision; alias=local-vision ;;
    media) profile=media; service=comfyui; alias=ComfyUI ;;
    *) echo "Unknown selected model: $1" >&2; return 2 ;;
  esac
}

selected=$(sed -n 's/^AI_SELECTED_MODELS=//p' .env | head -n 1)
active=$(sed -n 's/^AI_START_MODEL=//p' .env | head -n 1)
[[ -n $selected && -n $active ]] || { echo 'Run the installer to select models first' >&2; exit 1; }
IFS=, read -ra models <<< "$selected"
[[ " ,$selected," == *",$active,"* ]] || { echo 'Initial model is not in selection' >&2; exit 1; }

sudo docker ps -q --filter label=local-ai.model-manager=1 | xargs -r sudo docker stop
compose "${all_profiles[@]}" stop "${all_services[@]}"
compose up -d
for model in "${models[@]}"; do
  service_for "$model"
  if [[ $model == media ]]; then
    compose --profile media build comfyui
  fi
  compose --profile "$profile" create "$service"
done
service_for "$active"
compose --profile "$profile" up -d --no-deps "$service"
printf 'Started %s (%s)\n' "$active" "$alias"
