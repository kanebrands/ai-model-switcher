# AI Model Switcher

An all-in-one Ubuntu 24.04 installation for a local NVIDIA RTX 3090. It runs an OpenAI-compatible LLM API, Open WebUI, ComfyUI, and a browser-based switcher behind Nginx. The switcher manages model selection, hardware-aware recommendations, benchmark history, context and generation-slot tuning, client limits, and live GPU metrics.

The **installer currently requires exactly one RTX 3090**. The switcher's recommendation and client-limit defaults use detected hardware rather than assuming a 3090. A different GPU needs installer and model-profile validation before installation.

## Quick start

On the Ubuntu host, copy the generated `ai-setup.sh` and run it as your normal user:

```bash
chmod +x ai-setup.sh
./ai-setup.sh
```

The first run may install a driver and ask for a reboot. Run the same command again after reboot. The script installs Docker and the NVIDIA container runtime, downloads the configured models, writes the Compose stack under `~/ai_setup` by default, and starts its services. Downloads can take considerable time and disk space.

The script prints the URLs when ready. With the default ports, substitute your Ubuntu host's LAN address:

| Service | URL |
| --- | --- |
| OpenAI-compatible API | `http://HOST:8080/v1` |
| Open WebUI | `http://HOST:3000` |
| AI Model Switcher | `http://HOST:3001` |
| ComfyUI, when its workload is active | `http://HOST:8188` |

The switcher and ComfyUI use HTTP Basic authentication. The username is `operator`; get the generated password from the installed controller with `~/ai_setup/ai-control.sh switch-password`. Get the existing API key with `~/ai_setup/ai-control.sh key`. The switcher can create separate, revocable keys for individual agents under **Client sharing**.

## What is in this repository

- `ai-setup.sh` is the generated, standalone installer. It embeds the Compose file, controller, gateway, assets, and helper scripts.
- `ai-setup.base.sh` and `build_installer.py` are the source and generator for that installer.
- `server.py` and its companion Python modules implement the switcher, catalog, benchmarks, updates, tuning, and API sharing.
- `fairness_gateway.py` forwards API requests with per-client limits and streaming support.
- `docker-compose.yml`, `nginx.conf`, and `comfyui.Dockerfile` define the installed services.
- `test_*.py` contains the local test suite.

For installation details, storage locations, and upgrades, see [Installation](docs/INSTALLATION.md). For daily use, see [Operations](docs/OPERATIONS.md). For the service map, see [Architecture](docs/ARCHITECTURE.md). For source changes and tests, see [Development](docs/DEVELOPMENT.md). For credentials and network exposure, see [Security](SECURITY.md).

## Project state

The generated installer and live stack were verified on one Ubuntu 24.04 host with a single RTX 3090. CI checks syntax, generation of the installer, and unit/integration tests without a GPU. Model download, driver installation, and actual GPU workload changes require an Ubuntu/NVIDIA host.
