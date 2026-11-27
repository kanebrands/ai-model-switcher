"""Escaped model recommendation and installation page."""
import html

from benchmark_views import shell


def esc(value):
    return html.escape(str(value if value is not None else "—"), quote=True)


def specific_search_section(search, installed, jobs, csrf, engine=None):
    search = search or {"status": "not searched", "items": [], "query": "", "error": None}
    installing = {job["candidate_id"] for job in jobs if job["status"] in ("queued", "running")}
    installed_files = {(item["repo"], item["sha256"]) for item in installed}
    engine_build = (engine or {}).get("local_build") or 0
    rows = []
    for item in search["items"]:
        installed_here = item.get("installed") or (item["repo"], item["sha256"]) in installed_files
        needs_engine = (item.get("runtime") == "gpu" and item.get("min_engine_build", 0) > engine_build)
        busy = item["id"] in installing
        fit = bool(item.get("fits"))
        disk_fits = item.get("disk_fits", True)
        state = ("Installed" if installed_here else "Installing" if busy else
                 "Insufficient disk" if not disk_fits else "Engine update needed" if needs_engine else
                 "Likely fits" if fit else "Memory estimate exceeded")
        disabled = " disabled" if installed_here or busy or needs_engine or not disk_fits else ""
        override = ('' if fit else
                    '<label class="note"><input type="checkbox" name="override_fit" value="1" required> '
                    'Try anyway; loading may fail or use swap and run slowly</label><br>')
        action = (f'<form method="post" action="/models/discovered/install">'
                  f'<input type="hidden" name="token" value="{esc(csrf)}">'
                  f'<input type="hidden" name="candidate" value="{esc(item["id"])}">'
                  f'{override}<button type="submit"{disabled}>{"Install" if not installed_here else "Installed"}</button></form>')
        rows.append(f'<tr><td><a href="https://huggingface.co/{esc(item["repo"])}" target="_blank" rel="noopener noreferrer">'
                    f'{esc(item["repo"])}</a></td><td>{esc(item["file"])}</td><td>{esc(item["size_gb"])} GB</td>'
                    f'<td>{esc(item.get("runtime", "gpu").upper())}</td><td>{esc(state)}</td><td>{action}</td></tr>')
    status = esc(search["status"])
    if search.get("error"):
        status += ': ' + esc(search["error"])
    if search["status"] == "completed" and not rows:
        status += ' — no verified standalone GGUF files found. Gated repositories and sharded files are not supported by this installer.'
    installed_rows = ''.join(f'<tr><td>{esc(item["alias"])}</td><td>{esc(item["repo"])}</td>'
                             f'<td>{esc(item.get("runtime", "gpu").upper())}</td></tr>' for item in installed)
    return (f'<section class="card" id="specific-search"><h2>Search a specific Hugging Face model</h2>'
            '<p>Search by model name, exact owner/repository ID, or a Hugging Face repository URL. '
            'Searches any publisher and lists verified single-file GGUF variants, including other quantizations. '
            'A model is downloaded only after you choose Install. The initial memory check reserves 4 GiB for serving; '
            'you can explicitly try a model that exceeds this estimate. Disk capacity and SHA256 verification still apply. '
            'CPU fallback is available when GPU memory is insufficient. Loading, context, and speed are unverified until the model runs. '
            'Gated models, sharded GGUF files, projector files, and non-GGUF formats are not supported by this search.</p>'
            f'<form method="post" action="/models/search"><input type="hidden" name="token" value="{esc(csrf)}">'
            f'<label for="specific-model">Model or repository</label> <input id="specific-model" type="text" name="query" '
            f'maxlength="200" required value="{esc(search.get("query") or "")}" placeholder="owner/model-GGUF or model name"> '
            '<button type="submit">Search Hugging Face</button></form>'
            f'<p role="status">Search: {status}</p>'
            '<div class="scroll"><table><thead><tr><th>Repository</th><th>GGUF variant</th><th>Size</th><th>Runtime</th><th>Fit/status</th><th>Action</th></tr></thead>'
            f'<tbody>{"".join(rows) if rows else "<tr><td colspan=6>No search results yet.</td></tr>"}</tbody></table></div>'
            '<h3>Installed Hugging Face models</h3><div class="scroll"><table><tr><th>API name</th><th>Repository</th><th>Runtime</th></tr>'
            f'{installed_rows if installed_rows else "<tr><td colspan=3>None yet.</td></tr>"}</table></div></section>')


def index(hardware, suggestions, installed, jobs, csrf, prefer_ablit=True,
          media_suggestions=(), media_jobs=(), comfy_url="", discovery=None, engine=None, search=None):
    selected_on = " selected" if prefer_ablit else ""
    selected_off = "" if prefer_ablit else " selected"
    hardware_line = (f'<h2>Detected hardware</h2><p>GPU: <strong>{esc(hardware["gpu"] or "unavailable")}</strong> · '
                     f'VRAM: {esc(hardware["vram_mib"])} MiB · RAM: {esc(hardware["ram_gib"])} GiB · '
                     f'CPU: {esc(hardware.get("cpu_model"))} · Threads: {esc(hardware.get("cpu_threads"))} · '
                     f'Model storage free: {esc(hardware["disk_free_gib"])} GiB</p>'
                     f'<p class="note">Last scan UTC: {esc(hardware.get("checked_at"))} · '
                     f'Last hardware change UTC: {esc(hardware.get("changed_at"))}. '
                     'The switcher scans about once a minute while this page is used. '
                     'A GPU or memory change refreshes the Hugging Face discoveries and recalculates suitability. '
                     'CPU details are shown, but CPU upgrades do not increase the GPU memory available to models.</p>'
                     f'<form method="post" action="/models/hardware/rescan"><input type="hidden" name="token" value="{esc(csrf)}">'
                     '<button type="submit">Rescan hardware and suggestions</button></form>')
    search_section = specific_search_section(search, installed, jobs, csrf, engine)
    if not hardware.get("vram_mib"):
        body = ('<section class="card">' + hardware_line + '</section>'
                '<section class="card"><h2>CPU model choices</h2>'
                '<p>The current installation has no compatible NVIDIA GPU. The installer offers CPU models according to detected RAM. '
                'Installed choices are available on the switcher home page. To change the selection, rerun the installer with '
                '<code>AI_MODEL_SELECTION=cpu-tiny,cpu-4b</code> after checking which choices fit this host. '
                'The GPU catalog and engine updates appear when a supported GPU is detected and the installer is rerun.</p></section>'
                + search_section)
        return shell("Hardware and model choices", body, refresh=(search or {}).get("status") == "running", wide=True)
    discovery = discovery or {"status": "not checked", "items": [], "query": "", "error": None}
    engine = engine or {"checked_at": None, "checking": False, "update_available": None,
                        "local_build": None, "error": None, "job": None, "busy": False}
    engine_status = ("Checking GHCR for an update…" if engine.get("checking") else
                     "Update available" if engine.get("update_available") else
                     "Current image matches GHCR" if engine.get("update_available") is False else
                     "Not checked yet")
    if engine.get("error"):
        engine_status = "Update check failed: " + str(engine["error"])
    engine_job = engine.get("job") or {}
    engine_action = ""
    if engine.get("update_available") and not engine.get("busy"):
        engine_action = (f'<form method="post" action="/models/engine/update"><input type="hidden" name="token" value="{esc(csrf)}">'
                         '<button type="submit">Download and apply llama.cpp update</button></form>')
    engine_box = (f'<h2>Model engine updates</h2><p>llama.cpp CUDA 13: <strong>{esc(engine_status)}</strong> · '
                  f'Installed build: {esc(engine.get("local_build"))} · Last checked UTC: {esc(engine.get("checked_at"))}</p>'
                  '<p class="note">The check runs automatically when this page opens and every six hours thereafter. Applying an update pulls the current image, '
                  'recreates the built-in llama.cpp containers, and may briefly interrupt the active model. '
                  'The previous image is restored if the active model does not become ready. '
                  'Models installed from discoveries use the updated engine when next activated after switching away. '
                  'vLLM and ComfyUI have separate engines and are not changed by this button.</p>'
                  f'<form method="post" action="/models/engine/check"><input type="hidden" name="token" value="{esc(csrf)}">'
                  '<button type="submit">Check engine updates now</button></form>' + engine_action
                  + (f'<p>Latest update job: <strong>{esc(engine_job.get("status"))}</strong> · {esc(engine_job.get("stage"))}'
                     f' · {esc(engine_job.get("error"))}</p>' if engine_job else ''))
    rows = []
    installing = {job["candidate_id"] for job in jobs if job["status"] in ("queued", "running")}
    for number, item in enumerate(suggestions, 1):
        source = "https://huggingface.co/" + item["repo"]
        estimate = item["estimated_tps"] + " tokens/s" if item["estimated_tps"] else "No comparable run"
        state = "Installed" if item["installed"] else "Installing" if item["id"] in installing else "Available"
        context = f'{item["context_tokens"]:,} tokens' if item.get("context_tokens") else "Unknown"
        context += " (configured)" if item["installed"] else " (target)"
        disabled = " disabled" if state != "Available" else ""
        button_text = state if disabled else "Download and install"
        form = (f'<form method="post" action="/models/install"><input type="hidden" name="token" value="{esc(csrf)}">'
                f'<input type="hidden" name="candidate" value="{esc(item["id"])}"><button type="submit"{disabled}>{esc(button_text)}</button></form>')
        rows.append(f'<tr><td>{number}</td><td><strong>{esc(item["name"])}</strong><br><a href="{esc(source)}" target="_blank" rel="noopener noreferrer">Hugging Face source</a></td>'
                    f'<td>{esc(item["features"])}</td><td>{"Yes" if item["ablit"] else "No"}</td><td>{esc(item["size_gb"])} GB</td>'
                    f'<td>{esc(estimate)}</td><td>{esc(context)}</td><td><strong>{esc(state)}</strong></td><td>{form}</td></tr>')
    installed_rows = []
    for item in installed:
        mode = "hf-" + item["candidate_id"]
        form = (f'<form method="post" action="/switch"><input type="hidden" name="token" value="{esc(csrf)}">'
                f'<button name="mode" value="{esc(mode)}" type="submit">Activate</button></form>')
        configured_context = f'{item.get("context_tokens") or 32768:,} tokens'
        installed_rows.append(f'<tr><td>{esc(item["alias"])}</td><td>{esc(item["repo"])}</td>'
                              f'<td>{esc(configured_context)}</td><td>{esc(item["installed_at"])}</td><td>{form}</td></tr>')
    job_rows = []
    for job in jobs:
        job_rows.append(f'<tr><td>{esc(job["candidate_id"])}</td><td>{esc(job["status"])}</td>'
                        f'<td>{esc(job["stage"])}</td><td>{esc(job["error"])}</td></tr>')
    refreshing = any(job["status"] in ("queued", "running") for job in jobs)
    discovery_rows = []
    installing = installing | {job["candidate_id"] for job in jobs if job["status"] in ("queued", "running")}
    for number, item in enumerate(discovery.get("items", []), 1):
        required = item.get("min_engine_build") or 0
        blocked = required and (engine.get("local_build") or 0) < required
        state = ("Installed" if item.get("installed") else "Installing" if item["id"] in installing
                 else f"Needs llama.cpp build {required}+" if blocked else "Untested engine compatibility")
        disabled = " disabled" if item.get("installed") or item["id"] in installing or blocked else ""
        label = "Download and stage" if not disabled else state
        form = (f'<form method="post" action="/models/discovered/install"><input type="hidden" name="token" value="{esc(csrf)}">'
                f'<input type="hidden" name="candidate" value="{esc(item["id"])}"><button type="submit"{disabled}>{esc(label)}</button></form>')
        discovery_rows.append(f'<tr><td>{number}</td><td><strong>{esc(item["name"])}</strong><br>'
                              f'<a href="https://huggingface.co/{esc(item["repo"])}" target="_blank" rel="noopener noreferrer">Hugging Face source</a></td>'
                              f'<td>{esc(item["created_at"])}</td><td>{esc(item["file"])}</td><td>{esc(item["size_gb"])} GB</td>'
                              f'<td>{esc(item["pipeline"])}</td><td>{"Yes" if item["ablit"] else "No"}</td>'
                              f'<td>{esc(state)}</td><td>{form}</td></tr>')
    discovery_status = (f'{esc(discovery["status"])} · Search: {esc(discovery.get("query") or "newest")} · '
                        f'Checked UTC: {esc(discovery.get("ended_at") or discovery.get("started_at"))}')
    if discovery.get("error"):
        discovery_status += " · " + esc(discovery["error"])
    media_installing = {job["candidate_id"] for job in media_jobs if job["status"] in ("queued", "running")}
    media_rows = []
    for number, item in enumerate(media_suggestions, 1):
        state = ("Installed" if item["installed"] else "Installing" if item["id"] in media_installing
                 else "File conflict" if item.get("conflict") else "Available")
        disabled = " disabled" if state != "Available" else ""
        label = state if disabled else "Download and install"
        repo = item["files"][0][0]
        form = (f'<form method="post" action="/models/media/install"><input type="hidden" name="token" value="{esc(csrf)}">'
                f'<input type="hidden" name="candidate" value="{esc(item["id"])}"><button type="submit"{disabled}>{esc(label)}</button></form>')
        estimates = item.get("estimates", ("Uncalibrated",) * 4)
        media_rows.append(f'<tr><td>{number}</td><td>{esc(item["kind"])}</td>'
                          f'<td><strong>{esc(item["name"])}</strong><br><a href="https://huggingface.co/{esc(repo)}" target="_blank" rel="noopener noreferrer">Hugging Face source</a></td>'
                          f'<td>{esc(item["use"])}</td><td>{"Prompt encoder only" if item.get("ablit") else "No"}</td><td>{esc(item["vram"])} GiB</td>'
                          f'<td>{esc(item["total_gb"])} GB total<br>{esc(item["download_gb"])} GB needed · {esc(item["files_count"])} files</td>'
                          f'<td>{esc(estimates[0])}</td><td>{esc(estimates[1])}</td><td>{esc(estimates[2])}</td><td>{esc(estimates[3])}</td>'
                          f'<td><strong>{esc(state)}</strong></td>'
                          f'<td><a href="{esc(item["workflow"])}" target="_blank" rel="noopener noreferrer">Workflow guide</a></td><td>{form}</td></tr>')
    media_job_rows = []
    for job in media_jobs:
        media_job_rows.append(f'<tr><td>{esc(job["candidate_id"])}</td><td>{esc(job["status"])}</td>'
                              f'<td>{esc(job["stage"])}</td><td>{esc(job["error"])}</td></tr>')
    refreshing = (refreshing or bool(media_installing) or discovery["status"] == "running"
                  or bool(engine.get("checking")) or bool(engine.get("busy")))
    comfy_link = f'<a href="{esc(comfy_url)}">Open ComfyUI</a>' if comfy_url else "Open ComfyUI from the switcher"
    body = ('<div class="section-links" aria-label="Page sections"><a href="#specific-search">Search Hugging Face</a><a href="#text-models">Text and code</a><a href="#media-models">Image and video</a><a href="#discovered-models">Recent discoveries</a><a href="#installed-models">Installed</a></div>'
            + search_section
            + '<div class="overview-grid"><section class="card" id="hardware">' + hardware_line + '</section><section class="card" id="engine">' + engine_box + '</section></div>'
            + '<h2 id="text-models">Text and code models</h2><p class="note">Top 10 reviewed Hugging Face GGUF candidates, including already installed models. New downloads fit current GPU memory with a 4 GiB serving reserve and available disk. '
              'Features describe intended use, not measured quality. Tokens/s is a broad estimate calibrated from completed short-prompt runs on this GPU; run a benchmark after installation. '
              'Context is a conservative serving target chosen from GGUF weight size and detected VRAM, capped by the model family native limit; it is configured on installation and has not been load-tested at full length. '
              'Downloads verify the exact Hugging Face file size and SHA256. Installation does not activate the model. Text serving only; image input needs a separate projector.</p>'
            + f'<form method="get" action="/models"><label for="ablit">Abliterated model preference</label> '
              f'<select id="ablit" name="ablit"><option value="1"{selected_on}>Prefer abliterated</option>'
              f'<option value="0"{selected_off}>No preference</option></select> <button type="submit">Update recommendations</button></form>'
            + '<div class="scroll"><table><thead><tr><th>#</th><th>Model</th><th>Suitable for</th><th>Abliterated</th><th>Download</th><th>Estimated speed</th><th>Context on this hardware</th><th>Status</th><th>Action</th></tr></thead>'
              f'<tbody>{"".join(rows) if rows else "<tr><td colspan=9>No candidates verified or fitting this hardware right now.</td></tr>"}</tbody></table></div>'
            + '<h2 id="discovered-models">Recent Hugging Face GGUF discoveries</h2>'
              '<p class="note">These are the newest size-fitting GGUF files found from selected publishers, separate from the reviewed top 10. '
              'Refreshes happen daily when this page opens or when you use the search below. A discovery is not a quality recommendation. '
              'Download and stage verifies the exact file size and SHA256, then creates a stopped serving container; activation is a separate action. '
              'Engine compatibility and context length are untested until the model is loaded and benchmarked. '
              'Search for a model family such as Inkling to find a recent release that is no longer in the newest ten.</p>'
            + f'<form method="post" action="/models/refresh"><input type="hidden" name="token" value="{esc(csrf)}">'
              f'<label for="model-search">Search models</label> <input id="model-search" name="search" maxlength="64" value="{esc(discovery.get("query") or "")}" placeholder="Blank for newest"> '
              '<button type="submit">Refresh Hugging Face catalog</button></form>'
              f'<p>Catalog refresh: {discovery_status}</p>'
            + '<div class="scroll"><table class="results-table"><thead><tr><th>#</th><th>Model</th><th>Published UTC</th><th>GGUF file</th><th>Size</th><th>Source type</th><th>Abliterated</th><th>Engine status</th><th>Action</th></tr></thead>'
              f'<tbody>{"".join(discovery_rows) if discovery_rows else "<tr><td colspan=9>No matching, verified GGUF files are in this refresh.</td></tr>"}</tbody></table></div>'
            + '<h2 id="installed-models">Installed from this page</h2><div class="scroll"><table><tr><th>API name</th><th>Source</th><th>Configured context</th><th>Installed UTC</th><th>Action</th></tr>'
              f'{"".join(installed_rows) if installed_rows else "<tr><td colspan=5>None yet</td></tr>"}</table></div>'
            + '<h2 id="media-models">Image and video models for ComfyUI</h2><p class="note">A separate top 10 chosen for the detected GPU, RAM, and available media storage. '
              'The abliteration selector above also applies here: when preferred, Z-Image uses an abliterated Qwen prompt encoder; other entries have no verified abliterated bundle. '
              'VRAM values are conservative starting targets, not measured peaks; video resolution and clip length affect memory use. '
              'Time ranges are rough planning estimates for an RTX 3090 24 GiB, not completed benchmarks on this machine. '
              'Image estimates cover one 1024×1024 render; image edit means one image-to-image or reference edit. '
              'Video estimates cover 30 seconds at 24 fps assembled from shorter clips at about 720p. These installs do not add a 1080p upscaler, so native 1080p times are unavailable. '
              'NA means the listed bundle is not intended for that task. Other GPUs show Uncalibrated until measured. '
              'Each install places every required checkpoint, text encoder, and VAE in the ComfyUI model folders, reusing matching files already present. '
              'Downloads verify Hugging Face file size and SHA256. Installing files does not start ComfyUI or select a workflow. '
              f'Use the linked workflow guide, then {comfy_link} and refresh its model lists if needed.</p>'
            + '<div class="scroll"><table class="results-table"><thead><tr><th>#</th><th>Type</th><th>Model</th><th>Suitable for</th><th>Abliterated</th><th>VRAM target</th><th>Files</th><th>Text → image</th><th>Image edit</th><th>30s 720p video</th><th>30s 1080p video</th><th>Status</th><th>Workflow</th><th>Action</th></tr></thead>'
              f'<tbody>{"".join(media_rows) if media_rows else "<tr><td colspan=14>No verified image or video candidates fit this hardware right now.</td></tr>"}</tbody></table></div>'
            + '<h2>Recent image and video installations</h2><div class="scroll"><table><tr><th>Model</th><th>Status</th><th>Stage</th><th>Error</th></tr>'
              f'{"".join(media_job_rows) if media_job_rows else "<tr><td colspan=4>No media downloads yet</td></tr>"}</table></div>'
            + '<h2>Recent installations</h2><div class="scroll"><table><tr><th>Model</th><th>Status</th><th>Stage</th><th>Error</th></tr>'
              f'{"".join(job_rows) if job_rows else "<tr><td colspan=4>No download jobs yet</td></tr>"}</table></div>')
    return shell("Hugging Face model recommendations", body,
                 refresh=refreshing or (search or {}).get("status") == "running", wide=True)
