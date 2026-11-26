# Installation and upgrades

## Requirements

- Ubuntu 24.04 on x86_64.
- Exactly one RTX 3090 for the current standalone installer.
- `sudo` access and internet access for packages, images, and model downloads.
- Enough free disk space for multiple model files, container images, and ComfyUI assets.

Run `ai-setup.sh` as a normal user, not root. It uses `sudo` for the steps that need it. If it installs or changes the NVIDIA driver, reboot and rerun the script. The script can be rerun to update generated files while preserving its existing `.env` and data directories.

```bash
chmod +x ai-setup.sh
./ai-setup.sh
```

For a custom installation root or model/media storage location, set the variables before the first run:

```bash
AI_SETUP_DIR="$HOME/ai_setup" \
AI_MODEL_CACHE="/mnt/shared/ai_models/huggingface" \
AI_MEDIA_DIR="/mnt/shared/ai_media" \
./ai-setup.sh
```

The installer creates `.env` in the installation root and generates credentials. Its default storage locations are:

| Data | Default host path |
| --- | --- |
| Compose stack and controller | `~/ai_setup` |
| Hugging Face cache and GGUF files | `~/ai_models/huggingface` and `~/ai_models/huggingface/gguf` |
| ComfyUI models, inputs, outputs, user data | `~/ai_media` |
| Benchmark and switcher database | `~/ai_setup/gpu_control/results/benchmarks.sqlite3` |
| Open WebUI data | Docker volume `local-ai_webui_data` |

Download progress for GGUF models is visible in the installer output and the model directory. ComfyUI model downloads use `.part` files until complete. The switcher's **Find models** page can install additional supported models after setup.

## Upgrading an existing host

Back up `~/ai_setup/.env`, `~/ai_setup/secure_proxy`, `~/ai_setup/gpu_control/results`, the Open WebUI Docker volume, and any irreplaceable media. Copy the newly generated `ai-setup.sh` to the host and rerun it as the same normal user. Review local Nginx or Compose customizations first: the installer preserves an existing Nginx config and applies only known API-route migrations, while it regenerates `docker-compose.yml`.

After installation, check:

```bash
cd ~/ai_setup
./ai-control.sh status
./ai-control.sh web-test
./ai-control.sh test
```

The installed API binds to the LAN IP selected at setup. Set `AI_BIND_IP` before the first run if automatic detection chooses the wrong interface. The installer validates it as a private IPv4 address.
