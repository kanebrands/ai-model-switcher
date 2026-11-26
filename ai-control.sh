#!/usr/bin/env bash
set -Eeuo pipefail
DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[[ -f $DIR/.env && -f $DIR/docker-compose.yml ]] || { echo 'Missing .env or docker-compose.yml. Run ai_setup.sh first.' >&2; exit 1; }
cd "$DIR"
docker_cmd() { sudo docker compose --env-file .env "$@"; }
stop_managed_models() {
  local ids=()
  mapfile -t ids < <(sudo docker ps -q --filter label=local-ai.model-manager=1)
  if (( ${#ids[@]} )); then sudo docker stop "${ids[@]}"; fi
}
port=$(sed -n 's/^AI_PORT=//p' .env | head -n 1)
web_port=$(sed -n 's/^AI_WEB_PORT=//p' .env | head -n 1)
control_port=$(sed -n 's/^AI_CONTROL_PORT=//p' .env | head -n 1)
comfy_port=$(sed -n 's/^AI_COMFY_PORT=//p' .env | head -n 1)
bind_ip=$(sed -n 's/^AI_BIND_IP=//p' .env | head -n 1)
key=$(sed -n 's/^AI_API_KEY=//p' .env | head -n 1)
case "${1:-help}" in
  start)
    stop_managed_models
    docker_cmd --profile media --profile vision --profile huihui --profile qwen4b --profile flint --profile deepseek --profile qwen38 --profile gemma4 stop comfyui vision huihui qwen4b flint-iq4 flint-q3 deepseek-v4-distill qwen38-ablit gemma4-ablit
    docker_cmd up -d
    docker_cmd --profile media create comfyui
    docker_cmd --profile vision create vision
    docker_cmd --profile huihui create huihui
    docker_cmd --profile qwen4b create qwen4b
    docker_cmd --profile flint create flint-iq4 flint-q3
    docker_cmd --profile deepseek create deepseek-v4-distill
    docker_cmd --profile qwen38 create qwen38-ablit
    docker_cmd --profile gemma4 create gemma4-ablit
    echo "API: http://$bind_ip:$port/v1"
    echo "WebUI: http://$bind_ip:$web_port"
    echo "GPU switch: http://$bind_ip:$control_port"
    ;;
  stop) stop_managed_models; docker_cmd --profile media --profile vision --profile huihui --profile qwen4b --profile flint --profile deepseek --profile qwen38 --profile gemma4 down ;;
  restart)
    stop_managed_models
    docker_cmd --profile media --profile vision --profile huihui --profile qwen4b --profile flint --profile deepseek --profile qwen38 --profile gemma4 down
    docker_cmd up -d
    docker_cmd --profile media create comfyui
    docker_cmd --profile vision create vision
    docker_cmd --profile huihui create huihui
    docker_cmd --profile qwen4b create qwen4b
    docker_cmd --profile flint create flint-iq4 flint-q3
    docker_cmd --profile deepseek create deepseek-v4-distill
    docker_cmd --profile qwen38 create qwen38-ablit
    docker_cmd --profile gemma4 create gemma4-ablit
    ;;
  status) docker_cmd --profile media --profile vision --profile huihui --profile qwen4b --profile flint --profile deepseek --profile qwen38 --profile gemma4 ps -a; sudo docker ps -a --filter label=local-ai.model-manager=1 --format 'Managed model: {{.Names}} {{.Status}}'; nvidia-smi ;;
  context)
    printf 'Context: %s tokens (prompt and output combined)\n' "$(sed -n 's/^AI_CONTEXT_TOKENS=//p' .env | head -n 1)"
    printf 'GPU memory utilization: %s\n' "$(sed -n 's/^AI_GPU_MEMORY_UTILIZATION=//p' .env | head -n 1)"
    printf 'CPU weight offload: %s GiB\n' "$(sed -n 's/^AI_CPU_OFFLOAD_GB=//p' .env | head -n 1)"
    printf 'Vision context: %s tokens\n' "$(sed -n 's/^AI_VISION_CONTEXT_TOKENS=//p' .env | head -n 1)"
    printf 'Huihui context: %s tokens\n' "$(sed -n 's/^AI_HUIHUI_CONTEXT_TOKENS=//p' .env | head -n 1)"
    printf 'Qwen 4B context: %s tokens\n' "${AI_QWEN4B_CONTEXT_TOKENS:-32768}"
    printf 'Flint IQ4 context: %s tokens\n' "${AI_FLINT_IQ4_CONTEXT_TOKENS:-16384}"
    printf 'Flint Q3 context: %s tokens\n' "${AI_FLINT_Q3_CONTEXT_TOKENS:-32768}"
    printf 'DeepSeek V4 distill context: %s tokens\n' "${AI_DEEPSEEK_CONTEXT_TOKENS:-65536}"
    printf 'Qwen3.8 distill context: %s tokens\n' "${AI_QWEN38_CONTEXT_TOKENS:-131072}"
    printf 'Gemma 4 abliterated context: %s tokens\n' "${AI_GEMMA4_CONTEXT_TOKENS:-32768}"
    ;;
  logs) docker_cmd logs -f --tail=100 llm ;;
  huihui-logs) docker_cmd --profile huihui logs -f --tail=100 huihui ;;
  qwen4b-logs) docker_cmd --profile qwen4b logs -f --tail=100 qwen4b ;;
  flint-iq4-logs) docker_cmd --profile flint logs -f --tail=100 flint-iq4 ;;
  flint-q3-logs) docker_cmd --profile flint logs -f --tail=100 flint-q3 ;;
  deepseek-logs) docker_cmd --profile deepseek logs -f --tail=100 deepseek-v4-distill ;;
  qwen38-logs) docker_cmd --profile qwen38 logs -f --tail=100 qwen38-ablit ;;
  gemma4-logs) docker_cmd --profile gemma4 logs -f --tail=100 gemma4-ablit ;;
  huihui-files) ls -lh "$(sed -n 's/^AI_MODEL_CACHE=//p' .env | head -n 1)/gguf" ;;
  vision-logs) docker_cmd --profile vision logs -f --tail=100 vision ;;
  media-logs) docker_cmd --profile media logs -f --tail=100 comfyui ;;
  web-logs) docker_cmd logs -f --tail=100 open-webui ;;
  web-test) curl -fsS --max-time 20 -o /dev/null -w 'Open WebUI HTTP %{http_code}\n' "http://$bind_ip:$web_port/" ;;
  terminal-test)
    docker_cmd exec -T open-webui python3 -c 'import urllib.request; u=urllib.request.build_opener(urllib.request.ProxyHandler({})); print(u.open("http://open-terminal:8000/health", timeout=15).read().decode())'
    ;;
  vision-test)
    curl -fsS --max-time 30 -H "Authorization: Bearer $key" "http://$bind_ip:$port/vision/v1/models" | python3 -m json.tool
    ;;
  key) printf '%s\n' "$key" ;;
  terminal-key) sed -n 's/^AI_TERMINAL_KEY=//p' .env | head -n 1 ;;
  switch-password) sed -n 's/^AI_SWITCH_PASSWORD=//p' .env | head -n 1 ;;
  switch-url) printf 'http://%s:%s\n' "$bind_ip" "$control_port" ;;
  comfy-url) printf 'http://%s:%s\n' "$bind_ip" "$comfy_port" ;;
  test)
    model=$(curl -fsS --max-time 20 -H "Authorization: Bearer $key" "http://$bind_ip:$port/v1/models" | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"][0]["id"])')
    curl -fsS --max-time 120 "http://$bind_ip:$port/v1/chat/completions" \
      -H "Authorization: Bearer $key" -H 'Content-Type: application/json' \
      -d "{\"model\":\"$model\",\"messages\":[{\"role\":\"user\",\"content\":\"Say hello in one sentence.\"}],\"max_tokens\":32}" | python3 -m json.tool
    ;;
  huihui-test)
    curl -fsS --max-time 180 "http://$bind_ip:$port/v1/chat/completions" \
      -H "Authorization: Bearer $key" -H 'Content-Type: application/json' \
      -d '{"model":"local-huihui","messages":[{"role":"user","content":"Say hello in one sentence."}],"max_tokens":32}' | python3 -m json.tool
    ;;
  tool-test)
    curl -fsS --max-time 180 "http://$bind_ip:$port/v1/chat/completions" \
      -H "Authorization: Bearer $key" -H 'Content-Type: application/json' \
      -d '{"model":"local-coder","messages":[{"role":"user","content":"Call echo_value with the text hello."}],"tools":[{"type":"function","function":{"name":"echo_value","description":"Echo text","parameters":{"type":"object","properties":{"text":{"type":"string"}},"required":["text"],"additionalProperties":false}}}],"tool_choice":"required","max_tokens":128}' | \
      python3 -c 'import json,sys; d=json.load(sys.stdin); calls=d.get("choices",[{}])[0].get("message",{}).get("tool_calls",[]); print(json.dumps(calls or d, indent=2)); sys.exit(0 if calls else 1)'
    ;;
  codex-test)
    curl -fsS --max-time 180 "http://$bind_ip:$port/v1/responses" \
      -H "Authorization: Bearer $key" -H 'Content-Type: application/json' \
      -d '{"model":"local-coder","input":"Call echo_value with the text hello.","tools":[{"type":"function","name":"echo_value","description":"Echo the supplied text","parameters":{"type":"object","properties":{"text":{"type":"string"}},"required":["text"],"additionalProperties":false}}],"tool_choice":"required","max_output_tokens":128}' | \
      python3 -c 'import json,sys; d=json.load(sys.stdin); calls=[x for x in d.get("output",[]) if x.get("type")=="function_call"]; print(json.dumps(calls or d, indent=2)); sys.exit(0 if calls else 1)'
    ;;
  help|-h|--help) echo 'Usage: ai-control.sh {start|stop|restart|status|context|logs|qwen4b-logs|flint-iq4-logs|flint-q3-logs|deepseek-logs|qwen38-logs|gemma4-logs|huihui-logs|huihui-files|huihui-test|vision-logs|vision-test|media-logs|web-logs|web-test|terminal-test|test|tool-test|codex-test|key|terminal-key|switch-password|switch-url|comfy-url}' ;;
  *) echo "Unknown command: $1" >&2; exit 2 ;;
esac
