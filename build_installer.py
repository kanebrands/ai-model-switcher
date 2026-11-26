"""Fold the tested live configuration into the standalone Ubuntu installer."""
from pathlib import Path
import base64
import re

root = Path(__file__).parent
path = root / "ai-setup.sh"
source = (root / "ai-setup.base.sh").read_text()


def replace_block(script, target, marker, content, wrapper=False):
    expression = re.compile(r"(?m)^cat > \"\$INSTALL_DIR/" + re.escape(target) + r"\" <<'" + marker + r"'\n.*?^" + marker + r"$", re.S)
    rendered = f"cat > \"$INSTALL_DIR/{target}\" <<'{marker}'\n{content.rstrip()}\n{marker}"
    if wrapper:
        rendered = f'if [[ ! -s "$INSTALL_DIR/{target}" ]]; then\n{rendered}\nfi'
    updated, count = expression.subn(lambda _: rendered, script, count=1)
    if count != 1:
        raise ValueError(f"Could not replace {target}: {count} matches")
    return updated


source = replace_block(source, "docker-compose.yml", "COMPOSE", (root / "docker-compose.yml").read_text())
nginx = (root / "nginx.conf").read_text().replace("    include /etc/nginx/iredmail-ai-tls.conf;\n", "")
source = replace_block(source, "secure_proxy/nginx.conf", "NGINX", nginx, wrapper=True)
nginx_upgrade = '''if [[ ! -w "$INSTALL_DIR/secure_proxy/nginx.conf" ]]; then
  sudo chown "$(id -u):$(id -g)" "$INSTALL_DIR/secure_proxy/nginx.conf"
fi
python3 - "$INSTALL_DIR/secure_proxy/nginx.conf" <<'PY_NGINX_GATEWAY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
config = path.read_text()
original = config
updates = (
    ("auth_request /_active_llm;\\n            auth_request_set $llm_upstream $upstream_http_x_llm_backend;\\n            proxy_pass http://$llm_upstream;",
     "set $api_gateway fairness-gateway:8002;\\n            proxy_pass http://$api_gateway;"),
    ("set $vision_upstream vision:8000;\\n            rewrite ^/vision(/.*)$ $1 break;\\n            proxy_pass http://$vision_upstream;",
     "set $api_gateway fairness-gateway:8002;\\n            proxy_pass http://$api_gateway;"),
)
for old, new in updates:
    if old in config:
        config = config.replace(old, new, 1)
    elif new not in config:
        raise SystemExit("Existing nginx API routing differs from the installed template; cannot safely enable client limits")
if config != original:
    path.write_text(config)
PY_NGINX_GATEWAY
if [[ -f "$INSTALL_DIR/secure_proxy/iredmail-ai-tls.conf" && ! -w "$INSTALL_DIR/secure_proxy/iredmail-ai-tls.conf" ]]; then
  sudo chown "$(id -u):$(id -g)" "$INSTALL_DIR/secure_proxy/iredmail-ai-tls.conf"
fi
python3 - "$INSTALL_DIR/secure_proxy/iredmail-ai-tls.conf" <<'PY_TLS_GATEWAY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
if path.exists():
    config = path.read_text()
    old = "auth_request /_iredai_active_llm;\\n        auth_request_set $llm_upstream $upstream_http_x_llm_backend;\\n        proxy_pass http://$llm_upstream;"
    new = "set $api_gateway fairness-gateway:8002;\\n        proxy_pass http://$api_gateway;"
    if old in config:
        path.write_text(config.replace(old, new, 1))
PY_TLS_GATEWAY
'''
assert source.count("NGINX\nfi\n") == 1
source = source.replace("NGINX\nfi\n", "NGINX\nfi\n" + nginx_upgrade, 1)
source = replace_block(source, "comfyui/Dockerfile", "COMFY_DOCKERFILE", (root / "comfyui.Dockerfile").read_text(), wrapper=True)
source = replace_block(source, "gpu_control/server.py", "GPU_CONTROLLER", (root / "server.py").read_text())
source = replace_block(source, "media-models.sh", "MEDIA_MODELS", (root / "media-models.sh").read_text())
source = replace_block(source, "ai-control.sh", "CONTROL", (root / "ai-control.sh").read_text())

insert_after = "GPU_CONTROLLER\n"
modules = ""
for target, marker, file in (
    ("gpu_control/benchmarks.py", "BENCHMARK_RUNNER", "benchmarks.py"),
    ("gpu_control/benchmark_views.py", "BENCHMARK_VIEWS", "benchmark_views.py"),
    ("gpu_control/model_catalog.py", "MODEL_CATALOG", "model_catalog.py"),
    ("gpu_control/model_views.py", "MODEL_VIEWS", "model_views.py"),
    ("gpu_control/catalog_discovery.py", "CATALOG_DISCOVERY", "catalog_discovery.py"),
    ("gpu_control/engine_updates.py", "ENGINE_UPDATES", "engine_updates.py"),
    ("gpu_control/webui_updates.py", "WEBUI_UPDATES", "webui_updates.py"),
    ("gpu_control/sharing.py", "SHARING_POLICY", "sharing.py"),
    ("gpu_control/sharing_views.py", "SHARING_VIEWS", "sharing_views.py"),
    ("gpu_control/fairness_gateway.py", "FAIRNESS_GATEWAY", "fairness_gateway.py"),
    ("gpu_control/gpu_metrics.py", "GPU_METRICS", "gpu_metrics.py"),
    ("gpu_control/parallel_tuning.py", "PARALLEL_TUNING", "parallel_tuning.py"),
    ("gpu_control/context_tuning.py", "CONTEXT_TUNING", "context_tuning.py"),
    ("gpu_control/media_catalog.py", "MEDIA_CATALOG", "media_catalog.py"),
):
    modules += f"\ncat > \"$INSTALL_DIR/{target}\" <<'{marker}'\n{(root / file).read_text().rstrip()}\n{marker}\n"
modules += f"\ncat > \"$INSTALL_DIR/gpu_control/update-engine.sh\" <<'ENGINE_UPDATE_SCRIPT'\n{(root / 'update-engine.sh').read_text().rstrip()}\nENGINE_UPDATE_SCRIPT\n"
modules += f"\ncat > \"$INSTALL_DIR/gpu_control/update-webui.sh\" <<'WEBUI_UPDATE_SCRIPT'\n{(root / 'update-webui.sh').read_text().rstrip()}\nWEBUI_UPDATE_SCRIPT\n"
modules += f"\ncat > \"$INSTALL_DIR/gpu_control/tune-parallel.sh\" <<'PARALLEL_TUNE_SCRIPT'\n{(root / 'tune-parallel.sh').read_text().rstrip()}\nPARALLEL_TUNE_SCRIPT\n"
modules += f"\ncat > \"$INSTALL_DIR/gpu_control/tune-context.sh\" <<'CONTEXT_TUNE_SCRIPT'\n{(root / 'tune-context.sh').read_text().rstrip()}\nCONTEXT_TUNE_SCRIPT\n"
asset = base64.b64encode((root / "assets/gpu-workstation.webp").read_bytes()).decode("ascii")
modules += ("\nmkdir -p \"$INSTALL_DIR/gpu_control/assets\"\n"
            "base64 -d > \"$INSTALL_DIR/gpu_control/assets/gpu-workstation.webp\" <<'GPU_HERO_WEBP'\n"
            + asset + "\nGPU_HERO_WEBP\n")
assert source.count(insert_after) == 1
source = source.replace(insert_after, insert_after + modules, 1)

source = source.replace(
    'chmod 700 "$INSTALL_DIR/secure_proxy"',
    '''chmod 700 "$INSTALL_DIR/secure_proxy"
if [[ ! -d "$INSTALL_DIR/gpu_control/results" ]]; then
  install -d -m 700 "$INSTALL_DIR/gpu_control/results"
fi
# Files may have been updated by root over SSH. Allow the normal installer user
# to replace only its generated configuration files; leave data and keys alone.
if [[ -d "$INSTALL_DIR/gpu_control/assets" && ! -w "$INSTALL_DIR/gpu_control/assets" ]]; then
  sudo chown "$(id -u):$(id -g)" "$INSTALL_DIR/gpu_control/assets"
fi
for generated in docker-compose.yml gpu_control/server.py gpu_control/benchmarks.py gpu_control/benchmark_views.py gpu_control/model_catalog.py gpu_control/model_views.py gpu_control/catalog_discovery.py gpu_control/engine_updates.py gpu_control/webui_updates.py gpu_control/sharing.py gpu_control/sharing_views.py gpu_control/fairness_gateway.py gpu_control/gpu_metrics.py gpu_control/parallel_tuning.py gpu_control/context_tuning.py gpu_control/update-engine.sh gpu_control/update-webui.sh gpu_control/tune-parallel.sh gpu_control/tune-context.sh gpu_control/media_catalog.py gpu_control/assets/gpu-workstation.webp ai-control.sh; do
  if [[ -e "$INSTALL_DIR/$generated" && ! -w "$INSTALL_DIR/$generated" ]]; then
    sudo chown "$(id -u):$(id -g)" "$INSTALL_DIR/$generated"
  fi
done''', 1)

after_driver = 'DRIVER_VERSION=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n 1)'
assert source.count(after_driver) == 1
source = source.replace(after_driver, after_driver + '''
HOST_RAM_GIB=$(awk '/^MemTotal:/ {printf "%.1f", $2 / 1048576}' /proc/meminfo)
[[ -n $HOST_RAM_GIB ]] || die 'Could not read host RAM from /proc/meminfo.'
info "Ubuntu reports $HOST_RAM_GIB GiB of usable RAM; sizing CPU offload against that value"''', 1)

anchor_helper = "sudo systemctl enable --now docker"
assert source.count(anchor_helper) == 1
source = source.replace(anchor_helper, anchor_helper + '''
# The switcher uses this small CLI container only after the operator chooses
# to apply a llama.cpp engine update in the web interface.
sudo docker pull docker:cli''', 1)

compose_end = "\nCOMPOSE\n"
assert source.count(compose_end) == 1
source = source.replace(compose_end, compose_end + '''# The optional iRedMail TLS listener exists only on hosts with its own config.
# Keep the existing nginx config on upgrades, including any local TLS changes.
if [[ ! -f "$INSTALL_DIR/secure_proxy/iredmail-ai-tls.conf" ]]; then
  sed -i '/AI_TLS_PORT:-8443/d; /iredmail-ai-tls.conf:/d; /secure_proxy\/tls:/d' "$INSTALL_DIR/docker-compose.yml"
fi
''', 1)

source = source.replace(
    'if not 0.5 <= gpu_util < 1 or not 0.5 <= vision_gpu_util < 1 or not 0 <= offload <= 64:\n    sys.exit(\'AI_GPU_MEMORY_UTILIZATION and AI_VISION_GPU_UTILIZATION must be 0.5..<1; AI_CPU_OFFLOAD_GB must be 0..64 GiB\')',
    '''ram_kib = next(int(line.split()[1]) for line in pathlib.Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemTotal:'))
max_offload = max(0, min(64, int(ram_kib / 1048576 - 16)))
if not 0.5 <= gpu_util < 1 or not 0.5 <= vision_gpu_util < 1 or not 0 <= offload <= max_offload:
    sys.exit(f'GPU utilization must be 0.5..<1; CPU offload must be 0..{max_offload} GiB on this host')''', 1)

anchor = 'SWITCH_PASSWORD=$(sed -n \'s/^AI_SWITCH_PASSWORD=//p\' "$ENV_FILE" | head -n 1)'
assert source.count(anchor) == 1
downloads = '''# Keep every selectable GGUF model available after a fresh installation.
# hf resumes incomplete files in the model directory and verifies Hub metadata.
download_gguf() {
  local repository=$1 filename=$2
  [[ -s "$HUIHUI_MODEL_DIR/$filename" ]] && return 0
  info "Downloading $filename to $HUIHUI_MODEL_DIR"
  sudo docker run --rm -e HF_HOME=/tmp/hf \\
    -v "$HUIHUI_MODEL_DIR:/models" --entrypoint hf \\
    vllm/vllm-openai:v0.32.0-ubuntu2404 \\
    download "$repository" "$filename" --local-dir /models
  [[ -s "$HUIHUI_MODEL_DIR/$filename" ]] || die "Model download failed: $filename"
}
download_gguf bartowski/RecursiveSI_flint-GGUF RecursiveSI_flint-IQ4_XS.gguf
download_gguf bartowski/RecursiveSI_flint-GGUF RecursiveSI_flint-Q3_K_M.gguf
download_gguf mradermacher/DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic-GGUF DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic.Q5_K_M.gguf
download_gguf IsValorum/Qwen3.8-35B-A3B-Distill-MTP-APEX-I-MiniPlus-V2.1-Abliterated-GGUF Qwen3.8-35B-A3B-Distill.APEX-I-MiniPlus-V2.1-Abliterated.gguf
download_gguf bullerwins/Huihui-gemma-4-26B-A4B-it-abliterated-GGUF Huihui-gemma-4-26B-A4B-it-abliterated-Q4_K_M.gguf

'''
source = source.replace(anchor, downloads + anchor, 1)

old_start = '''sudo docker compose --env-file .env --profile media --profile vision --profile huihui stop comfyui vision huihui
sudo docker compose --env-file .env up -d
sudo docker compose --env-file .env --profile media create --force-recreate comfyui
sudo docker compose --env-file .env --profile vision create vision
sudo docker compose --env-file .env --profile huihui create huihui'''
new_start = '''sudo docker ps -q --filter label=local-ai.model-manager=1 | xargs -r sudo docker stop
sudo docker compose --env-file .env --profile media --profile vision --profile huihui --profile qwen4b --profile flint --profile deepseek --profile qwen38 --profile gemma4 stop comfyui vision huihui qwen4b flint-iq4 flint-q3 deepseek-v4-distill qwen38-ablit gemma4-ablit
sudo docker compose --env-file .env up -d
sudo docker compose --env-file .env --profile media create comfyui
sudo docker compose --env-file .env --profile vision create vision
sudo docker compose --env-file .env --profile huihui create huihui
sudo docker compose --env-file .env --profile qwen4b create qwen4b
sudo docker compose --env-file .env --profile flint create flint-iq4 flint-q3
sudo docker compose --env-file .env --profile deepseek create deepseek-v4-distill
sudo docker compose --env-file .env --profile qwen38 create qwen38-ablit
sudo docker compose --env-file .env --profile gemma4 create gemma4-ablit'''
assert source.count(old_start) == 1
source = source.replace(old_start, new_start, 1)

source = source.replace("printf 'Model name: local-coder\\n'", "printf 'Host RAM visible to Ubuntu: %s GiB\\n' \"$HOST_RAM_GIB\"\nprintf 'Benchmarks: http://%s:%s/benchmarks\\n' \"$READY_BIND_IP\" \"$READY_CONTROL_PORT\"\nprintf 'Model name: local-coder\\n'", 1)

path.write_text(source)
print(f"Updated {path} ({len(source.splitlines())} lines)")
