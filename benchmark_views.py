"""Escaped, no-JavaScript pages for persisted benchmark results."""
import html
import os
import json
import statistics
import urllib.parse

from benchmarks import MODELS, SUITE_VERSION


def esc(value):
    return html.escape(str(value if value is not None else "—"), quote=True)


STYLE = """*{box-sizing:border-box}html{color-scheme:dark}body{font:16px/1.55 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;background:#0c1418;color:#edf2ef}body:before{content:"";position:fixed;inset:0;z-index:-1;background:radial-gradient(circle at 5% 0%,#1b3034 0,transparent 34%),radial-gradient(circle at 95% 60%,#18272a 0,transparent 36%)}main{width:min(100% - 2.5rem,90rem);margin:0 auto;padding:1.35rem 0 3rem}body.wide main{width:calc(100% - 2rem)}h1,h2,h3{line-height:1.12;letter-spacing:-.035em}h1{font-size:clamp(2rem,4vw,3.7rem);margin:.7rem 0}h2{font-size:clamp(1.35rem,2vw,1.85rem);margin:2rem 0 .8rem}h3{font-size:1.12rem}p{margin:.65rem 0 1rem}a{color:#f1bf81;text-decoration-color:#a47c4f;text-underline-offset:.2em}a:hover{color:#ffe0ad}a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:2px solid #f5c786;outline-offset:3px}nav{display:flex;align-items:center;gap:.35rem;flex-wrap:wrap;margin-bottom:1.1rem;color:#748c91;font-size:.91rem}nav a{display:inline-block;padding:.5rem .8rem;border-radius:99px;color:#d6e2df;text-decoration:none}nav a:first-child{font-weight:750;color:#f7c586;letter-spacing:.02em;margin-right:auto}nav a:hover{background:#26383a}button,select,input{font:inherit}button{cursor:pointer;background:#e9b977;border:1px solid #f5ce96;color:#172126;border-radius:.55rem;padding:.62rem .9rem;font-weight:700;min-height:2.65rem}button:hover{background:#ffce8b}button:disabled{opacity:.45;cursor:not-allowed}select,input[type=number],input[type=date],input[type=text]{color:#eff5f2;background:#101d21;border:1px solid #466066;border-radius:.5rem;padding:.62rem .75rem;min-height:2.65rem;max-width:100%}label{margin-right:.55rem;font-weight:600}form{margin:.6rem 0}table{border-collapse:separate;border-spacing:0;width:100%;margin:1rem 0;background:#152327;border:1px solid #365056;border-radius:.6rem}th,td{border-bottom:1px solid #31464b;padding:.7rem;text-align:left;vertical-align:top}tr:last-child td{border-bottom:0}th{background:#203236;color:#d9e8e4;font-size:.86em;letter-spacing:.015em}tbody tr:hover{background:#203136}.group th{background:#344649}th a{color:#f6c98d;white-space:normal}.card{background:#17262a;border:1px solid #344b50;padding:clamp(1rem,2vw,1.5rem);border-radius:.9rem;margin:1rem 0;box-shadow:0 10px 40px #0002}.note{color:#b6c6c4}.scroll{overflow-x:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#101b1f;border:1px solid #35494d;border-radius:.65rem;padding:1rem}.hero{position:relative;isolation:isolate;min-height:15rem;display:flex;align-items:center;overflow:hidden;border:1px solid #435653;border-radius:1rem;background:#142328 url('/assets/gpu-workstation.webp') center right/cover no-repeat;padding:clamp(1.4rem,3.5vw,3rem);box-shadow:0 20px 60px #0004}.hero:before{content:"";position:absolute;z-index:-1;inset:0;background:linear-gradient(90deg,#0e1b1f 0%,#102025ed 35%,#10202588 68%,#10202533 100%)}.hero-copy{max-width:42rem}.eyebrow{text-transform:uppercase;font-size:.73rem;letter-spacing:.18em;color:#e8ba78;font-weight:800}.hero p{color:#cbd9d5;max-width:38rem;font-size:1.03rem}.hero h1{color:#fff7e9;text-wrap:balance}.results-table{table-layout:fixed;font-size:clamp(.68rem,.85vw,.9rem)}.results-table th,.results-table td{padding:.4rem .3rem;overflow-wrap:anywhere;word-break:normal}.results-table td form{margin:.2rem 0 0}.results-table td button{padding:.2rem .35rem;font-size:inherit;min-height:1.8rem}.results-table-wrapper{width:100%;overflow-x:visible}@media(max-width:740px){main,body.wide main{width:calc(100% - 1.25rem)}nav a:first-child{width:100%}.hero{min-height:13rem;padding:1.3rem;background-position:62% center}.hero:before{background:linear-gradient(90deg,#0e1b1ff7 0%,#102025e8 60%,#10202588 100%)}.scroll,.results-table-wrapper{overflow-x:auto}.results-table{table-layout:auto;min-width:72rem}table:not(.results-table){display:block;overflow-x:auto}button{max-width:100%}}@media(prefers-reduced-motion:no-preference){button,a{transition:background-color .15s,color .15s}}"""


STYLE += """.section-links{display:flex;gap:.5rem;flex-wrap:wrap;margin:1rem 0 1.5rem}.section-links a{display:inline-block;padding:.48rem .75rem;border:1px solid #435a5a;border-radius:99px;background:#17272a;color:#e9c18b;text-decoration:none;font-size:.88rem;font-weight:650}.section-links a:hover{background:#274044}.overview-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1rem}.overview-grid .card{margin:0}.overview-grid h2{margin:.2rem 0 .7rem}@media(max-width:900px){.overview-grid{grid-template-columns:1fr}}"""

STYLE += """
.installed-models-table th:last-child,.installed-models-table td:last-child{width:20rem}
.installed-models-table td:first-child{overflow-wrap:anywhere}
.model-meta,.model-source{display:block;font-size:.84rem;line-height:1.4;margin-top:.25rem}
.model-source{width:max-content;max-width:100%;margin-top:.4rem}
.model-actions{display:grid;gap:.4rem;max-width:19rem}
.model-actions form{margin:0}
.model-actions button{width:100%}
.model-action-panel{border:1px solid #385056;border-radius:.55rem;background:#102024}
.model-action-panel summary{cursor:pointer;padding:.45rem .7rem;font-size:.88rem;font-weight:650;color:#d6e5e1}
.model-action-panel summary:hover{background:#203539}
.model-action-panel[open] form{padding:0 .7rem .7rem}
.model-action-panel label{display:block;margin:0;font-size:.82rem;color:#c6d4d0}
.model-action-panel .model-delete-check{cursor:pointer;line-height:1.4}
.model-delete-check input{accent-color:#e9b977;vertical-align:middle;margin-right:.35rem}
.model-action-panel input[type=text]{display:block;width:100%;margin-top:.35rem}
.model-action-panel button{margin-top:.6rem}
.model-action-panel.danger summary{color:#efb0aa}
.model-action-panel.danger button{background:#6e3735;border-color:#a85c56;color:#fff1ed}
.model-action-panel.danger button:hover{background:#894440}
.model-action-note{margin:.3rem 0;font-size:.84rem}
@media(max-width:740px){.installed-models-table th:last-child,.installed-models-table td:last-child{min-width:15rem}}
"""


def shell(title, body, refresh=False, wide=False, monitor_url=None):
    if refresh and not monitor_url:
        monitor_url = ("/operations/status?scope=models" if title in ("Hugging Face model recommendations", "Hardware and model choices") else
                       "/operations/status?scope=sharing" if title == "Client sharing and rate limits" else None)
    monitor = (f'<p class="message" role="status" data-operation-monitor="{esc(monitor_url)}">Checking operation status…</p>'
               '<script src="/assets/operation_poll.js" defer></script>') if monitor_url else ""
    body_class = ' class="wide"' if wide else ""
    description = ("Compare saved speed and quality results for this machine." if title == "Model benchmarks" else
                   "Find models sized for the hardware in this system." if title == "Hugging Face model recommendations" else
                   "Compare coding agents and connect them to your local API." if title == "Coding harnesses" else
                   "Inspect this model's recorded runs and system details.")
    display_title = "Find models" if title == "Hugging Face model recommendations" else title
    return (f'<!doctype html><html lang="en"><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{esc(title)}</title><style>{STYLE}</style><body{body_class}><main><nav aria-label="Main navigation"><a href="/">AI Model Switcher</a>'
            f'<a href="/benchmarks">Benchmarks</a><a href="/models">Find models</a><a href="/sharing">Client sharing</a><a href="/harnesses">Coding harnesses</a></nav>'
            f'<header class="hero"><div class="hero-copy"><div class="eyebrow">Local AI / Control desk</div><h1>{esc(display_title)}</h1><p>{description}</p></div></header>{monitor}{body}</main></body></html>')


def summary(run):
    result = json.loads(run["results_json"] or "{}")
    mail = result.get("mail_summary") or {}
    code = result.get("code_summary") or {}
    image = result.get("image_generation") or {}
    system = json.loads(run["system_json"] or "{}")
    return {
        "inbound_f1": (mail.get("inbound") or {}).get("f1"),
        "outbound_f1": (mail.get("outbound") or {}).get("f1"),
        "spam_fpr": (mail.get("overall") or {}).get("false_positive_rate"),
        "p95": (mail.get("overall") or {}).get("p95_seconds"),
        "mail_rate": (mail.get("overall") or {}).get("sequential_messages_per_minute"),
        "tokens_per_second": (result.get("generation") or {}).get("end_to_end_tokens_per_second"),
        "code": f"{code.get('functional_passes') if code.get('functional_passes') is not None else code.get('syntax_and_function_passes')}/{code.get('total')}" if code else None,
        "image": f"{image.get('seconds')} s" if image.get("status") == "completed" else "NA" if image.get("status") == "not_applicable" else image.get("status"),
        "gpu": (system.get("gpu") or {}).get("name"),
        "context": (system.get("serving_config") or {}).get("--ctx-size") or (system.get("serving_config") or {}).get("--max-model-len"),
    }


COLUMNS = (
    ("started_at", "UTC start"), ("model_id", "Model"), ("status", "Status"),
    ("gpu", "GPU"), ("context", "Context"), ("inbound_f1", "Inbound F1"),
    ("outbound_f1", "Outbound F1"), ("spam_fpr", "Spam FPR"),
    ("p95", "Mail p95 s"), ("mail_rate", "Sequential emails/min"),
    ("tokens_per_second", "Tokens/s"), ("code", "Code"),
    ("image", "Image"), ("id", "Runs"),
)
NUMERIC = {"context", "inbound_f1", "outbound_f1", "spam_fpr", "p95",
           "mail_rate", "tokens_per_second", "code", "image"}
TEXT_ASCENDING = {"model_id", "status", "gpu", "id"}


def sort_value(run, metric, column):
    value = run.get(column) if column in ("started_at", "model_id", "status", "id") else metric.get(column)
    if column == "code" and value:
        value = value.split("/", 1)[0]
    if column == "image" and isinstance(value, str) and value.endswith(" s"):
        value = value[:-2]
    if value is None or value == "" or (column == "image" and value == "not_applicable"):
        return None
    if column in NUMERIC:
        try:
            return float(value)
        except (ValueError, TypeError):
            return None
    return str(value).casefold()


def average_summary(runs):
    metrics = [summary(run) for run in runs]
    merged = {}
    for key in metrics[0]:
        values = [metric[key] for metric in metrics if metric[key] is not None]
        if key in ("gpu", "context"):
            merged[key] = values[0] if values and len(values) == len(metrics) and all(value == values[0] for value in values) else "Mixed" if values else None
        elif key == "code":
            ratios = [value.split("/", 1) for value in values if "/" in str(value)]
            ratios = [(float(a), b) for a, b in ratios if a.replace(".", "", 1).isdigit()]
            merged[key] = f'{statistics.mean(a for a, _ in ratios):.1f}/{ratios[0][1]}' if ratios and all(b == ratios[0][1] for _, b in ratios) else None
        elif key == "image":
            seconds = [float(value[:-2]) for value in values if isinstance(value, str) and value.endswith(" s")]
            merged[key] = f'{statistics.mean(seconds):.2f} s' if seconds else "NA" if values and all(value == "NA" for value in values) else "Mixed" if values else None
        else:
            numeric = [float(value) for value in values if isinstance(value, (int, float))]
            merged[key] = round(statistics.mean(numeric), 3) if numeric else None
    return merged


def combined_runs(runs):
    by_model = {}
    for run in runs:
        by_model.setdefault(run["model_id"], []).append(run)
    combined = []
    for model_id, model_runs in by_model.items():
        latest = max(model_runs, key=lambda run: run["started_at"])
        statuses = {run["status"] for run in model_runs}
        combined.append(({**latest, "status": next(iter(statuses)) if len(statuses) == 1 else "Mixed",
                          "count": len(model_runs), "active": bool(statuses & {"queued", "running"})}, average_summary(model_runs)))
    return combined


def sorted_rows(runs, column="tokens_per_second", direction="desc", group_context=False):
    if column not in dict(COLUMNS):
        column = "tokens_per_second"
    if direction not in ("asc", "desc"):
        direction = "desc"
    records = combined_runs(runs)

    def ordered(items):
        present = [item for item in items if sort_value(*item, column) is not None]
        missing = [item for item in items if sort_value(*item, column) is None]
        present.sort(key=lambda item: sort_value(*item, column), reverse=direction == "desc")
        return present + missing

    if not group_context:
        return [(None, ordered(records))]
    groups = {}
    for item in records:
        context = sort_value(*item, "context")
        groups.setdefault(context, []).append(item)
    sizes = sorted((size for size in groups if size is not None),
                   reverse=not (column == "context" and direction == "asc"))
    if None in groups:
        sizes.append(None)
    return [(size, ordered(groups[size])) for size in sizes]


def index(runs, csrf, message="", active_mode=None, sort="tokens_per_second", direction="desc", group_context=False):
    if sort not in dict(COLUMNS):
        sort = "tokens_per_second"
    if direction not in ("asc", "desc"):
        direction = "desc"
    selected_modes = {value for value in os.environ.get("AI_SELECTED_MODELS", "").split(",") if value}
    options = "".join(f'<option value="{esc(mode)}" {"selected" if mode == active_mode else ""}>{esc(alias)} ({esc(mode)})</option>'
                      for mode, (alias, _) in MODELS.items()
                      if not selected_modes or mode in selected_modes or mode.startswith("hf-"))
    headers = []
    for key, title in COLUMNS:
        next_direction = ("asc" if direction == "desc" else "desc") if key == sort else ("asc" if key in TEXT_ASCENDING else "desc")
        params = {"sort": key, "dir": next_direction}
        if group_context:
            params["group"] = "context"
        href = "/benchmarks?" + urllib.parse.urlencode(params)
        arrow = " ▼" if key == sort and direction == "desc" else " ▲" if key == sort else ""
        aria = direction.replace("asc", "ascending").replace("desc", "descending") if key == sort else "none"
        headers.append(f'<th scope="col" aria-sort="{aria}"><a href="{esc(href)}">{esc(title + arrow)}</a></th>')
    rows = []
    for context, items in sorted_rows(runs, sort, direction, group_context):
        if group_context and items:
            label = "Mixed or no context" if context is None else f"Context: {int(context):,} tokens"
            rows.append(f'<tr class="group"><th colspan="{len(COLUMNS)}" scope="rowgroup">{esc(label)}</th></tr>')
        for run, metric in items:
            values = [run["started_at"], run["model_id"], run["status"], metric["gpu"], metric["context"],
                      metric["inbound_f1"], metric["outbound_f1"], metric["spam_fpr"], metric["p95"],
                      metric["mail_rate"], metric["tokens_per_second"], metric["code"], metric["image"]]
            cells = "".join(f"<td>{esc(value)}</td>" for value in values)
            model_query = urllib.parse.urlencode({"model_id": run["model_id"]})
            disabled = " disabled" if run["active"] else ""
            delete = (f'<form method="post" action="/benchmarks/delete"><input type="hidden" name="token" value="{esc(csrf)}">'
                      f'<input type="hidden" name="model_id" value="{esc(run["model_id"])}"><button type="submit"{disabled}>Delete all</button></form>')
            rows.append(f'<tr>{cells}<td>{run["count"]} · <a href="/benchmarks/model?{esc(model_query)}">Details</a>{delete}</td></tr>')
    header = "".join(headers)
    widths = (10, 15, 6, 8, 6, 6, 6, 6, 6, 7, 6, 5, 5, 8)
    columns = "<colgroup>" + "".join(f'<col style="width:{width}%">' for width in widths) + "</colgroup>"
    grouped = " checked" if group_context else ""
    body = (f'<p class="note">Suite {esc(SUITE_VERSION)}. The fixed synthetic email set is a small comparison smoke test, not a production accuracy estimate. '
            'Code scores run a few functional checks in a disposable offline container with no mounted files. '
            'Tokens/s is completion tokens divided by total request time. Image generation runs only for ComfyUI. '
            'Benchmarking switches the active GPU workload and leaves the selected mode active. Repeated runs for each model are averaged; the start time is the latest run. Missing metrics are omitted from their average.</p>'
            f'<div class="card"><h2>Run benchmark</h2><p>{esc(message)}</p><form method="post" action="/benchmark">'
            f'<input type="hidden" name="token" value="{esc(csrf)}"><label for="mode">Select workload</label> '
            f'<select id="mode" name="mode">{options}</select> <button type="submit">Start benchmark</button></form></div>'
            f'<form method="get" action="/benchmarks"><input type="hidden" name="sort" value="{esc(sort)}">'
            f'<input type="hidden" name="dir" value="{esc(direction)}"><label><input type="checkbox" name="group" value="context"{grouped}> '
            'Group rows by context size</label> <button type="submit">Apply</button></form>'
            f'<form method="post" action="/benchmarks/delete"><input type="hidden" name="token" value="{esc(csrf)}">'
            '<label for="before">Delete benchmarks before date (UTC)</label> '
            '<input id="before" type="date" name="before" required> <button type="submit">Delete older runs</button></form>'
            f'<div class="results-table-wrapper"><table class="results-table">{columns}<thead><tr>{header}</tr></thead><tbody>{"".join(rows) if rows else f"<tr><td colspan={len(COLUMNS)}>No runs yet</td></tr>"}</tbody></table></div>')
    return shell("Model benchmarks", body, wide=True)


def model_detail(model_id, runs, csrf):
    if not runs:
        return shell("Model benchmark details", f'<p>No runs found for {esc(model_id)}.</p>')
    aggregate = average_summary(runs)
    rows = []
    for run in runs:
        metric = summary(run)
        values = (run["started_at"], run["status"], metric["context"], metric["tokens_per_second"],
                  metric["inbound_f1"], metric["outbound_f1"], metric["spam_fpr"], metric["p95"],
                  metric["mail_rate"], metric["code"], metric["image"])
        cells = "".join(f'<td>{esc(value)}</td>' for value in values)
        disabled = " disabled" if run["status"] in ("queued", "running") else ""
        delete = (f'<form method="post" action="/benchmarks/delete"><input type="hidden" name="token" value="{esc(csrf)}">'
                  f'<input type="hidden" name="run_id" value="{esc(run["id"])}"><button type="submit"{disabled}>Delete run</button></form>')
        rows.append(f'<tr>{cells}<td><a href="/benchmarks/{esc(run["id"])}">Run details</a>{delete}</td></tr>')
    aggregate_rows = "".join(f'<tr><th>{esc(key.replace("_", " ").title())}</th><td>{esc(value)}</td></tr>' for key, value in aggregate.items())
    body = (f'<p><strong>Model:</strong> {esc(model_id)} · <strong>Runs:</strong> {len(runs)} · '
            f'<strong>Latest start:</strong> {esc(max(run["started_at"] for run in runs))} UTC</p>'
            f'<div class="card"><h2>Averages</h2><table>{aggregate_rows}</table></div>'
            '<div class="results-table-wrapper"><table class="results-table"><thead><tr>'
            '<th>UTC start</th><th>Status</th><th>Context</th><th>Tokens/s</th><th>Inbound F1</th>'
            '<th>Outbound F1</th><th>Spam FPR</th><th>Mail p95 s</th><th>Emails/min</th><th>Code</th><th>Image</th><th>Action</th>'
            f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>')
    return shell("Model benchmark details", body, wide=True)


def detail(run):
    system = json.loads(run["system_json"] or "{}")
    result = json.loads(run["results_json"] or "{}")
    state = f'<p><strong>Status:</strong> {esc(run["status"])} · <strong>Stage:</strong> {esc(run["stage"])} · <strong>Started:</strong> {esc(run["started_at"])} UTC · <strong>Ended:</strong> {esc(run["ended_at"])} UTC</p>'
    state += f'<p><strong>Selected model:</strong> {esc(run["model_id"])} · <strong>Run ID:</strong> {esc(run["id"])}</p>'
    if run.get("error"):
        state += f'<p><strong>Error:</strong> {esc(run["error"])}</p>'
    if run["status"] in ("queued", "running"):
        state += '<p class="note">Progress updates above without reloading this page.</p>'
    state += f'<p><a href="/benchmarks/{esc(run["id"])}.json">Download run JSON</a></p>'

    hardware = {
        "Host OS": system.get("host_os"), "Kernel": system.get("kernel"), "CPU": system.get("cpu_model"),
        "CPU threads": system.get("cpu_threads"), "RAM visible to Docker GiB": system.get("ram_gib"),
        "GPU": (system.get("gpu") or {}).get("name"), "GPU VRAM MiB": (system.get("gpu") or {}).get("vram_mib"),
        "NVIDIA driver": (system.get("gpu") or {}).get("driver"), "PCI bus": (system.get("gpu") or {}).get("pci_bus_id"),
        "Docker": system.get("docker_version"), "Container image": system.get("container_image"),
        "Image ID": system.get("container_image_id"), "Suite": system.get("suite_version"),
    }
    hardware.update({f"Serve {key}": value for key, value in (system.get("serving_config") or {}).items()})
    hardware_rows = "".join(f"<tr><th>{esc(key)}</th><td>{esc(value)}</td></tr>" for key, value in hardware.items())
    body = state + f'<div class="card"><h2>System and model configuration</h2><table>{hardware_rows}</table></div>'

    generation = result.get("generation") or {}
    if generation:
        body += (f'<div class="card"><h2>Text generation</h2><p>End to end: {esc(generation.get("seconds"))} s · '
                 f'Completion tokens: {esc(generation.get("completion_tokens"))} · '
                 f'Effective completion tokens/s: {esc(generation.get("end_to_end_tokens_per_second"))} · '
                 f'Final answer present: {esc(generation.get("has_answer"))}</p></div>')

    mail = result.get("mail_summary") or {}
    if mail:
        headings = ("Direction", "N", "TP", "TN", "FP", "FN", "Invalid", "Precision", "Recall", "F1", "FPR", "p50 s", "p95 s", "Sequential emails/min")
        header = "".join(f"<th>{esc(x)}</th>" for x in headings)
        rows = []
        for direction in ("inbound", "outbound", "overall"):
            m = mail[direction]
            values = (direction, m["count"], m["tp"], m["tn"], m["fp"], m["fn"], m["invalid"],
                      m["precision"], m["recall"], m["f1"], m["false_positive_rate"], m["p50_seconds"], m["p95_seconds"], m.get("sequential_messages_per_minute"))
            rows.append("<tr>" + "".join(f"<td>{esc(x)}</td>" for x in values) + "</tr>")
        body += f'<div class="card scroll"><h2>Email classification</h2><table><thead><tr>{header}</tr></thead><tbody>{"".join(rows)}</tbody></table>'
        body += '<p class="note">SPAM is positive. Invalid output counts as a missed spam detection. FPR = false positives / all HAM examples. p95 is end to end latency. Emails/min is sequential throughput with no concurrent requests.</p>'
        case_rows = "".join("<tr>" + "".join(f"<td>{esc(case.get(key))}</td>" for key in ("id", "direction", "expected", "predicted", "seconds", "error")) + "</tr>" for case in result["mail_cases"])
        body += f'<details><summary>Individual synthetic email cases</summary><table><tr><th>#</th><th>Direction</th><th>Expected</th><th>Predicted</th><th>Seconds</th><th>Error</th></tr>{case_rows}</table></details></div>'

    code = result.get("code_cases") or []
    if code:
        score = result["code_summary"]
        body += f'<div class="card"><h2>Code generation</h2><p>Python syntax and function name: {esc(score["syntax_and_function_passes"])}/{esc(score["total"])}. Functional checks: {esc(score.get("functional_passes"))}/{esc(score["total"])}. Generated code ran only in a disposable offline container with no mounted files and capped resources.</p>'
        for case in code:
            body += (f'<details><summary>{esc(case["function"])} · syntax {esc(case["syntax_ok"])} · function {esc(case["function_present"])} · {esc(case["seconds"])} s</summary>'
                     f'<p>Functional pass: {esc((case.get("functional") or {}).get("passed"))}; test output: {esc((case.get("functional") or {}).get("output"))}</p>'
                     f'<pre>{esc(case.get("output"))}</pre></details>')
        body += '</div>'

    image = result.get("image_generation") or {}
    if image:
        body += f'<div class="card"><h2>Image generation</h2><p>Status: {esc(image.get("status"))} · Time: {esc(image.get("seconds"))} s · Resolution: {esc(image.get("resolution"))} · Steps: {esc(image.get("steps"))} · Checkpoint: {esc(image.get("checkpoint"))}</p>'
        if image.get("filename"):
            image_url = f'/benchmarks/{run["id"]}/image'
            body += f'<p><a href="{esc(image_url)}">Open saved image</a></p><a href="{esc(image_url)}"><img src="{esc(image_url)}" alt="Benchmark generated image" style="max-width:512px;width:100%"></a>'
        if image.get("error") or image.get("reason"):
            body += f'<p>{esc(image.get("error") or image.get("reason"))}</p>'
        body += '</div>'
    active = run["status"] in ("queued", "running")
    return shell("Benchmark run", body, active,
                 monitor_url="/operations/status?scope=benchmark&run_id=" + urllib.parse.quote(run["id"]) if active else None)
