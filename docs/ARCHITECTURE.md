# Architecture

```mermaid
flowchart LR
    Agent[API clients] --> Nginx[Nginx :8080]
    Browser[Operator browser] --> Nginx
    Nginx --> Gateway[Fairness gateway]
    Gateway --> Controller[Switcher controller]
    Gateway --> Engine[Active vLLM or llama.cpp model]
    Controller --> Docker[Docker socket]
    Docker --> Engine
    Nginx --> WebUI[Open WebUI :3000]
    Nginx --> Controller
    Nginx --> ComfyUI[ComfyUI :8188]
    Controller --> Database[(Benchmark and settings SQLite DB)]
    Gateway --> Database
```

Nginx exposes the LAN ports and protects the switcher and ComfyUI with Basic Auth. The API gateway checks a client key, applies the configured rate and concurrency limits, and streams accepted requests to the active model. It asks the controller which model backend is active. The controller manages containers through the Docker socket and stores benchmarks, installed-model metadata, client-key hashes, and tuning status in SQLite. The gateway writes per-key request timing and engine-reported token counts to the same database; it does not store API keys, prompts, or responses in usage records. A selected key's usage page aggregates that history into time buckets and charts.

Open WebUI connects to the API through Nginx. The installer offers CPU inference without a GPU and adds GPU profiles only when compatible hardware is detected. It records selected model IDs in `.env`, downloads only those model files, and creates their containers; one selected text or vision model starts initially. The switcher can search Hugging Face for single or sharded GGUF and standard Safetensors repositories. It verifies complete weight sets and checksums before creating a stopped llama.cpp or vLLM container. The controller detects NVIDIA through its container runtime probe and AMD/Intel render devices through host sysfs. GGUF uses CUDA, Vulkan, or CPU; Safetensors uses CUDA or CPU. A saved GPU + RAM preference, enabled by default, uses a conservative RAM/VRAM budget to select hybrid placement for new installs; vLLM receives a weight offload limit and llama.cpp uses automatic layer placement. Memory estimates can be overridden explicitly, while disk capacity, file integrity, and runtime support remain required. ComfyUI is an optional, separate GPU workload. The switcher stops conflicting workloads when changing the active model. Most data stays outside the source tree: model caches and media are host directories, Open WebUI has a Docker volume, and the SQLite database sits in the installed `gpu_control/results` directory.

An optional host-specific HTTPS listener can be included in Nginx for a restricted client such as a mail service. Its certificates, allowlist, and credentials are local configuration and are not included in this repository.
