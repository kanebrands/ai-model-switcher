"""Hardware-aware task recipes that preserve the stable text-model API ID."""
import html
import json
from pathlib import Path
import uuid

from benchmark_views import shell


RECIPES = (
    ("general", "General assistant", "Chat and document analysis", "base"),
    ("documents", "Document desk", "PDF extraction, OCR, and spoken transcription", "small"),
    ("code", "Code desk", "Code generation and repository work", "tiny"),
    ("creative", "Creative studio", "Text assistance and queued ComfyUI images", "base"),
    ("video", "Video studio", "Text-to-video clips through ComfyUI", "base"),
    ("audio", "Audio desk", "Speech transcription and speech generation", "small"),
)


def recommend(hardware, modes, media_ready, worker_enabled, checkpoints=(), current=None,
              video_bundles=(), image_checkpoints=None):
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
    image_checkpoints = list(image_checkpoints if image_checkpoints is not None else
                             (name for name in checkpoints if "inpaint" not in name.lower()))
    video_choices = list(video_bundles)
    rows = []
    for ident, label, use, asr in RECIPES:
        video = bool(worker_enabled and media_ready and video_choices and ident == "video")
        image = bool(worker_enabled and media_ready and (image_checkpoints or edit_choices)
                     and ident == "creative")
        available = bool(worker_enabled and (ident not in ("creative", "video") or
                                             (image if ident == "creative" else video)))
        rows.append({"id": ident, "name": label, "use": use, "text_mode": chosen,
                     "text_modes": sorted(modes),
                     "asr_model": asr if ident in ("documents", "audio") else "tiny",
                     "asr_models": ["tiny", "base", "small"] if ident in ("documents", "audio") else [],
                     "prefer_ablit": False,
                     "pdf": worker_enabled, "ocr": worker_enabled, "audio": worker_enabled,
                     "image": image, "video": video,
                     "video_model": video_choices[0] if video else None,
                     "video_models": video_choices if ident == "video" else [],
                     "image_checkpoint": image_checkpoints[0] if image_checkpoints and ident == "creative" else None,
                     "image_edit_checkpoint": edit_choices[0] if edit_choices and ident == "creative" else None,
                     "image_edit_checkpoints": edit_choices if ident == "creative" else [],
                     "checkpoints": list(image_checkpoints) if ident == "creative" else [],
                     "available": available,
                     "reason": ("Ready to configure" if available else
                                "Enable multimodal workers in the installer" if not worker_enabled else
                                "Install ComfyUI and an image checkpoint" if ident == "creative" else
                                "Install ComfyUI and the Wan 2.1 T2V 1.3B bundle from Find models")})
    return rows


def load_library(path):
    target = Path(path)
    if not target.is_file():
        return {"saved": {}, "custom": {}}
    data = json.loads(target.read_text())
    return {"saved": data.get("saved", {}), "custom": data.get("custom", {})}


def save_library(path, library):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(library, sort_keys=True))
    temporary.replace(target)


def profile_rows(rows, library):
    from copy import deepcopy
    rows = [deepcopy(row) for row in rows]
    base = {row["id"]: row for row in rows}
    for row in rows:
        row["base"] = row["id"]
        row["custom"] = False
        selected = library.get("saved", {}).get(row["id"], {})
        apply_choices(row, selected)
    for ident, saved in library.get("custom", {}).items():
        template = base.get(saved.get("base"))
        if template is None or not ident.startswith("custom-"):
            continue
        row = deepcopy(template)
        row.update(id=ident, name=saved.get("name", "Custom profile"), custom=True)
        apply_choices(row, saved.get("selection", {}))
        rows.append(row)
    return rows


def apply_choices(row, saved):
    for key, choices in (("text_mode", "text_modes"), ("image_checkpoint", "checkpoints"),
                         ("image_edit_checkpoint", "image_edit_checkpoints"),
                         ("video_model", "video_models"), ("asr_model", "asr_models")):
        if saved.get(key) in row.get(choices, []):
            row[key] = saved[key]
    row["prefer_ablit"] = saved.get("prefer_ablit") is True


def selected_choices(row):
    return {key: row[key] for key in ("text_mode", "image_checkpoint", "image_edit_checkpoint",
                                     "video_model", "asr_model", "prefer_ablit")}


def new_custom_id():
    return "custom-" + uuid.uuid4().hex


def save(path, profile):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(profile, sort_keys=True))
    temporary.replace(target)


def load(path):
    target = Path(path)
    return json.loads(target.read_text()) if target.is_file() else None


def text_fit(info, role, prefer_ablit=False):
    name = (info.get("name") or "").lower()
    features = (info.get("features") or "").lower()
    context = int(info.get("context_tokens") or 0)
    ablit = bool(info.get("ablit"))
    score = (2 if context >= 32768 and role in ("documents", "code") else 0)
    score += 3 if role == "code" and ("coder" in name or "coding" in features) else 0
    score += 2 if role == "documents" and ("document" in features or "reasoning" in features) else 0
    score += 2 if role in ("creative", "video") and ablit else 0
    score += 3 if prefer_ablit and ablit else 0
    score += 1 if role == "audio" and ("4b" in name or "tiny" in name) else 0
    return score


def _comparison(esc, title, choices, details, selected, role, prefer_ablit=False):
    if not choices:
        return ""
    ranked = sorted(choices, key=lambda key: (-text_fit(details.get(key, {}), role, prefer_ablit), key)
                    if title == "Language models" else (-details.get(key, {}).get("rank", 0), key))
    rows = []
    for index, key in enumerate(ranked):
        info = details.get(key, {})
        label = info.get("name") or key
        note = "Suggested · " if index == 0 else ""
        note += "Selected · " if key == selected else ""
        rows.append(f'<tr><td><strong>{esc(label)}</strong><br><small>{esc(key)}</small></td>'
                    f'<td>{esc(note + info.get("description", "Installed model"))}</td>'
                    f'<td>{esc(info.get("pros", "Installed and selectable"))}</td>'
                    f'<td>{esc(info.get("cons", "Performance depends on this system"))}</td></tr>')
    return (f'<details><summary>{esc(title)}: descriptions and tradeoffs</summary>'
            '<div class="table-wrap"><table><thead><tr><th>Model</th><th>Use</th><th>Pros</th><th>Cons</th>'
            '</tr></thead><tbody>' + "".join(rows) + '</tbody></table></div></details>')


def page(rows, active, hardware, csrf, jobs=(), model_info=None):
    esc = lambda value: html.escape(str(value), quote=True)
    model_info = model_info or {}
    cards = []
    for row in rows:
        state = "Available" if row["available"] else "Unavailable"
        checkpoint_choice = (f'<label>Image checkpoint <select name="checkpoint">'
                             + "".join(f'<option value="{esc(name)}"{" selected" if name == row["image_checkpoint"] else ""}>{esc(name)}</option>'
                                       for name in row["checkpoints"])
                             + '</select></label>' if row["checkpoints"] else "")
        edit_checkpoint_choice = (f'<label>Inpainting checkpoint <select name="edit_checkpoint">'
                                  + "".join(f'<option value="{esc(name)}"{" selected" if name == row["image_edit_checkpoint"] else ""}>{esc(name)}</option>'
                                            for name in row["image_edit_checkpoints"])
                                  + '</select></label>' if row["image"] and row["image_edit_checkpoints"] else "")
        video_choice = (f'<label>Video model <select name="video_model">'
                        + "".join(f'<option value="{esc(name)}"{" selected" if name == row["video_model"] else ""}>{esc(name)}</option>'
                                  for name in row["video_models"])
                        + '</select></label>' if row["video"] else "")
        text_choice = (f'<label>Language model <select name="text_mode">' +
                       "".join(f'<option value="{esc(mode)}"{" selected" if mode == row["text_mode"] else ""}>'
                               f'{esc(model_info.get("text", {}).get(mode, {}).get("name", mode))}</option>'
                               for mode in row["text_modes"]) + '</select></label>')
        asr_choice = (f'<label>Transcription model <select name="asr_model">' +
                      "".join(f'<option value="{esc(mode)}"{" selected" if mode == row["asr_model"] else ""}>'
                              f'Whisper {esc(mode)}</option>' for mode in row["asr_models"]) +
                      '</select></label>') if row["asr_models"] else ""
        preference = ('<label><input type="checkbox" name="prefer_ablit" value="on"'
                      + (' checked' if row["prefer_ablit"] else '') +
                      '> Prefer abliterated or uncensored models in suggestions; this does not change model behavior.</label>')
        custom_name = '<label>Copy name <input name="copy_name" maxlength="60" placeholder="My workflow"></label>'
        buttons = ('<button name="action" value="save" type="submit">Save choices</button> '
                   '<button name="action" value="load" type="submit">Load profile</button> '
                   '<button name="action" value="copy" type="submit">Save as copy</button>'
                   + (' <button name="action" value="delete" type="submit">Delete this copy</button>'
                      if row["custom"] else ''))
        button = (f'<form method="post" action="/profiles/apply"><input type="hidden" name="token" value="{esc(csrf)}">'
                  f'<input type="hidden" name="profile" value="{esc(row["id"])}">{text_choice}{checkpoint_choice}{edit_checkpoint_choice}{video_choice}{asr_choice}{preference}{custom_name}'
                  + buttons + '</form>')
        comparisons = _comparison(esc, "Language models", row["text_modes"], model_info.get("text", {}),
                                  row["text_mode"], row["base"], row["prefer_ablit"])
        if row["image"]:
            comparisons += _comparison(esc, "Image generation", row["checkpoints"],
                                       model_info.get("image", {}), row["image_checkpoint"], row["base"])
            comparisons += _comparison(esc, "Image editing", row["image_edit_checkpoints"],
                                       model_info.get("image_edit", {}), row["image_edit_checkpoint"], row["base"])
        if row["video"]:
            comparisons += _comparison(esc, "Video generation", row["video_models"],
                                       model_info.get("video", {}), row["video_model"], row["base"])
        if row["asr_models"]:
            comparisons += _comparison(esc, "Transcription", row["asr_models"],
                                       model_info.get("asr", {}), row["asr_model"], row["base"])
        cards.append(f'<section class="card"><div class="eyebrow">{esc(state)}</div><h2>{esc(row["name"])}</h2>'
                     f'<p>{esc(row["use"])}</p><p>Language model: {esc(row["text_mode"])} via '
                     '<code>local-llm-api</code>. PDF/OCR and audio run as queued CPU work. '
                     f'Image generation: {"queued on ComfyUI" if row["image_checkpoint"] else "not configured"}. '
                     f'Object removal: {"available" if row["image_edit_checkpoint"] else "install an inpainting checkpoint"}. '
                     f'Video: {esc(row["video_model"] or "not configured")}. '
                     f'ASR size: {esc(row["asr_model"])}.</p><p>{esc(row["reason"])}</p>{button}'
                     f'{comparisons}<p><a href="/models">Download or uninstall models</a> · '
                     '<a href="/benchmarks">Compare benchmarks</a></p></section>')
    current = (f'Active profile: {esc(active["name"])}; language model: {esc(active["text_mode"])}.'
               if active else 'No multimodal profile is active.')
    body = ('<section class="card"><div class="eyebrow">MULTIMODAL PROFILES</div><h1>Task profiles</h1>'
            f'<p>{current}</p><p>Detected hardware: {esc(hardware.get("gpu") or "CPU only")}; '
            f'{esc(hardware.get("ram_gib"))} GiB RAM. Profiles choose installed models and worker settings; '
            'they do not download a language or image model.</p><p>Clients continue using the same '
            'OpenAI compatible base URL and <code>local-llm-api</code> for text. '
            'Document, audio, and image tasks use dedicated paths at that base URL.</p>'
            '<p><a href="/models">Find image and video models</a> · '
            '<a href="/image-edit">Open image editor</a> · <a href="/">Back to switcher</a></p></section>')
    queue = ('<section class="card"><h2>Recent task jobs</h2><p>Jobs stay in the durable queue across worker restarts. '
             'Use the authenticated API job URL to retrieve each result.</p><table><thead><tr><th>Started UTC</th>'
             '<th>Task</th><th>Status</th><th>Stage</th></tr></thead><tbody>' +
             "".join(f'<tr><td>{esc(item["created_at"])}</td><td>{esc(item["kind"])}</td>'
                     f'<td>{esc(item["status"])}</td><td>{esc(item["stage"])}</td></tr>' for item in jobs) +
             '</tbody></table></section>')
    return shell("Multimodal profiles", body + "".join(cards) + queue)
