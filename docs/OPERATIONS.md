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

## Switch models and GPU workloads

Open the switcher URL and select a model or ComfyUI. A single GPU normally runs one model workload at a time. The home page shows the active workload and a GPU/VRAM graph. **Find models** lists compatible recommendations and whether they are installed. The switcher can download and install a selected catalog model. **Benchmarks** records results with timestamps and hardware details.

Use **Client sharing** to create a key for each agent and adjust per-client request rate, burst, active and queued requests, global limits, output length, and request size. The page explains each control. Save confirms the gateway loaded the new policy; failure restores the previous values. The original shared key remains valid, but all clients using it count as one client. Rejected requests receive HTTP 429 and `Retry-After`.

The same page can test a new parallel generation-slot count. This restarts the active model, interrupts in-flight requests, waits for readiness, and saves the new count on success. A failed readiness test attempts to restore the old count and reports the result. Increasing a gateway concurrency limit alone does not add generation slots to a model server. Context tuning is a separate control; the switcher displays context values that completed its tuning checks.

## API check

```bash
cd ~/ai_setup
./ai-control.sh test
```

For an agent, use the API base URL shown at setup, `/v1`, a model ID returned by `/v1/models`, and either the existing key or a dedicated key from **Client sharing**. Vision requests use `/vision/v1` when the vision model is active. API compatibility depends on the selected serving engine and model; test each client's required endpoint and tool-calling behavior.

## Service checks and recovery

```bash
cd ~/ai_setup
./ai-control.sh status
./ai-control.sh web-test
./ai-control.sh logs
./ai-control.sh web-logs
./ai-control.sh media-logs
```

The script also has model-specific log commands; run `./ai-control.sh help` for the full list. If a slot or context change fails, read its status on the switcher page before retrying. If an engine or Open WebUI update fails, inspect its status in the switcher and the corresponding service logs. Keep the installation's `.env`, results database, Open WebUI volume, and media files in regular backups; repository source alone does not restore user data.
