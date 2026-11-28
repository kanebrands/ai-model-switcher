"""Hardware-aware task recipes that preserve the stable text-model API ID."""
import html
import json
from pathlib import Path

from benchmark_views import shell


RECIPES = (
    ("general", "General assistant", "Chat and document analysis", "base"),
    ("documents", "Document desk", "PDF extraction, OCR, and spoken transcription", "small"),
    ("creative", "Creative studio", "Text assistance and queued ComfyUI images", "base"),
    ("audio", "Audio desk", "Speech transcription and speech generation", "small"),
)


def recommend(hardware, modes, media_ready, worker_enabled, checkpoints=(), current=None):
    """Rank installed text workloads using detected RAM/VRAM; no model download."""
    vram = (hardware.get("vram_mib") or 0) / 1024
    ram = hardware.get("ram_gib") or 0
    modes = set(modes) - {"media"}
    preferred = ["qwen4b", "deepseek", "cpu-4b", "cpu-tiny", "llm", "gemma4", "huihui"]
    if vram >= 24 and ram >= 48:
        preferred = ["qwen4b", "llm", "deepseek", "gemma4", "huihui", "cpu-4b", "cpu-tiny"]
    elif vram < 12:
        preferred = ["cpu-4b", "cpu-tiny", "deepseek", "qwen4b", "llm"]
    chosen = current if current in modes else next((mode for mode in preferred if mode in modes), next(iter(sorted(modes)), None))
    if chosen is None:
        return []
    edit_choices = [name for name in checkpoints if "inpaint" in name.lower()]
    rows = []
    for ident, label, use, asr in RECIPES:
        rows.append({"id": ident, "name": label, "use": use, "text_mode": chosen,
                     "text_modes": sorted(modes),
                     "asr_model": asr if ident in ("documents", "audio") else "tiny",
                     "pdf": worker_enabled, "ocr": worker_enabled, "audio": worker_enabled,
                     "image": bool(worker_enabled and media_ready and ident == "creative"),
                     "image_checkpoint": checkpoints[0] if checkpoints and ident == "creative" else None,
                     "image_edit_checkpoint": edit_choices[0] if edit_choices and ident == "creative" else None,
                     "image_edit_checkpoints": edit_choices if ident == "creative" else [],
                     "checkpoints": list(checkpoints) if ident == "creative" else [],
                     "available": bool(worker_enabled and (ident != "creative" or media_ready)),
                     "reason": ("Ready to configure" if worker_enabled and (ident != "creative" or media_ready) else
                                "Enable multimodal workers in the installer" if not worker_enabled else
                                "Install ComfyUI and an SDXL checkpoint to enable images")})
    return rows


def save(path, profile):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(profile, sort_keys=True))
    temporary.replace(target)


def load(path):
    target = Path(path)
    return json.loads(target.read_text()) if target.is_file() else None


def page(rows, active, hardware, csrf, jobs=()):
    esc = lambda value: html.escape(str(value), quote=True)
    cards = []
    for row in rows:
        state = "Available" if row["available"] else "Unavailable"
        checkpoint_choice = (f'<label>Image checkpoint <select name="checkpoint">'
                             + "".join(f'<option value="{esc(name)}">{esc(name)}</option>' for name in row["checkpoints"])
                             + '</select></label>' if row["image"] else "")
        edit_checkpoint_choice = (f'<label>Inpainting checkpoint <select name="edit_checkpoint">'
                                  + "".join(f'<option value="{esc(name)}">{esc(name)}</option>' for name in row["image_edit_checkpoints"])
                                  + '</select></label>' if row["image"] and row["image_edit_checkpoints"] else "")
        text_choice = (f'<label>Language model <select name="text_mode">' +
                       "".join(f'<option value="{esc(mode)}"{" selected" if mode == row["text_mode"] else ""}>'
                               f'{esc(mode)}</option>' for mode in row["text_modes"]) + '</select></label>')
        button = (f'<form method="post" action="/profiles/apply"><input type="hidden" name="token" value="{esc(csrf)}">'
                  f'<input type="hidden" name="profile" value="{esc(row["id"])}">{text_choice}{checkpoint_choice}{edit_checkpoint_choice}<button type="submit"'
                  f'{"" if row["available"] else " disabled"}>Load profile</button></form>')
        cards.append(f'<section class="card"><div class="eyebrow">{esc(state)}</div><h2>{esc(row["name"])}</h2>'
                     f'<p>{esc(row["use"])}</p><p>Language model: {esc(row["text_mode"])} via '
                     '<code>local-llm-api</code>. PDF/OCR and audio run as queued CPU work. '
                     f'Image generation: {"queued on ComfyUI" if row["image"] else "not configured"}. '
                     f'Object removal: {"available" if row["image_edit_checkpoint"] else "install an inpainting checkpoint"}. '
                     f'ASR size: {esc(row["asr_model"])}.</p><p>{esc(row["reason"])}</p>{button}</section>')
    current = (f'Active profile: {esc(active["name"])}; language model: {esc(active["text_mode"])}.'
               if active else 'No multimodal profile is active.')
    body = ('<section class="card"><div class="eyebrow">MULTIMODAL PROFILES</div><h1>Task profiles</h1>'
            f'<p>{current}</p><p>Detected hardware: {esc(hardware.get("gpu") or "CPU only")}; '
            f'{esc(hardware.get("ram_gib"))} GiB RAM. Profiles choose installed models and worker settings; '
            'they do not download a language or image model.</p><p>Clients continue using the same '
            'OpenAI compatible base URL and <code>local-llm-api</code> for text. '
            'Document, audio, and image tasks use dedicated paths at that base URL.</p>'
            '<p><a href="/image-edit">Open image editor</a> · <a href="/">Back to switcher</a></p></section>')
    queue = ('<section class="card"><h2>Recent task jobs</h2><p>Jobs stay in the durable queue across worker restarts. '
             'Use the authenticated API job URL to retrieve each result.</p><table><thead><tr><th>Started UTC</th>'
             '<th>Task</th><th>Status</th><th>Stage</th></tr></thead><tbody>' +
             "".join(f'<tr><td>{esc(item["created_at"])}</td><td>{esc(item["kind"])}</td>'
                     f'<td>{esc(item["status"])}</td><td>{esc(item["stage"])}</td></tr>' for item in jobs) +
             '</tbody></table></section>')
    return shell("Multimodal profiles", body + "".join(cards) + queue)
