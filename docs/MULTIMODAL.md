# Multimodal task profiles

The switcher can run a language model and CPU document/audio workers together. Image jobs temporarily reserve the GPU for ComfyUI, then restore the prior language model. Text clients keep using the same `/v1` base URL and the `local-llm-api` model ID. During an image job, text requests may receive a temporary backend-unavailable response until the language model is ready again.

## Install and configure

Run the standalone installer and answer **yes** to the multimodal worker prompt, or set `AI_MULTIMODAL_ENABLED=1` before running it. The installer builds a CPU worker with Poppler, Tesseract, FFmpeg, eSpeak NG, and faster-whisper. It does not download language, image, or speech-recognition model weights by default. The first transcription request downloads the explicitly selected Whisper size (`tiny`, `base`, or `small`) into `~/ai_setup/gpu_control/results/multimodal/asr-models`.

For images, also select the ComfyUI workload during installation and install an SDXL-compatible `.safetensors` checkpoint through **Find models** or place one in the ComfyUI `models/checkpoints` directory. Object removal additionally needs an inpainting checkpoint in that directory (a filename containing `inpaint`). The bundled ComfyUI image currently requires an NVIDIA GPU; document and audio workers also work on CPU-only, AMD, and Intel systems. Only SDXL-style checkpoints work with the built-in text-to-image workflow. Other ComfyUI workflows remain available in the separate ComfyUI interface.

Open **Task profiles** in the switcher. The page detects installed workloads, RAM, GPU type, image checkpoints, and complete supported video bundles, then recommends general, document, creative, video, or audio recipes. **Load profile** starts its text model and saves the recipe. Creative studio selects installed image and inpainting checkpoints. Video studio selects an installed Wan 2.1 T2V 1.3B bundle. ComfyUI is queued only when an image or video request arrives. Profiles do not install missing weights. Install the complete Wan 2.1 T2V 1.3B bundle from **Find models** to enable Video studio. Other catalog video models remain available for manual workflows in ComfyUI; this job adapter currently supports only Wan 2.1 T2V 1.3B.

## Task API

Use the same authenticated API origin as `/v1/chat/completions`. The task endpoints are asynchronous: `POST` returns HTTP 202 with a job ID and status URL. Poll `GET /v1/jobs/<id>`; completed JSON results appear in `result`. Binary images/audio are at `GET /v1/jobs/<id>/result`. `GET /v1/jobs` lists the current API key's recent jobs. Keys cannot read each other's jobs. Requests remain subject to gateway key authentication and admission limits. The request body limit is 24 MiB, with decoded uploads limited to 16 MiB.

| Task | POST path | JSON body |
| --- | --- | --- |
| PDF text and optional OCR | `/v1/documents/parse` | `{"data":"<base64 PDF>","ocr":true}` |
| Image OCR | `/v1/ocr` | `{"data":"<base64 image>","language":"eng"}` |
| Speech transcription | `/v1/audio/transcriptions` | `{"data":"<base64 audio>","model":"tiny"}` |
| Speech generation | `/v1/audio/speech` | `{"input":"Hello","voice":"en"}` |
| Image generation | `/v1/images/generations` | `{"prompt":"A red bicycle","width":1024,"height":1024,"steps":20}` |
| Object removal | `/v1/images/edits` | `{"data":"<base64 JPEG/PNG/WebP>","prompt":"Remove the trash can from the attached image"}` |
| Text-to-video | `/v1/videos/generations` | `{"prompt":"A fox running through snow","width":512,"height":512,"frames":33,"fps":16,"steps":30}` |

The image route uses the active Creative studio profile's checkpoint unless `checkpoint` is specified. These job routes are switcher-specific; `/v1/images/generations` does **not** return the synchronous OpenAI Images API response. The text chat/responses routes retain their existing OpenAI-compatible behavior and `local-llm-api` alias.

The video route requires the active Video studio profile. It produces a short silent WebM clip from the installed Wan 2.1 T2V 1.3B model. Defaults are 512×512, 33 frames, 16 fps, and 30 sampling steps (about two seconds). Supported sizes are 512×512 and 832×480; frame counts are 17, 33, 49, or 81; rates are 8, 16, or 24 fps. Render time and available memory depend on the host. Download the completed clip from the job result URL. This route does not yet support image-to-video, audio, or 30-second generation.

The **Image editor** page at `/image-edit` accepts a photo and instruction through the switcher's Basic Auth UI and polls progress without refreshing the page. The API route accepts the same two inputs as JSON. The first object-removal request downloads Grounding DINO Tiny and SAM ViT Base into the persistent model cache. It then detects the named object, segments it, runs the installed inpainting checkpoint in ComfyUI, and composites only the edited region into a PNG at the original dimensions. If detection finds no confident match or several similar matches, the job fails without changing the image; clients can retry with an explicit `target` and `[x1,y1,x2,y2]` `box` in source-image pixels. This automatic workflow is intended for a single named object and may require manual ComfyUI editing for difficult scenes.

Example (substitute the local API address and key):

```bash
curl -sS -H "Authorization: Bearer $AI_KEY" -H 'Content-Type: application/json' \
  -d '{"input":"Hello from the switcher","voice":"en"}' \
  http://YOUR_HOST:8080/v1/audio/speech
curl -sS -H "Authorization: Bearer $AI_KEY" \
  http://YOUR_HOST:8080/v1/jobs/JOB_ID
curl -sS -H "Authorization: Bearer $AI_KEY" \
  http://YOUR_HOST:8080/v1/jobs/JOB_ID/result -o speech.wav
```

Jobs and artifacts persist under `~/ai_setup/gpu_control/results/multimodal` (or the installation directory configured with `AI_SETUP_DIR`). The worker requeues unfinished jobs when it starts; an interrupted job may run again. A PDF is limited to 20 pages. ComfyUI image jobs are serialized; CPU jobs run separately. Jobs currently have no automatic artifact retention policy, so include this directory in backups and remove old artifacts during maintenance.
