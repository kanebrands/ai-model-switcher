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
source = source.replace("COMFY_DOCKERFILE\nfi\n", "COMFY_DOCKERFILE\nfi\n" +
    "mkdir -p \"$INSTALL_DIR/multimodal\"\n" +
    "if [[ ! -w \"$INSTALL_DIR/multimodal\" ]]; then sudo chown \"$(id -u):$(id -g)\" \"$INSTALL_DIR/multimodal\"; fi\n" +
    "cat > \"$INSTALL_DIR/multimodal/Dockerfile\" <<'MULTIMODAL_DOCKERFILE'\n" +
    (root / "multimodal.Dockerfile").read_text().rstrip() + "\nMULTIMODAL_DOCKERFILE\n", 1)
source = replace_block(source, "gpu_control/server.py", "GPU_CONTROLLER", (root / "server.py").read_text())
source = replace_block(source, "media-models.sh", "MEDIA_MODELS", (root / "media-models.sh").read_text())
source = replace_block(source, "ai-control.sh", "CONTROL", (root / "ai-control.sh").read_text())

insert_after = "GPU_CONTROLLER\n"
modules = ""
for target, marker, file in (
    ("gpu_control/benchmarks.py", "BENCHMARK_RUNNER", "benchmarks.py"),
    ("gpu_control/benchmark_views.py", "BENCHMARK_VIEWS", "benchmark_views.py"),
    ("gpu_control/operation_poll.js", "OPERATION_POLL", "operation_poll.js"),
    ("gpu_control/harness_views.py", "HARNESS_VIEWS", "harness_views.py"),
    ("gpu_control/model_catalog.py", "MODEL_CATALOG", "model_catalog.py"),
    ("gpu_control/hybrid_memory.py", "HYBRID_MEMORY", "hybrid_memory.py"),
    ("gpu_control/model_artifacts.py", "MODEL_ARTIFACTS", "model_artifacts.py"),
    ("gpu_control/model_views.py", "MODEL_VIEWS", "model_views.py"),
    ("gpu_control/catalog_discovery.py", "CATALOG_DISCOVERY", "catalog_discovery.py"),
    ("gpu_control/engine_updates.py", "ENGINE_UPDATES", "engine_updates.py"),
    ("gpu_control/webui_updates.py", "WEBUI_UPDATES", "webui_updates.py"),
    ("gpu_control/sharing.py", "SHARING_POLICY", "sharing.py"),
    ("gpu_control/sharing_views.py", "SHARING_VIEWS", "sharing_views.py"),
    ("gpu_control/fairness_gateway.py", "FAIRNESS_GATEWAY", "fairness_gateway.py"),
    ("gpu_control/gpu_metrics.py", "GPU_METRICS", "gpu_metrics.py"),
    ("gpu_control/parallel_tuning.py", "PARALLEL_TUNING", "parallel_tuning.py"),
    ("gpu_control/install_profile.py", "INSTALL_PROFILE", "install_profile.py"),
    ("gpu_control/context_tuning.py", "CONTEXT_TUNING", "context_tuning.py"),
    ("gpu_control/media_catalog.py", "MEDIA_CATALOG", "media_catalog.py"),
    ("gpu_control/multimodal_jobs.py", "MULTIMODAL_JOBS", "multimodal_jobs.py"),
    ("gpu_control/multimodal_workers.py", "MULTIMODAL_WORKERS", "multimodal_workers.py"),
    ("gpu_control/multimodal_service.py", "MULTIMODAL_SERVICE", "multimodal_service.py"),
    ("gpu_control/multimodal_profiles.py", "MULTIMODAL_PROFILES", "multimodal_profiles.py"),
    ("gpu_control/image_edit.py", "IMAGE_EDIT_WORKER", "image_edit.py"),
    ("gpu_control/image_edit_views.py", "IMAGE_EDIT_VIEWS", "image_edit_views.py"),
    ("gpu_control/image_edit_poll.js", "IMAGE_EDIT_POLL", "image_edit_poll.js"),
):
    modules += f"\ncat > \"$INSTALL_DIR/{target}\" <<'{marker}'\n{(root / file).read_text().rstrip()}\n{marker}\n"
modules += f"\ncat > \"$INSTALL_DIR/gpu_control/update-engine.sh\" <<'ENGINE_UPDATE_SCRIPT'\n{(root / 'update-engine.sh').read_text().rstrip()}\nENGINE_UPDATE_SCRIPT\n"
modules += f"\ncat > \"$INSTALL_DIR/gpu_control/update-webui.sh\" <<'WEBUI_UPDATE_SCRIPT'\n{(root / 'update-webui.sh').read_text().rstrip()}\nWEBUI_UPDATE_SCRIPT\n"
modules += f"\ncat > \"$INSTALL_DIR/gpu_control/tune-parallel.sh\" <<'PARALLEL_TUNE_SCRIPT'\n{(root / 'tune-parallel.sh').read_text().rstrip()}\nPARALLEL_TUNE_SCRIPT\n"
modules += f"\ncat > \"$INSTALL_DIR/gpu_control/tune-context.sh\" <<'CONTEXT_TUNE_SCRIPT'\n{(root / 'tune-context.sh').read_text().rstrip()}\nCONTEXT_TUNE_SCRIPT\n"
modules += f"\ncat > \"$INSTALL_DIR/gpu_control/install_models.sh\" <<'INSTALL_MODELS_SCRIPT'\n{(root / 'install_models.sh').read_text().rstrip()}\nINSTALL_MODELS_SCRIPT\n"
modules += f"\ncat > \"$INSTALL_DIR/start-selected.sh\" <<'START_SELECTED_SCRIPT'\n{(root / 'start-selected.sh').read_text().rstrip()}\nSTART_SELECTED_SCRIPT\n"
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
for generated in docker-compose.yml multimodal/Dockerfile gpu_control/server.py gpu_control/benchmarks.py gpu_control/benchmark_views.py gpu_control/operation_poll.js gpu_control/harness_views.py gpu_control/model_catalog.py gpu_control/model_artifacts.py gpu_control/hybrid_memory.py gpu_control/model_views.py gpu_control/catalog_discovery.py gpu_control/engine_updates.py gpu_control/webui_updates.py gpu_control/sharing.py gpu_control/sharing_views.py gpu_control/fairness_gateway.py gpu_control/gpu_metrics.py gpu_control/parallel_tuning.py gpu_control/install_profile.py gpu_control/install_models.sh gpu_control/context_tuning.py gpu_control/update-engine.sh gpu_control/update-webui.sh gpu_control/tune-parallel.sh gpu_control/tune-context.sh gpu_control/media_catalog.py gpu_control/multimodal_jobs.py gpu_control/multimodal_workers.py gpu_control/multimodal_service.py gpu_control/multimodal_profiles.py gpu_control/image_edit.py gpu_control/image_edit_views.py gpu_control/image_edit_poll.js gpu_control/assets/gpu-workstation.webp ai-control.sh start-selected.sh; do
  if [[ -e "$INSTALL_DIR/$generated" && ! -w "$INSTALL_DIR/$generated" ]]; then
    sudo chown "$(id -u):$(id -g)" "$INSTALL_DIR/$generated"
  fi
done''', 1)

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

old_start = '''sudo docker compose --env-file .env --profile media --profile vision --profile huihui stop comfyui vision huihui
sudo docker compose --env-file .env up -d
sudo docker compose --env-file .env --profile media create --force-recreate comfyui
sudo docker compose --env-file .env --profile vision create vision
sudo docker compose --env-file .env --profile huihui create huihui'''
new_start = '''bash "$INSTALL_DIR/start-selected.sh"'''
assert source.count(old_start) == 1
source = source.replace(old_start, new_start, 1)
old_build = 'sudo docker compose --env-file .env --profile media build comfyui\n'
assert source.count(old_build) == 1
source = source.replace(old_build, '', 1)
source = source.replace("info 'Building ComfyUI and starting the LLM (downloads may take a while)'",
                        "info 'Starting selected workloads (downloads may take a while)'", 1)

# The base script embeds an older control helper. Keep the standalone installer
# in sync with the maintained source helper used by this repository.
source = replace_block(source, "ai-control.sh", "CONTROL", (root / "ai-control.sh").read_text())

source = source.replace("printf 'Model name: local-coder\\n'", "printf 'Host RAM visible to Ubuntu: %s GiB\\n' \"$HOST_RAM_GIB\"\nprintf 'Benchmarks: http://%s:%s/benchmarks\\n' \"$READY_BIND_IP\" \"$READY_CONTROL_PORT\"\nprintf 'Initial model: %s (%s)\\n' \"$READY_SERVICE\" \"$READY_ALIAS\"", 1)

path.write_text(source)
print(f"Updated {path} ({len(source.splitlines())} lines)")
