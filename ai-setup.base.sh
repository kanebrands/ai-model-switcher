#!/usr/bin/env bash
# Ubuntu 24.04: Docker, a hardware-selected local model, Open WebUI, and nginx.
set -Eeuo pipefail

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
info() { printf '\n==> %s\n' "$*"; }
trap 'printf "ERROR at line %s. Re-run this script after fixing the issue.\n" "$LINENO" >&2' ERR

[[ $EUID -ne 0 ]] || die 'Run as your normal Ubuntu user: bash ai_setup.sh (sudo is used only where needed).'
[[ $(. /etc/os-release; printf '%s' "$ID") == ubuntu ]] || die 'Ubuntu is required.'
[[ $(. /etc/os-release; printf '%s' "$VERSION_ID") == 24.04 ]] || die 'This installer targets Ubuntu 24.04.'
[[ $(uname -m) == x86_64 ]] || die 'This installer targets x86_64.'
command -v sudo >/dev/null || die 'sudo is required.'
sudo -v
sudo install -d -m 700 /var/backups/ai_setup

# The old installer wrote https://docker.com as an APT repository. It has no
# Release file, so every apt-get update fails until that exact entry is disabled.
for REPO_FILE in /etc/apt/sources.list /etc/apt/sources.list.d/*.list; do
  [[ -f $REPO_FILE ]] || continue
  if sudo grep -Eq '^[[:space:]]*deb[[:space:]].*https?://(www\.)?docker\.com([[:space:]/]|$)' "$REPO_FILE"; then
    info "Disabling invalid legacy Docker repository in $REPO_FILE"
    sudo sed -i.ai_setup_backup -E \
      '/^[[:space:]]*deb[[:space:]].*https?:\/\/(www\.)?docker\.com([[:space:]\/]|$)/ s/^/# disabled by ai_setup: /' \
      "$REPO_FILE"
    sudo mv "$REPO_FILE.ai_setup_backup" \
      "/var/backups/ai_setup/$(basename "$REPO_FILE").$(date +%s%N)"
  fi
done
for REPO_FILE in /etc/apt/sources.list.d/*.sources; do
  [[ -f $REPO_FILE ]] || continue
  if sudo grep -Eq '^URIs:[[:space:]]*https?://(www\.)?docker\.com([[:space:]/]|$)' "$REPO_FILE"; then
    info "Disabling invalid legacy Docker repository in $REPO_FILE"
    sudo mv "$REPO_FILE" "/var/backups/ai_setup/$(basename "$REPO_FILE").$(date +%s%N)"
  fi
done
for BACKUP_FILE in /etc/apt/sources.list.d/*.ai_setup_backup; do
  [[ -f $BACKUP_FILE ]] || continue
  sudo mv "$BACKUP_FILE" "/var/backups/ai_setup/$(basename "$BACKUP_FILE").$(date +%s%N)"
done

INSTALL_DIR="${AI_SETUP_DIR:-$HOME/ai_setup}"
MODEL_CACHE="${AI_MODEL_CACHE:-$HOME/ai_models/huggingface}"
PORT="${AI_PORT:-8080}"
WEB_PORT="${AI_WEB_PORT:-3000}"
CONTROL_PORT="${AI_CONTROL_PORT:-3001}"
COMFY_PORT="${AI_COMFY_PORT:-8188}"
MEDIA_DIR="${AI_MEDIA_DIR:-$HOME/ai_media}"
[[ $PORT =~ ^[0-9]+$ ]] && (( PORT >= 1024 && PORT <= 65535 )) || die 'AI_PORT must be 1024..65535.'
[[ $WEB_PORT =~ ^[0-9]+$ ]] && (( WEB_PORT >= 1024 && WEB_PORT <= 65535 )) || die 'AI_WEB_PORT must be 1024..65535.'
for p in "$CONTROL_PORT" "$COMFY_PORT"; do
  [[ $p =~ ^[0-9]+$ ]] && (( p >= 1024 && p <= 65535 )) || die 'AI_CONTROL_PORT and AI_COMFY_PORT must be 1024..65535.'
done
(( PORT != WEB_PORT && PORT != CONTROL_PORT && PORT != COMFY_PORT && WEB_PORT != CONTROL_PORT && WEB_PORT != COMFY_PORT && CONTROL_PORT != COMFY_PORT )) || die 'API, WebUI, switch, and ComfyUI ports must differ.'
mkdir -p "$INSTALL_DIR/secure_proxy" "$INSTALL_DIR/comfyui" "$INSTALL_DIR/gpu_control" "$MODEL_CACHE" \
  "$MEDIA_DIR/models" "$MEDIA_DIR/input" "$MEDIA_DIR/output" "$MEDIA_DIR/user" "$MEDIA_DIR/temp"
chmod 700 "$INSTALL_DIR/secure_proxy"

info 'Detecting available compute hardware'
NVIDIA_PCI=0
for device in /sys/bus/pci/devices/*; do
  [[ -f $device/vendor && -f $device/class ]] || continue
  [[ $(<"$device/vendor") == 0x10de && $(<"$device/class") == 0x03* ]] && NVIDIA_PCI=1
done
GPU_NAME='none'
GPU_VRAM_MIB=0
GPU_COMPUTE_CAP=0
GPU_CUDA_READY=0
if command -v nvidia-smi >/dev/null && nvidia-smi >/dev/null 2>&1; then
  GPU_DETAILS=$(nvidia-smi --query-gpu=name,memory.total,compute_cap,driver_version --format=csv,noheader,nounits 2>/dev/null | head -n 1 || true)
  IFS=, read -r GPU_NAME GPU_VRAM_MIB GPU_COMPUTE_CAP DRIVER_VERSION <<< "$GPU_DETAILS"
  GPU_NAME=${GPU_NAME## }
  GPU_VRAM_MIB=${GPU_VRAM_MIB//[[:space:]]/}
  GPU_COMPUTE_CAP=${GPU_COMPUTE_CAP//[[:space:]]/}
  DRIVER_VERSION=${DRIVER_VERSION//[[:space:]]/}
  if [[ $GPU_VRAM_MIB =~ ^[0-9]+$ && $GPU_COMPUTE_CAP =~ ^[0-9]+\.[0-9]+$ && $DRIVER_VERSION =~ ^[0-9]+(\.[0-9]+)+$ ]] && dpkg --compare-versions "$GPU_COMPUTE_CAP" ge 7.5; then
    if dpkg --compare-versions "$DRIVER_VERSION" lt 580.65.06; then
      sudo apt-get update
      sudo apt-get install -y ubuntu-drivers-common
      sudo ubuntu-drivers install
      printf '\nUpdated NVIDIA driver for CUDA 13. Reboot, then run this script again.\n'
      exit 0
    fi
    GPU_CUDA_READY=1
  else
    GPU_VRAM_MIB=0
    info "GPU $GPU_NAME cannot run the included CUDA 13 profiles; CPU models remain available"
  fi
elif (( NVIDIA_PCI )); then
  sudo apt-get update
  sudo apt-get install -y ubuntu-drivers-common
  sudo ubuntu-drivers install
  printf '\nNVIDIA driver installed. Reboot, then run this script again.\n'
  exit 0
else
  info 'No NVIDIA GPU detected; installing a CPU inference profile'
fi
HOST_RAM_GIB=$(awk '/^MemTotal:/ {printf "%.1f", $2 / 1048576}' /proc/meminfo)
[[ -n $HOST_RAM_GIB ]] || die 'Could not read host RAM from /proc/meminfo.'
info "Ubuntu reports $HOST_RAM_GIB GiB of usable RAM"

info 'Installing Docker Engine and Compose from the official repository'
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg openssl python3 iproute2
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  sudo chmod a+r /etc/apt/keyrings/docker.asc
  sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
  sudo apt-get update
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
sudo systemctl enable --now docker

if (( GPU_CUDA_READY )); then
  info 'Installing NVIDIA Container Toolkit'
  if ! dpkg-query -W -f='${Status}' nvidia-container-toolkit 2>/dev/null | grep -q 'install ok installed'; then
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
      sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
      sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
    sudo apt-get update
    sudo apt-get install -y nvidia-container-toolkit
  fi
  sudo nvidia-ctk runtime configure --runtime=docker
  sudo systemctl restart docker
fi

info 'Writing the Compose stack, Open WebUI, and nginx proxy'
cat > "$INSTALL_DIR/docker-compose.yml" <<'COMPOSE'
name: local-ai
services:
  llm:
    image: vllm/vllm-openai:v0.32.0-ubuntu2404
    container_name: local-ai-llm
    restart: unless-stopped
    ipc: host
    environment:
      HF_HOME: /root/.cache/huggingface
    volumes:
      - ${AI_MODEL_CACHE:?set AI_MODEL_CACHE in .env}:/root/.cache/huggingface
      - vllm_cache:/root/.cache/vllm
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    command:
      - --model
      - cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit
      - --served-model-name
      - local-coder
      - --host
      - 0.0.0.0
      - --port
      - '8000'
      - --gpu-memory-utilization
      - "${AI_GPU_MEMORY_UTILIZATION:-0.95}"
      - --max-model-len
      - "${AI_CONTEXT_TOKENS:-32768}"
      - --max-num-seqs
      - '1'
      - --cpu-offload-gb
      - "${AI_CPU_OFFLOAD_GB:-0}"
      - --enable-auto-tool-choice
      - --tool-call-parser
      - qwen3_coder
      - --api-key
      - ${AI_API_KEY:?set AI_API_KEY in .env}
    networks: [ai_internal]
  huihui:
    profiles: [huihui]
    image: ghcr.io/ggml-org/llama.cpp:server-cuda13
    container_name: local-ai-huihui
    restart: unless-stopped
    environment:
      LLAMA_API_KEY: ${AI_API_KEY:?set AI_API_KEY in .env}
    volumes:
      - ${AI_MODEL_CACHE:?set AI_MODEL_CACHE in .env}/gguf:/models:ro
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    command:
      - --model
      - /models/huihui-qwen3.6-35b-a3b-q3_k.gguf
      - --alias
      - local-huihui
      - --host
      - 0.0.0.0
      - --port
      - '8080'
      - --ctx-size
      - "${AI_HUIHUI_CONTEXT_TOKENS:-32768}"
      - --n-gpu-layers
      - auto
      - --fit-target
      - '2048'
      - --flash-attn
      - 'on'
      - --parallel
      - '1'
      - --spec-type
      - draft-mtp
      - --spec-draft-n-max
      - '4'
    networks: [ai_internal]
  vision:
    profiles: [vision]
    image: vllm/vllm-openai:v0.32.0-ubuntu2404
    container_name: local-ai-vision
    restart: unless-stopped
    ipc: host
    environment:
      HF_HOME: /root/.cache/huggingface
    volumes:
      - ${AI_MODEL_CACHE:?set AI_MODEL_CACHE in .env}:/root/.cache/huggingface
      - vllm_cache:/root/.cache/vllm
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    command:
      - --model
      - QuantTrio/Qwen3-VL-30B-A3B-Instruct-AWQ
      - --served-model-name
      - local-vision
      - --host
      - 0.0.0.0
      - --port
      - '8000'
      - --gpu-memory-utilization
      - "${AI_VISION_GPU_UTILIZATION:-0.92}"
      - --max-model-len
      - "${AI_VISION_CONTEXT_TOKENS:-8192}"
      - --max-num-seqs
      - '1'
      - --enable-auto-tool-choice
      - --tool-call-parser
      - qwen3_coder
      - --api-key
      - ${AI_API_KEY:?set AI_API_KEY in .env}
    networks: [ai_internal]
  comfyui:
    profiles: [media]
    build:
      context: ./comfyui
    image: local-ai/comfyui:v0.43.0-cu130
    container_name: local-ai-comfyui
    restart: unless-stopped
    user: ${AI_UID:?set AI_UID in .env}:${AI_GID:?set AI_GID in .env}
    environment:
      HOME: /tmp
      XDG_CACHE_HOME: /tmp/.cache
    volumes:
      - ${AI_MEDIA_DIR:?set AI_MEDIA_DIR in .env}/models:/opt/ComfyUI/models
      - ${AI_MEDIA_DIR:?set AI_MEDIA_DIR in .env}/input:/opt/ComfyUI/input
      - ${AI_MEDIA_DIR:?set AI_MEDIA_DIR in .env}/output:/opt/ComfyUI/output
      - ${AI_MEDIA_DIR:?set AI_MEDIA_DIR in .env}/user:/opt/ComfyUI/user
      - ${AI_MEDIA_DIR:?set AI_MEDIA_DIR in .env}/temp:/opt/ComfyUI/temp
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8188/system_stats', timeout=5).close()"]
      interval: 15s
      timeout: 8s
      start_period: 180s
      retries: 3
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    networks: [ai_internal]
  open-webui:
    image: ghcr.io/open-webui/open-webui:main
    restart: unless-stopped
    environment:
      ENABLE_OLLAMA_API: "false"
      OPENAI_API_BASE_URL: http://nginx:8080/v1
      OPENAI_API_KEY: ${AI_API_KEY:?set AI_API_KEY in .env}
      OPENAI_API_BASE_URLS: http://nginx:8080/v1;http://vision:8000/v1
      OPENAI_API_KEYS: ${AI_API_KEY:?set AI_API_KEY in .env};${AI_API_KEY:?set AI_API_KEY in .env}
      WEBUI_SECRET_KEY: ${AI_WEBUI_SECRET_KEY:?set AI_WEBUI_SECRET_KEY in .env}
      WEBUI_URL: http://${AI_BIND_IP:?set AI_BIND_IP in .env}:${AI_WEB_PORT:-3000}
      CORS_ALLOW_ORIGIN: http://${AI_BIND_IP:?set AI_BIND_IP in .env}:${AI_WEB_PORT:-3000}
      ENABLE_WEB_SEARCH: "true"
      WEB_SEARCH_ENGINE: duckduckgo
      WEB_SEARCH_RESULT_COUNT: "5"
      WEB_FETCH_MAX_CONTENT_LENGTH: "12000"
      TERMINAL_SERVER_CONNECTIONS: '[{"id":"local-terminal","name":"Project workspace","enabled":true,"url":"http://open-terminal:8000","key":"${AI_TERMINAL_KEY:?set AI_TERMINAL_KEY in .env}","auth_type":"bearer","config":{"access_grants":[],"chat_uploads":"filesystem"}}]'
    volumes:
      - webui_data:/app/backend/data
    networks: [ai_internal]
  open-terminal:
    image: ghcr.io/open-webui/open-terminal:latest
    restart: unless-stopped
    environment:
      OPEN_TERMINAL_API_KEY: ${AI_TERMINAL_KEY:?set AI_TERMINAL_KEY in .env}
      OPEN_TERMINAL_MULTI_USER: "true"
    volumes:
      - terminal_home:/home
    networks: [ai_internal]
  gpu-control:
    image: python:3.12-alpine
    restart: unless-stopped
    environment:
      CONTROL_ORIGIN: http://${AI_BIND_IP:?set AI_BIND_IP in .env}:${AI_CONTROL_PORT:-3001}
      WEB_URL: http://${AI_BIND_IP:?set AI_BIND_IP in .env}:${AI_WEB_PORT:-3000}
      COMFY_URL: http://${AI_BIND_IP:?set AI_BIND_IP in .env}:${AI_COMFY_PORT:-8188}
      CONTROL_CSRF_TOKEN: ${AI_CONTROL_CSRF_TOKEN:?set AI_CONTROL_CSRF_TOKEN in .env}
    volumes:
      - ./gpu_control/server.py:/app/server.py:ro
      - /var/run/docker.sock:/var/run/docker.sock
    command: [python, /app/server.py]
    read_only: true
    networks: [control_internal]
  nginx:
    image: nginx:stable-alpine
    restart: unless-stopped
    ports:
      - ${AI_BIND_IP:?set AI_BIND_IP in .env}:${AI_PORT:-8080}:8080
      - ${AI_BIND_IP:?set AI_BIND_IP in .env}:${AI_WEB_PORT:-3000}:3000
      - ${AI_BIND_IP:?set AI_BIND_IP in .env}:${AI_CONTROL_PORT:-3001}:3001
      - ${AI_BIND_IP:?set AI_BIND_IP in .env}:${AI_COMFY_PORT:-8188}:8188
    volumes:
      - ./secure_proxy/nginx.conf:/etc/nginx/nginx.conf:ro
      - ./secure_proxy/.htpasswd:/etc/nginx/.htpasswd:ro
    depends_on: [open-webui, gpu-control]
    networks: [ai_internal, control_internal]
networks:
  ai_internal: {}
  control_internal: {}
volumes:
  vllm_cache: {}
  webui_data: {}
  terminal_home: {}
COMPOSE

cat > "$INSTALL_DIR/secure_proxy/nginx.conf" <<'NGINX'
events { worker_connections 1024; }
http {
    resolver 127.0.0.11 valid=10s ipv6=off;
    server {
        listen 8080;
        server_name _;
        client_max_body_size 16m;
        location = /_active_llm {
            internal;
            proxy_pass http://gpu-control:8001/route;
            proxy_pass_request_body off;
            proxy_set_header Content-Length "";
        }
        location / {
            auth_request /_active_llm;
            auth_request_set $llm_upstream $upstream_http_x_llm_backend;
            proxy_pass http://$llm_upstream;
            proxy_http_version 1.1;
            proxy_set_header Host $http_host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_set_header Connection "";
            proxy_buffering off;
            proxy_read_timeout 600s;
            proxy_send_timeout 600s;
        }
        location /vision/v1/ {
            set $vision_upstream vision:8000;
            rewrite ^/vision(/.*)$ $1 break;
            proxy_pass http://$vision_upstream;
            proxy_http_version 1.1;
            proxy_set_header Host $http_host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_set_header Connection "";
            proxy_buffering off;
            proxy_read_timeout 600s;
        }
    }
    server {
        listen 3000;
        server_name _;
        client_max_body_size 16m;
        location / {
            proxy_pass http://open-webui:8080;
            proxy_http_version 1.1;
            proxy_set_header Host $http_host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_set_header X-Forwarded-Host $http_host;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";
            proxy_buffering off;
            proxy_cache off;
            proxy_read_timeout 600s;
            proxy_send_timeout 600s;
        }
    }
    server {
        listen 3001;
        server_name _;
        auth_basic "GPU switch";
        auth_basic_user_file /etc/nginx/.htpasswd;
        location / {
            proxy_pass http://gpu-control:8001;
            proxy_http_version 1.1;
            proxy_set_header Host $http_host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_set_header Authorization "";
            proxy_buffering off;
        }
    }
    server {
        listen 8188;
        server_name _;
        auth_basic "ComfyUI";
        auth_basic_user_file /etc/nginx/.htpasswd;
        client_max_body_size 512m;
        location / {
            set $comfy_upstream comfyui:8188;
            proxy_pass http://$comfy_upstream;
            proxy_http_version 1.1;
            proxy_set_header Host $http_host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";
            proxy_set_header Authorization "";
            proxy_buffering off;
            proxy_read_timeout 3600s;
            proxy_send_timeout 3600s;
        }
    }
}
NGINX

cat > "$INSTALL_DIR/comfyui/Dockerfile" <<'COMFY_DOCKERFILE'
FROM nvidia/cuda:13.0.0-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates git python3 python3-pip python3-venv ffmpeg libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch v0.43.0 https://github.com/Comfy-Org/ComfyUI.git /opt/ComfyUI
RUN python3 -m venv /opt/venv \
    && pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir torch==2.9.1 torchvision==0.24.1 --index-url https://download.pytorch.org/whl/cu130 \
    && pip install --no-cache-dir -r /opt/ComfyUI/requirements.txt

WORKDIR /opt/ComfyUI
EXPOSE 8188
CMD ["python", "main.py", "--listen", "0.0.0.0", "--port", "8188"]
COMFY_DOCKERFILE

cat > "$INSTALL_DIR/gpu_control/server.py" <<'GPU_CONTROLLER'
#!/usr/bin/env python3
"""Small, fixed-action GPU mode switch. Only nginx should expose this service."""
import html
import http.client
import json
import os
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SOCKET = "/var/run/docker.sock"
LLM = "local-ai-llm"
HUIHUI = "local-ai-huihui"
VISION = "local-ai-vision"
MEDIA = "local-ai-comfyui"
ORIGIN = os.environ["CONTROL_ORIGIN"]
WEB_URL = os.environ["WEB_URL"]
COMFY_URL = os.environ["COMFY_URL"]
CSRF = os.environ["CONTROL_CSRF_TOKEN"]
LOCK = threading.Lock()


class UnixConnection(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("localhost", timeout=45)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(SOCKET)


def docker(method, path):
    conn = UnixConnection()
    try:
        conn.request(method, path, headers={"Host": "localhost", "Content-Length": "0"})
        resp = conn.getresponse()
        body = resp.read()
        if resp.status >= 400:
            raise RuntimeError(f"Docker API {resp.status}: {body[:240].decode('utf-8', 'replace')}")
        return json.loads(body) if body else None
    finally:
        conn.close()


def running(name):
    try:
        return bool(docker("GET", f"/containers/{name}/json")["State"]["Running"])
    except RuntimeError as exc:
        if "Docker API 404" in str(exc):
            return False
        raise


def workload_state(name):
    try:
        state = docker("GET", f"/containers/{name}/json")["State"]
    except RuntimeError as exc:
        if "Docker API 404" in str(exc):
            return "not created"
        raise
    status = state.get("Status", "unknown")
    if status == "running":
        return state.get("Health", {}).get("Status", "running")
    if status in ("exited", "dead"):
        return f"{status} (exit {state.get('ExitCode', '?')})"
    return status


def switch(target):
    workloads = {"llm": LLM, "huihui": HUIHUI, "vision": VISION, "media": MEDIA}
    if target not in workloads:
        raise ValueError("Unknown mode")
    with LOCK:
        start_name = workloads[target]
        previous = [name for name in workloads.values() if name != start_name and running(name)]
        for name in previous:
            docker("POST", f"/containers/{name}/stop?t=30")
        if not running(start_name):
            try:
                docker("POST", f"/containers/{start_name}/start")
            except Exception:
                for name in previous:
                    try:
                        docker("POST", f"/containers/{name}/start")
                    except Exception:
                        pass
                raise


class Handler(BaseHTTPRequestHandler):
    def _page(self, message="", status=200):
        try:
            llm_on, huihui_on, vision_on, media_on = (
                running(LLM), running(HUIHUI), running(VISION), running(MEDIA)
            )
            huihui_state = workload_state(HUIHUI)
            media_state = workload_state(MEDIA)
        except Exception as exc:
            llm_on = huihui_on = vision_on = media_on = False
            huihui_state = media_state = "unknown"
            message = f"Docker status error: {exc}"
            status = 503
        active = [label for label, on in (("Coder", llm_on), ("Huihui Qwen3.6", huihui_on), ("Vision", vision_on), ("ComfyUI", media_on)) if on]
        mode = active[0] if len(active) == 1 else "Neither" if not active else "Conflict: " + ", ".join(active)
        h = lambda s: html.escape(s, quote=True)
        page = f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>3090 GPU switch</title><style>body{{font:18px system-ui;margin:4rem auto;max-width:42rem;padding:0 1rem;background:#111827;color:#f9fafb}}a{{color:#93c5fd}}button{{font:inherit;padding:.8rem 1.2rem;margin:.4rem .5rem .4rem 0;cursor:pointer}}.card{{background:#1f2937;padding:1.5rem;border-radius:1rem}}.note{{color:#d1d5db}}select{{font:inherit;padding:.6rem;max-width:100%}}form{{margin:1rem 0}}</style>
<main class="card"><h1>3090 GPU switch</h1><p>Active workload: <strong>{h(mode)}</strong></p><p>Huihui status: <strong>{h(huihui_state)}</strong> · ComfyUI status: <strong>{h(media_state)}</strong> · <a href="/">Refresh status</a></p><p>{h(message)}</p>
<form method="post" action="/switch"><input type="hidden" name="token" value="{h(CSRF)}"><label for="model">LLM model</label><br><select id="model" name="mode"><option value="llm" {'selected' if llm_on else ''}>Qwen3 Coder 30B-A3B (local-coder)</option><option value="huihui" {'selected' if huihui_on else ''}>Huihui Qwen3.6 35B-A3B Q3_K (local-huihui)</option><option value="vision" {'selected' if vision_on else ''}>Qwen3-VL 30B-A3B (local-vision)</option></select><button type="submit">Use selected model</button></form>
<form method="post" action="/switch"><input type="hidden" name="token" value="{h(CSRF)}"><button name="mode" value="media">Use ComfyUI</button></form>
<p class="note">Selecting a model stops the other GPU workloads and routes the existing /v1 API URL to it. In Open WebUI, choose the matching model name after it starts. Switching interrupts active requests. Check ai-control.sh huihui-logs or media-logs if a service fails to start.</p>
<p class="note">If an older Open WebUI installation still shows local-coder after switching, set its saved OpenAI connection URL to http://nginx:8080/v1 in Admin → Settings → Connections, then refresh.</p>
<p><a href="{h(WEB_URL)}">Open WebUI</a> · <a href="{h(COMFY_URL)}">ComfyUI</a></p></main></html>"""
        data = page.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/route":
            try:
                backend = "huihui:8080" if running(HUIHUI) else "llm:8000"
            except Exception:
                self.send_error(503, "Docker status unavailable")
                return
            self.send_response(200)
            self.send_header("X-LLM-Backend", backend)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path != "/":
            self.send_error(404)
            return
        self._page()

    def do_POST(self):
        if self.path != "/switch":
            self.send_error(404)
            return
        if self.headers.get("Origin") != ORIGIN or self.headers.get("Host") != urllib.parse.urlsplit(ORIGIN).netloc:
            self.send_error(403, "Invalid origin")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400)
            return
        if not 1 <= length <= 1024 or self.headers.get("Content-Type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
            self.send_error(400)
            return
        fields = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"), strict_parsing=True)
        if fields.get("token") != [CSRF] or fields.get("mode") not in (["llm"], ["huihui"], ["vision"], ["media"]):
            self.send_error(403, "Invalid request")
            return
        mode = fields["mode"][0]
        try:
            switch(mode)
            self._page(f"Start requested for {mode}. Refresh status before opening the service.")
        except Exception as exc:
            self._page(f"Switch failed: {exc}", 500)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8001), Handler).serve_forever()
GPU_CONTROLLER

cat > "$INSTALL_DIR/media-models.sh" <<'MEDIA_MODELS'
#!/usr/bin/env bash
# Optional, resumable downloads for the ComfyUI image and video templates.
set -Eeuo pipefail
DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[[ -f $DIR/.env ]] || { echo 'Run ai_setup.sh first.' >&2; exit 1; }
MEDIA_DIR=$(sed -n 's/^AI_MEDIA_DIR=//p' "$DIR/.env" | head -n 1)
[[ $MEDIA_DIR == /* ]] || { echo 'AI_MEDIA_DIR must be absolute.' >&2; exit 1; }
ROOT="$MEDIA_DIR/models"

download() {
  local subdir=$1 filename=$2 url=$3 dest="$ROOT/$1/$2" partial="$ROOT/$1/$2.part"
  mkdir -p "$ROOT/$subdir"
  if [[ -s $dest ]]; then
    printf 'Already present: %s\n' "$dest"
    return
  fi
  printf '\nDownloading %s\nPartial file: %s\n' "$filename" "$partial"
  curl --fail --location --retry 5 --retry-delay 3 --continue-at - --output "$partial" "$url"
  [[ -s $partial ]] || { echo "Empty download: $partial" >&2; exit 1; }
  mv "$partial" "$dest"
}

image() {
  download checkpoints flux1-schnell-fp8.safetensors \
    'https://huggingface.co/Comfy-Org/Flux-1-Schnell/resolve/main/flux1-schnell-fp8.safetensors'
}
video() {
  local base='https://huggingface.co/Comfy-Org/Wan-2.2/resolve/main/split_files'
  download diffusion_models wan2.2_ti2v_5B_fp16.safetensors "$base/diffusion_models/wan2.2_ti2v_5B_fp16.safetensors"
  download text_encoders umt5_xxl_fp8_e4m3fn_scaled.safetensors "$base/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors"
  download vae wan2.2_vae.safetensors "$base/vae/wan2.2_vae.safetensors"
}

case "${1:-help}" in
  image) image ;;
  video) video ;;
  all) image; video ;;
  *) echo 'Usage: media-models.sh {image|video|all}' >&2; exit 2 ;;
esac
MEDIA_MODELS
chmod 755 "$INSTALL_DIR/media-models.sh"

ENV_FILE="$INSTALL_DIR/.env"
if [[ ! -e $ENV_FILE ]]; then
  BIND_IP="${AI_BIND_IP:-$(ip -4 route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i=="src") {print $(i+1); exit}}')}"
  python3 - "$BIND_IP" <<'PY'
import ipaddress, sys
try:
    ip = ipaddress.ip_address(sys.argv[1])
    if ip.version != 4 or not ip.is_private or ip.is_loopback or ip.is_unspecified or ip.is_link_local:
        raise ValueError('expected a private IPv4 LAN address')
except ValueError as exc:
    sys.exit(f'Cannot bind the LAN proxy: {exc}. Set AI_BIND_IP to the host LAN address and rerun.')
PY
  umask 077
  printf 'AI_MODEL_CACHE=%s\nAI_BIND_IP=%s\nAI_PORT=%s\nAI_WEB_PORT=%s\nAI_CONTROL_PORT=%s\nAI_COMFY_PORT=%s\nAI_MEDIA_DIR=%s\nAI_UID=%s\nAI_GID=%s\nAI_CONTEXT_TOKENS=%s\nAI_GPU_MEMORY_UTILIZATION=%s\nAI_CPU_OFFLOAD_GB=%s\nAI_VISION_CONTEXT_TOKENS=%s\nAI_VISION_GPU_UTILIZATION=%s\nAI_API_KEY=%s\nAI_WEBUI_SECRET_KEY=%s\nAI_TERMINAL_KEY=%s\nAI_SWITCH_PASSWORD=%s\nAI_CONTROL_CSRF_TOKEN=%s\n' \
    "$MODEL_CACHE" "$BIND_IP" "$PORT" "$WEB_PORT" "$CONTROL_PORT" "$COMFY_PORT" "$MEDIA_DIR" \
    "$(id -u)" "$(id -g)" "${AI_CONTEXT_TOKENS:-32768}" "${AI_GPU_MEMORY_UTILIZATION:-0.95}" "${AI_CPU_OFFLOAD_GB:-0}" "${AI_VISION_CONTEXT_TOKENS:-8192}" "${AI_VISION_GPU_UTILIZATION:-0.92}" \
    "$(openssl rand -hex 32)" "$(openssl rand -hex 32)" "$(openssl rand -hex 32)" "$(openssl rand -hex 24)" "$(openssl rand -hex 32)" > "$ENV_FILE"
fi
# Older installations have no WebUI settings. Preserve their API key and add only the new values.
chmod 600 "$ENV_FILE"
if ! grep -q '^AI_WEB_PORT=' "$ENV_FILE"; then
  printf 'AI_WEB_PORT=%s\n' "$WEB_PORT" >> "$ENV_FILE"
fi
if ! grep -q '^AI_WEBUI_SECRET_KEY=' "$ENV_FILE"; then
  printf 'AI_WEBUI_SECRET_KEY=%s\n' "$(openssl rand -hex 32)" >> "$ENV_FILE"
fi
if ! grep -q '^AI_TERMINAL_KEY=' "$ENV_FILE"; then
  printf 'AI_TERMINAL_KEY=%s\n' "$(openssl rand -hex 32)" >> "$ENV_FILE"
fi
if ! grep -q '^AI_CONTROL_PORT=' "$ENV_FILE"; then
  printf 'AI_CONTROL_PORT=%s\n' "$CONTROL_PORT" >> "$ENV_FILE"
fi
if ! grep -q '^AI_COMFY_PORT=' "$ENV_FILE"; then
  printf 'AI_COMFY_PORT=%s\n' "$COMFY_PORT" >> "$ENV_FILE"
fi
if ! grep -q '^AI_MEDIA_DIR=' "$ENV_FILE"; then
  printf 'AI_MEDIA_DIR=%s\n' "$MEDIA_DIR" >> "$ENV_FILE"
fi
if ! grep -q '^AI_SWITCH_PASSWORD=' "$ENV_FILE"; then
  printf 'AI_SWITCH_PASSWORD=%s\n' "$(openssl rand -hex 24)" >> "$ENV_FILE"
fi
if ! grep -q '^AI_CONTROL_CSRF_TOKEN=' "$ENV_FILE"; then
  printf 'AI_CONTROL_CSRF_TOKEN=%s\n' "$(openssl rand -hex 32)" >> "$ENV_FILE"
fi
if ! grep -q '^AI_UID=' "$ENV_FILE"; then
  printf 'AI_UID=%s\n' "$(id -u)" >> "$ENV_FILE"
fi
if ! grep -q '^AI_GID=' "$ENV_FILE"; then
  printf 'AI_GID=%s\n' "$(id -g)" >> "$ENV_FILE"
fi
if ! grep -q '^AI_CONTEXT_TOKENS=' "$ENV_FILE"; then
  printf 'AI_CONTEXT_TOKENS=%s\n' "${AI_CONTEXT_TOKENS:-32768}" >> "$ENV_FILE"
fi
if ! grep -q '^AI_GPU_MEMORY_UTILIZATION=' "$ENV_FILE"; then
  printf 'AI_GPU_MEMORY_UTILIZATION=%s\n' "${AI_GPU_MEMORY_UTILIZATION:-0.95}" >> "$ENV_FILE"
fi
if ! grep -q '^AI_CPU_OFFLOAD_GB=' "$ENV_FILE"; then
  printf 'AI_CPU_OFFLOAD_GB=%s\n' "${AI_CPU_OFFLOAD_GB:-0}" >> "$ENV_FILE"
fi
if ! grep -q '^AI_VISION_CONTEXT_TOKENS=' "$ENV_FILE"; then
  printf 'AI_VISION_CONTEXT_TOKENS=%s\n' "${AI_VISION_CONTEXT_TOKENS:-8192}" >> "$ENV_FILE"
fi
if ! grep -q '^AI_VISION_GPU_UTILIZATION=' "$ENV_FILE"; then
  printf 'AI_VISION_GPU_UTILIZATION=%s\n' "${AI_VISION_GPU_UTILIZATION:-0.92}" >> "$ENV_FILE"
fi
if ! grep -q '^AI_HUIHUI_CONTEXT_TOKENS=' "$ENV_FILE"; then
  printf 'AI_HUIHUI_CONTEXT_TOKENS=%s\n' "${AI_HUIHUI_CONTEXT_TOKENS:-32768}" >> "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"
python3 - "$ENV_FILE" <<'PY'
import ipaddress, pathlib, sys
values = dict(line.split('=', 1) for line in pathlib.Path(sys.argv[1]).read_text().splitlines() if '=' in line and not line.startswith('#'))
ports = []
for name in ('AI_PORT', 'AI_WEB_PORT', 'AI_CONTROL_PORT', 'AI_COMFY_PORT'):
    value = values.get(name, '')
    if not value.isdecimal() or not 1024 <= int(value) <= 65535:
        sys.exit(f'{name} in .env must be a port from 1024 to 65535')
    ports.append(int(value))
if len(set(ports)) != len(ports):
    sys.exit('The four ports in .env must be different')
ip = ipaddress.ip_address(values.get('AI_BIND_IP', ''))
if ip.version != 4 or not ip.is_private or ip.is_loopback or ip.is_link_local:
    sys.exit('AI_BIND_IP in .env must be a private LAN IPv4 address')
if not pathlib.Path(values.get('AI_MEDIA_DIR', '')).is_absolute():
    sys.exit('AI_MEDIA_DIR in .env must be an absolute path')
context = values.get('AI_CONTEXT_TOKENS', '')
if not context.isdecimal() or not 8192 <= int(context) <= 262144:
    sys.exit('AI_CONTEXT_TOKENS in .env must be 8192..262144')
vision_context = values.get('AI_VISION_CONTEXT_TOKENS', '')
if not vision_context.isdecimal() or not 2048 <= int(vision_context) <= 65536:
    sys.exit('AI_VISION_CONTEXT_TOKENS in .env must be 2048..65536')
huihui_context = values.get('AI_HUIHUI_CONTEXT_TOKENS', '')
if not huihui_context.isdecimal() or not 4096 <= int(huihui_context) <= 262144:
    sys.exit('AI_HUIHUI_CONTEXT_TOKENS in .env must be 4096..262144')
try:
    gpu_util = float(values.get('AI_GPU_MEMORY_UTILIZATION', ''))
    vision_gpu_util = float(values.get('AI_VISION_GPU_UTILIZATION', ''))
    offload = float(values.get('AI_CPU_OFFLOAD_GB', ''))
except ValueError:
    sys.exit('GPU utilization and CPU offload must be numbers')
if not 0.5 <= gpu_util < 1 or not 0.5 <= vision_gpu_util < 1 or not 0 <= offload <= 64:
    sys.exit('AI_GPU_MEMORY_UTILIZATION and AI_VISION_GPU_UTILIZATION must be 0.5..<1; AI_CPU_OFFLOAD_GB must be 0..64 GiB')
PY
ACTUAL_MEDIA_DIR=$(sed -n 's/^AI_MEDIA_DIR=//p' "$ENV_FILE" | head -n 1)
[[ $ACTUAL_MEDIA_DIR == /* ]] || die 'AI_MEDIA_DIR in .env must be an absolute path.'
mkdir -p "$ACTUAL_MEDIA_DIR/models" "$ACTUAL_MEDIA_DIR/input" "$ACTUAL_MEDIA_DIR/output" "$ACTUAL_MEDIA_DIR/user" "$ACTUAL_MEDIA_DIR/temp"
ACTUAL_MODEL_CACHE=$(sed -n 's/^AI_MODEL_CACHE=//p' "$ENV_FILE" | head -n 1)
[[ $ACTUAL_MODEL_CACHE == /* ]] || die 'AI_MODEL_CACHE in .env must be an absolute path.'
CPU_THREADS=$(nproc)
(( CPU_THREADS > 2 )) && CPU_THREADS=$((CPU_THREADS - 2))
if ! grep -q '^AI_CPU_THREADS=' "$ENV_FILE"; then
  printf 'AI_CPU_THREADS=%s\n' "$CPU_THREADS" >> "$ENV_FILE"
fi
if grep -q '^AI_GPU_COMPATIBLE=' "$ENV_FILE"; then
  sed -i "s/^AI_GPU_COMPATIBLE=.*/AI_GPU_COMPATIBLE=$GPU_CUDA_READY/" "$ENV_FILE"
else
  printf 'AI_GPU_COMPATIBLE=%s\n' "$GPU_CUDA_READY" >> "$ENV_FILE"
fi
info 'Selecting models for detected hardware'
python3 "$INSTALL_DIR/gpu_control/install_profile.py" "$ENV_FILE" "$HOST_RAM_GIB" "$GPU_VRAM_MIB"
bash "$INSTALL_DIR/gpu_control/install_models.sh" "$INSTALL_DIR"
SWITCH_PASSWORD=$(sed -n 's/^AI_SWITCH_PASSWORD=//p' "$ENV_FILE" | head -n 1)
printf 'operator:%s\n' "$(printf '%s' "$SWITCH_PASSWORD" | openssl passwd -apr1 -stdin)" > "$INSTALL_DIR/secure_proxy/.htpasswd"
# Nginx's unprivileged worker reads this file on each authenticated request.
# It contains a password hash, not the plaintext password.
chmod 644 "$INSTALL_DIR/secure_proxy/.htpasswd"

cat > "$INSTALL_DIR/ai-control.sh" <<'CONTROL'
#!/usr/bin/env bash
set -Eeuo pipefail
DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[[ -f $DIR/.env && -f $DIR/docker-compose.yml ]] || { echo 'Missing .env or docker-compose.yml. Run ai_setup.sh first.' >&2; exit 1; }
cd "$DIR"
docker_cmd() { sudo docker compose --env-file .env "$@"; }
port=$(sed -n 's/^AI_PORT=//p' .env | head -n 1)
web_port=$(sed -n 's/^AI_WEB_PORT=//p' .env | head -n 1)
control_port=$(sed -n 's/^AI_CONTROL_PORT=//p' .env | head -n 1)
comfy_port=$(sed -n 's/^AI_COMFY_PORT=//p' .env | head -n 1)
bind_ip=$(sed -n 's/^AI_BIND_IP=//p' .env | head -n 1)
key=$(sed -n 's/^AI_API_KEY=//p' .env | head -n 1)
case "${1:-help}" in
  start)
    docker_cmd --profile media --profile vision --profile huihui stop comfyui vision huihui
    docker_cmd up -d
    docker_cmd --profile media create comfyui
    docker_cmd --profile vision create vision
    docker_cmd --profile huihui create huihui
    echo "API: http://$bind_ip:$port/v1"
    echo "WebUI: http://$bind_ip:$web_port"
    echo "GPU switch: http://$bind_ip:$control_port"
    ;;
  stop) docker_cmd --profile media --profile vision --profile huihui down ;;
  restart)
    docker_cmd --profile media --profile vision --profile huihui down
    docker_cmd up -d
    docker_cmd --profile media create comfyui
    docker_cmd --profile vision create vision
    docker_cmd --profile huihui create huihui
    ;;
  status) docker_cmd --profile media --profile vision --profile huihui ps -a; nvidia-smi ;;
  context)
    printf 'Context: %s tokens (prompt and output combined)\n' "$(sed -n 's/^AI_CONTEXT_TOKENS=//p' .env | head -n 1)"
    printf 'GPU memory utilization: %s\n' "$(sed -n 's/^AI_GPU_MEMORY_UTILIZATION=//p' .env | head -n 1)"
    printf 'CPU weight offload: %s GiB\n' "$(sed -n 's/^AI_CPU_OFFLOAD_GB=//p' .env | head -n 1)"
    printf 'Vision context: %s tokens\n' "$(sed -n 's/^AI_VISION_CONTEXT_TOKENS=//p' .env | head -n 1)"
    printf 'Huihui context: %s tokens\n' "$(sed -n 's/^AI_HUIHUI_CONTEXT_TOKENS=//p' .env | head -n 1)"
    ;;
  logs) docker_cmd logs -f --tail=100 llm ;;
  huihui-logs) docker_cmd --profile huihui logs -f --tail=100 huihui ;;
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
  help|-h|--help) echo 'Usage: ai-control.sh {start|stop|restart|status|context|logs|huihui-logs|huihui-files|huihui-test|vision-logs|vision-test|media-logs|web-logs|web-test|terminal-test|test|tool-test|codex-test|key|terminal-key|switch-password|switch-url|comfy-url}' ;;
  *) echo "Unknown command: $1" >&2; exit 2 ;;
esac
CONTROL
chmod 755 "$INSTALL_DIR/ai-control.sh"

info 'Building ComfyUI and starting the LLM (downloads may take a while)'
cd "$INSTALL_DIR"
sudo docker compose --env-file .env config -q
sudo docker compose --env-file .env --profile media build comfyui
sudo docker compose --env-file .env --profile media --profile vision --profile huihui stop comfyui vision huihui
sudo docker compose --env-file .env up -d
sudo docker compose --env-file .env --profile media create --force-recreate comfyui
sudo docker compose --env-file .env --profile vision create vision
sudo docker compose --env-file .env --profile huihui create huihui
READY_PORT=$(sed -n 's/^AI_PORT=//p' .env | head -n 1)
READY_WEB_PORT=$(sed -n 's/^AI_WEB_PORT=//p' .env | head -n 1)
READY_CONTROL_PORT=$(sed -n 's/^AI_CONTROL_PORT=//p' .env | head -n 1)
READY_COMFY_PORT=$(sed -n 's/^AI_COMFY_PORT=//p' .env | head -n 1)
READY_BIND_IP=$(sed -n 's/^AI_BIND_IP=//p' .env | head -n 1)
READY_KEY=$(sed -n 's/^AI_API_KEY=//p' .env | head -n 1)
READY_SERVICE=$(sed -n 's/^AI_START_MODEL=//p' .env | head -n 1)
case "$READY_SERVICE" in
  cpu-tiny) READY_ALIAS=local-cpu-tiny; READY_COMPOSE_SERVICE=cpu-tiny ;;
  cpu-4b) READY_ALIAS=local-cpu-4b; READY_COMPOSE_SERVICE=cpu-4b ;;
  llm) READY_ALIAS=local-coder; READY_COMPOSE_SERVICE=llm ;;
  qwen4b) READY_ALIAS=local-qwen4b; READY_COMPOSE_SERVICE=qwen4b ;;
  huihui) READY_ALIAS=local-huihui; READY_COMPOSE_SERVICE=huihui ;;
  flint-iq4) READY_ALIAS=local-flint-iq4; READY_COMPOSE_SERVICE=flint-iq4 ;;
  flint-q3) READY_ALIAS=local-flint-q3; READY_COMPOSE_SERVICE=flint-q3 ;;
  deepseek) READY_ALIAS=local-deepseek-v4-distill; READY_COMPOSE_SERVICE=deepseek-v4-distill ;;
  qwen38) READY_ALIAS=local-qwen38-ablit; READY_COMPOSE_SERVICE=qwen38-ablit ;;
  gemma4) READY_ALIAS=local-gemma4-ablit; READY_COMPOSE_SERVICE=gemma4-ablit ;;
  vision) READY_ALIAS=local-vision; READY_COMPOSE_SERVICE=vision ;;
  *) die "Unknown initial workload: $READY_SERVICE" ;;
esac
info 'Waiting for the model API to become ready'
ready=0
for attempt in $(seq 1 180); do
  if curl -fsS --max-time 5 -H "Authorization: Bearer $READY_KEY" \
      "http://$READY_BIND_IP:$READY_PORT/v1/models" 2>/dev/null | grep -Fq "\"$READY_ALIAS\""; then
    ready=1
    break
  fi
  if ! sudo docker compose --env-file .env --profile coder --profile cpu-tiny --profile cpu-4b --profile qwen4b --profile huihui --profile flint --profile deepseek --profile qwen38 --profile gemma4 --profile vision ps --status running --services | grep -qx "$READY_COMPOSE_SERVICE"; then
    sudo docker compose --env-file .env --profile coder --profile cpu-tiny --profile cpu-4b --profile qwen4b --profile huihui --profile flint --profile deepseek --profile qwen38 --profile gemma4 --profile vision logs --tail=80 "$READY_COMPOSE_SERVICE" >&2
    die 'The LLM container stopped before becoming ready.'
  fi
  if (( attempt % 6 == 0 )); then printf 'Still loading model (%s minutes)...\n' "$((attempt / 6))"; fi
  sleep 10
done
if (( ! ready )); then
  sudo docker compose --env-file .env --profile coder --profile cpu-tiny --profile cpu-4b --profile qwen4b --profile huihui --profile flint --profile deepseek --profile qwen38 --profile gemma4 --profile vision logs --tail=80 "$READY_COMPOSE_SERVICE" >&2
  die 'The LLM did not become ready within 30 minutes. Check logs and available disk space.'
fi
info 'Waiting for Open WebUI health endpoint to become ready'
web_ready=0
for attempt in $(seq 1 180); do
  if curl -fsS --max-time 5 -o /dev/null "http://$READY_BIND_IP:$READY_WEB_PORT/health" 2>/dev/null; then
    web_ready=1
    break
  fi
  if ! sudo docker compose --env-file .env ps --status running --services | grep -qx open-webui; then
    sudo docker compose --env-file .env logs --tail=80 open-webui >&2
    die 'Open WebUI stopped before becoming ready.'
  fi
  if ! sudo docker compose --env-file .env ps --status running --services | grep -qx nginx; then
    sudo docker compose --env-file .env logs --tail=80 nginx >&2
    die 'The nginx proxy stopped before Open WebUI became ready.'
  fi
  if (( attempt % 12 == 0 )); then printf 'Open WebUI is still starting (%s minutes)...\n' "$((attempt / 12))"; fi
  sleep 5
done
if (( ! web_ready )); then
  printf 'Last WebUI health HTTP status: ' >&2
  curl -sS --max-time 10 -o /dev/null -w '%{http_code}\n' "http://$READY_BIND_IP:$READY_WEB_PORT/health" >&2 || true
  printf 'Direct container health: ' >&2
  sudo docker compose --env-file .env exec -T nginx wget -qO- -T 10 http://open-webui:8080/health >&2 || true
  printf '\n' >&2
  sudo docker compose --env-file .env ps -a >&2
  sudo docker compose --env-file .env logs --tail=120 open-webui nginx >&2
  die 'Open WebUI did not pass /health within 15 minutes. Inspect the status and logs above.'
fi
printf '\nModel ready. Verify a completion with: %s/ai-control.sh test\n' "$INSTALL_DIR"
printf 'Logs: %s/ai-control.sh logs\n' "$INSTALL_DIR"
printf 'LAN URL: http://%s:%s/v1\n' "$READY_BIND_IP" "$READY_PORT"
printf 'Vision URL when active: http://%s:%s/vision/v1\n' "$READY_BIND_IP" "$READY_PORT"
printf 'Web interface: http://%s:%s\n' "$READY_BIND_IP" "$READY_WEB_PORT"
printf 'Model switcher: http://%s:%s (user: operator; password: %s/ai-control.sh switch-password)\n' "$READY_BIND_IP" "$READY_CONTROL_PORT" "$INSTALL_DIR"
printf 'ComfyUI when media mode is active: http://%s:%s\n' "$READY_BIND_IP" "$READY_COMFY_PORT"
printf 'API key: %s/ai-control.sh key\n' "$INSTALL_DIR"
printf 'Model name: local-coder\n'
printf 'Chosen models: %s\n' "$(sed -n 's/^AI_SELECTED_MODELS=//p' .env | head -n 1)"

