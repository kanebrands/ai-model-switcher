# Operations

## URLs and credentials

The installed `ai-control.sh` prints the configured switcher and ComfyUI URLs:

```bash
cd ~/ai_setup
./ai-control.sh switch-url
./ai-control.sh comfy-url
./ai-control.sh key
./ai-control.sh switch-password
```

Keep the API key and switcher password private. The switcher and ComfyUI Basic Auth username is `operator`.

## Switch models and workloads

Open the switcher URL and select one of the models chosen during installation. ComfyUI appears only if selected. One model workload runs at a time. The home page shows the active workload; a GPU/VRAM graph appears when a GPU workload is available. **Find models** lists compatible recommendations on GPU hosts and offers a specific Hugging Face model search on both GPU and CPU hosts. **Benchmarks** records results with timestamps and detected hardware details.

To add a model, open **Find models → Search a specific Hugging Face model**. Enter a name such as `Qwen3`, an exact `owner/repository` ID, or its `https://huggingface.co/owner/repository` URL. Review the variant type, total download size, proposed runtime, and fit status; click **Install**. Search results are separate from the curated top ten and recent-release list. Single GGUF files and complete GGUF shard sets have LFS SHA256 verification per weight. Safetensors repositories include the standard configuration, tokenizer, index, and weight files pinned to one Hub commit; LFS files have SHA256 verification. Small Git metadata files are commit-pinned and size-checked. Gated repositories, separate vision projectors, and model-specific Python code are excluded. Multimodal models may still need extra assets and image input is not guaranteed.

**Try anyway** permits a model whose estimated memory use exceeds the detected RAM or VRAM. It does not bypass disk space, checksum, file completeness, or runtime checks. A successful install means files were verified and a stopped container was created; activate the model to test whether its architecture, quantization, and context actually load. For AMD/Intel, GGUF uses the Vulkan image when a render device is detected; Safetensors uses CPU vLLM. A newer vLLM image may support more architectures but the llama.cpp update button does not update vLLM.

If the memory estimate is exceeded, check **Try anyway** on that row before installing. This bypasses the 4 GiB serving-memory reserve; it does not bypass disk capacity or SHA256 verification. The model may fail to load, use swap, or run slowly. Installation downloads the file and creates a stopped container; activate it from the switcher home page and test `/v1/models` and a real request. Recheck the search after a hardware change because fit labels reflect the most recent search. On a CPU-only host, rerun the installer with a new `AI_MODEL_SELECTION` to change its built-in CPU models.

Use **Client sharing** to create a key for each agent and adjust per-client request rate, burst, active and queued requests, global limits, output length, and request size. The page explains each control. Save confirms the gateway loaded the new policy; failure restores the previous values. The original shared key remains valid, but all clients using it count as one client. Rejected requests receive HTTP 429 and `Retry-After`.

The same page can test a new parallel generation-slot count. This restarts the active model, interrupts in-flight requests, waits for readiness, and saves the new count on success. A failed readiness test attempts to restore the old count and reports the result. Increasing a gateway concurrency limit alone does not add generation slots to a model server. Context tuning is a separate control; the switcher displays context values that completed its tuning checks.

## API check

```bash
cd ~/ai_setup
./ai-control.sh test
```

For an agent, use the API base URL shown at setup, `/v1`, a model ID returned by `/v1/models`, and either the existing key or a dedicated key from **Client sharing**. Vision requests can also use `/vision/v1` when the vision model is active. API compatibility depends on the selected serving engine and model; test each client's required endpoint and tool-calling behavior. CPU models can be significantly slower than GPU models, especially at larger contexts.

## Service checks and recovery

```bash
cd ~/ai_setup
./ai-control.sh status
./ai-control.sh web-test
./ai-control.sh logs
./ai-control.sh web-logs
./ai-control.sh media-logs
```

The script also has model-specific log commands; run `./ai-control.sh help` for the full list. `./ai-control.sh logs` follows the model chosen to start at installation; after a web switch, use the model-specific command. If a slot or context change fails, read its status on the switcher page before retrying. If an engine or Open WebUI update fails, inspect its status in the switcher and the corresponding service logs. Keep the installation's `.env`, results database, Open WebUI volume, and media files in regular backups; repository source alone does not restore user data.
