# Installation and upgrades

## Requirements

- Ubuntu 24.04 on x86_64.
- At least 4 GiB RAM for the smallest CPU model; more RAM unlocks additional options.
- An NVIDIA GPU is optional. GPU model choices require a compatible NVIDIA GPU, driver, and enough VRAM; CUDA 13 profiles require compute capability 7.5 or newer.
- `sudo` access and internet access for packages, images, and model downloads.
- Enough free disk space for your chosen model files and container images. ComfyUI assets need more space only when selected.

Run `ai-setup.sh` as a normal user, not root. It uses `sudo` for the steps that need it. On a supported NVIDIA host, it may install or change the driver; reboot and rerun the script if asked. On a CPU-only host it skips the driver and container toolkit. The script can be rerun to update generated files while preserving its existing `.env` and data directories.

```bash
chmod +x ai-setup.sh
./ai-setup.sh
```

The installer displays numbered choices sized for detected RAM and usable NVIDIA VRAM. Enter one or more numbers or IDs separated by commas, for example `1,2`. Nothing is selected by default on a fresh installation. At least one text or vision model must be selected. The first selected model starts after download; the others are installed as stopped switcher options. Choosing `media` adds ComfyUI, but does not download any image or video checkpoints. Use the switcher's **Find models** page later for those files.

For unattended setup, supply explicit IDs:

```bash
AI_MODEL_SELECTION=cpu-tiny,cpu-4b ./ai-setup.sh
```

Available IDs depend on the detected host. `cpu-tiny` is the Qwen3 0.6B CPU GGUF choice and `cpu-4b` is the Qwen3 4B CPU GGUF choice. GPU choices appear only when the card and driver pass the installer check. Rerunning keeps the previous selection if it still fits; pass a new `AI_MODEL_SELECTION` to change it. Previously downloaded files are not deleted when the selection changes. To change the initial model, reorder the IDs in `AI_MODEL_SELECTION`; the first non-media ID starts.

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

Download progress for selected GGUF models is visible in the installer output and the model directory; in-progress files end in `.part`. Hugging Face repository models use the cache in `AI_MODEL_CACHE`. ComfyUI model downloads use `.part` files until complete. The switcher's **Find models** page can install additional supported GPU models after setup. On a CPU-only host, use the installer selection to change CPU choices.

## Upgrading an existing host

Back up `~/ai_setup/.env`, `~/ai_setup/secure_proxy`, `~/ai_setup/gpu_control/results`, the Open WebUI Docker volume, and any irreplaceable media. Copy the newly generated `ai-setup.sh` to the host and rerun it as the same normal user. The existing selection is reused if compatible; specify `AI_MODEL_SELECTION` to replace it, especially after a hardware change. Review local Nginx or Compose customizations first: the installer preserves an existing Nginx config and applies only known API-route migrations, while it regenerates `docker-compose.yml`.

After installation, check:

```bash
cd ~/ai_setup
./ai-control.sh status
./ai-control.sh web-test
./ai-control.sh test
```

The installed API binds to the LAN IP selected at setup. Set `AI_BIND_IP` before the first run if automatic detection chooses the wrong interface. The installer validates it as a private IPv4 address.
