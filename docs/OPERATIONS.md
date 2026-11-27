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

Open **Coding harnesses** in the switcher for official install and local API setup links for Hermes Agent, OpenCode, Pi, Aider, Cline, and Goose. The page compares their strengths and tradeoffs. Install a harness on the machine where it will edit files; use a dedicated key from **Client sharing**, the API base URL shown on the switcher home page, and model ID `local-llm-api`.

## Switch models and workloads

Open the switcher URL and select one of the models chosen during installation. ComfyUI appears only if selected. One model workload runs at a time. The home page shows **Loading model** until the active model responds through `/v1/models`; only **Ready to use** gets a green badge. If the container exits, the page reports the failure. Status updates in place every few seconds. The home page also shows a copyable API base URL, its configured port, and stable model ID `local-llm-api`: use that ID in a harness to follow whichever language model is active. The full URL includes `AI_PORT` when it is customized. `/v1/models` advertises the stable ID alongside the underlying model ID once the model is ready. During switching or when ComfyUI is active, language model requests may fail until a language model becomes ready. A GPU/VRAM graph appears when a GPU workload is available. **Find models** lists compatible recommendations on GPU hosts and offers a specific Hugging Face model search on both GPU and CPU hosts. **Benchmarks** records results with timestamps and detected hardware details.

The **Context tuning** control offers preset sizes in a dropdown. It omits the current size and excludes values above a native limit when the running model reports one. These are valid tuner inputs, not guaranteed memory fits; the switcher restarts the model, checks readiness, saves successful settings, and attempts to restore the previous context if a test fails.

To add a model, open **Find models → Search a specific Hugging Face model**. Enter a name such as `Qwen3`, an exact `owner/repository` ID, or its `https://huggingface.co/owner/repository` URL. Review the variant type, total download size, proposed runtime, and fit status; click **Install**. Search results are separate from the curated top ten and recent-release list. Single GGUF files and complete GGUF shard sets have LFS SHA256 verification per weight. Safetensors repositories include the standard configuration, tokenizer, index, and weight files pinned to one Hub commit; LFS files have SHA256 verification. Small Git metadata files are commit-pinned and size-checked. Gated repositories, separate vision projectors, and model-specific Python code are excluded. Multimodal models may still need extra assets and image input is not guaranteed.

You can choose additional text or ComfyUI model downloads while one is running. Each type has its own first-in, first-out queue; the page shows queued and running stages. The queue is in the controller process, so a controller restart marks unfinished jobs interrupted and you must request those downloads again. The page no longer reloads every five seconds: a small status line updates without moving forms or scroll position. When work finishes, click **Show updated results** to refresh the tables once.

Discovered candidates use an internal `discovered-...` ID so the switcher can distinguish repositories and variants. The installed-model table now shows the repository model name as its main label; new installations receive a readable API ID such as `local-inkling-2-mini-q4-k-m`. Older API IDs are kept so existing clients continue working. For an inactive installed model, use **Change API ID** to replace an older ID explicitly; update Open WebUI and other API clients afterward. Previously recorded benchmarks keep their original model labels.

To remove a text model installed through **Find models**, switch to a different workload, open **Installed Hugging Face models**, expand **Uninstall model**, check **Delete this model and its downloaded files**, and click **Delete model and files**. The switcher removes its managed model files and serving container, then clears its installation record. It keeps benchmark history. The active model cannot be removed. Installer-selected built-in models, manually added files, and ComfyUI files are not covered by this action; ComfyUI bundles can share weights, and the switcher does not track ownership of older media files. Review those files separately before deleting them.

The **GPU + system RAM** option on Find models defaults to enabled. It is a saved preference for new installations. When weights are estimated to exceed GPU memory, llama.cpp can keep some GGUF layers in RAM and vLLM receives a calculated `--cpu-offload-gb` value for NVIDIA Safetensors. The switcher reserves a portion of RAM for the host and a portion of VRAM for serving, then shows an estimated RAM requirement beside assisted candidates. Disabling the option selects CPU inference for models estimated too large for VRAM. Existing installed models retain their current settings until you click **Apply saved preference** on their row. A running model is restarted and checked; a failure restores its previous container. A stopped model is staged and checked when activated. RAM and VRAM are not merged into one pool: loading can fail and inference can slow substantially across the GPU bus. Run a benchmark after activating an assisted model.

**Try anyway** permits a model whose estimated memory use exceeds the detected RAM or VRAM. It does not bypass disk space, checksum, file completeness, or runtime checks. A successful install means files were verified and a stopped container was created; activate the model to test whether its architecture, quantization, and context actually load. For AMD/Intel, GGUF uses the Vulkan image when a render device is detected; Safetensors uses CPU vLLM. A newer vLLM image may support more architectures but the llama.cpp update button does not update vLLM.

If the memory estimate is exceeded, check **Try anyway** on that row before installing. This bypasses the 4 GiB serving-memory reserve; it does not bypass disk capacity or SHA256 verification. The model may fail to load, use swap, or run slowly. Installation downloads the file and creates a stopped container; activate it from the switcher home page and test `/v1/models` and a real request. Recheck the search after a hardware change because fit labels reflect the most recent search. On a CPU-only host, rerun the installer with a new `AI_MODEL_SELECTION` to change its built-in CPU models.

Use **Client sharing** to create a key for each agent and adjust per-client request rate, burst, active and queued requests, global limits, output length, and request size. The page explains each control. Save confirms the gateway loaded the new policy; failure restores the previous values. The original shared key remains valid, but all clients using it count as one client. The Client API keys table shows a compact usage row below every key, including revoked keys, plus cumulative usage across all keys. Its single selected window applies to every row. The same all-key graphs appear below the home page GPU graph, where the window can be chosen independently. Token totals include only requests for which the engine reported usage. Rejected requests receive HTTP 429 and `Retry-After`.

Choose an amount from 1 to 90 and a unit of minutes, hours, or days above the Client API keys table to update all key rows and the cumulative totals together. The gateway records per-request timing and token counts reported by the active engine, including Chat Completions streaming usage. It never stores prompts, responses, or key values in usage history. If an endpoint omits token counts, the request is counted but its tokens are marked unmeasured; displayed totals are known counts, not estimates. Effective output tokens/s is total measured output divided by measured end-to-end request seconds. Revoked keys retain their previous usage history. The usage and GPU plots show UTC time markers, value markers, and a light grid.

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
