"""Explain and render client fairness settings without browser JavaScript."""
import datetime as dt
import html
import urllib.parse

from benchmark_views import shell
from sharing import FIELDS, defaults


def esc(value):
    return html.escape(str(value), quote=True)


def usage_graph(usage, key, title, suffix=""):
    points = usage["points"]
    peak = max((point[key] or 0 for point in points), default=0) or 1
    start = dt.datetime.fromisoformat(usage["start"])
    span = (dt.datetime.fromisoformat(usage["end"]) - start) / len(points)
    columns = []
    for number, point in enumerate(points):
        value = point[key]
        label = "unmeasured" if value is None else f"{value:,.2f}{suffix}" if isinstance(value, float) else f"{value:,}{suffix}"
        bucket = (start + span * number).strftime("%Y-%m-%d %H:%M UTC")
        height = max(0, min(100, (value or 0) / peak * 100))
        columns.append(f'<span class="usage-bar" style="height:{height:.1f}%" title="{esc(bucket)}: {esc(label)}"></span>')
    return (f'<div class="usage-chart"><h3>{esc(title)}</h3><div class="usage-bars" role="img" '
            f'aria-label="{esc(title)} across {len(points)} equal time buckets from {esc(usage["start"])} to {esc(usage["end"])}">'
            + "".join(columns) + '</div><p class="note">Older → newer; hover bars for each bucket.</p></div>')


def usage_panel(usage, label):
    totals = usage["totals"]
    choices = "".join(f'<option value="{unit}"{" selected" if unit == usage["unit"] else ""}>{unit.title()}</option>'
                      for unit in ("minutes", "hours", "days"))
    rate = f'{totals["tokens_per_second"]:,.2f}' if totals["tokens_per_second"] is not None else "Unmeasured"
    return (f'<section class="card" id="client-usage"><h2>Usage for {esc(label)}</h2>'
            '<p class="note">Totals use token counts reported by the serving engine. Streaming Chat Completions request usage explicitly. '
            'When a model or API route omits token usage, that request remains in the request count but its tokens are unmeasured. '
            'Tokens/s is reported output tokens divided by measured end-to-end request seconds, weighted across requests; '
            'it is not a GPU kernel speed measurement.</p>'
            f'<form method="get" action="/sharing"><input type="hidden" name="client" value="{esc(usage["client_id"])}">'
            f'<label for="usage-amount">Last</label><input id="usage-amount" type="number" min="1" max="129600" '
            f'name="amount" value="{usage["amount"]}" required> <select name="unit" aria-label="Usage time unit">{choices}</select> '
            '<button type="submit">Update usage window</button></form>'
            f'<p class="note">{esc(usage["start"])} to {esc(usage["end"])} UTC</p>'
            '<div class="usage-totals">'
            f'<div><span>Input tokens</span><strong>{totals["input_tokens"]:,}</strong><small>{totals["input_measured"]:,} of {totals["requests"]:,} requests measured</small></div>'
            f'<div><span>Output tokens</span><strong>{totals["output_tokens"]:,}</strong><small>{totals["output_measured"]:,} of {totals["requests"]:,} requests measured</small></div>'
            f'<div><span>Effective output tokens/s</span><strong>{rate}</strong><small>Across measured responses</small></div>'
            '</div><div class="usage-charts">'
            + usage_graph(usage, "input_tokens", "Input tokens")
            + usage_graph(usage, "output_tokens", "Output tokens")
            + usage_graph(usage, "tokens_per_second", "Effective output tokens/s", " tokens/s")
            + '</div></section>')


EXPLAIN = (
    ("requests_per_minute", "Requests per minute, per client",
     "Average admission rate for each API key. A request still counts when it waits for a generation slot. Lower values stop repeated short requests from flooding the GPU queue; they do not limit the length of one response."),
    ("burst", "Short burst, per client",
     "How many requests a client can send together when its rate bucket is full. A larger burst absorbs brief tool-call spikes, while a smaller burst spreads them out. Once exhausted, the API responds with HTTP 429 and Retry-After."),
    ("per_client_active", "Active requests per client",
     "Maximum generation requests a single API key may have in progress. One prevents an agent from taking every global slot. Each streaming response holds its place until it ends or the client disconnects."),
    ("per_client_queued", "Queued requests per client",
     "Maximum additional requests one client may leave waiting for a free slot. Keeping this small prevents one agent from filling the queue before other agents arrive."),
    ("global_active", "Global active requests",
     "Maximum requests the gateway sends to the model at once. This should normally match the model server's verified parallel slots; raising it alone does not create more GPU generation slots."),
    ("global_queued", "Global queued requests",
     "Maximum requests waiting across all clients. A full queue rejects new requests with HTTP 429 rather than letting latency grow without bound."),
    ("queue_wait_seconds", "Maximum queue wait, seconds",
     "How long a request may wait for admission. A short wait favors interactive agents; a longer wait lets batch clients absorb temporary load."),
    ("max_output_tokens", "Maximum output tokens per request",
     "Upper bound placed on OpenAI chat, completion and Responses API generation requests. Missing or larger client values are capped. This limits the time a single answer can hold a slot; long code responses may be cut short."),
    ("max_request_kib", "Maximum request body, KiB",
     "Rejects oversized request bodies before they reach the model. This is a byte limit, not an exact prompt-token limit; large documents may need a higher value."),
)


def index(settings, clients, csrf, hardware, server_slots, active_mode=None,
          parallel_status=None, created=None, message="", selected_usage=None, selected_client_label=""):
    recommended = defaults(hardware, server_slots)
    parallel_status = parallel_status or {"busy": False, "job": None}
    parallel_job = parallel_status.get("job") or {}
    parallel_result = (f'<p class="message" role="status">Latest slot test: <strong>{esc(parallel_job.get("status"))}</strong> '
                       f'· {esc(parallel_job.get("mode"))}: {esc(parallel_job.get("previous_slots"))} → '
                       f'{esc(parallel_job.get("requested_slots"))} slots · {esc(parallel_job.get("stage"))} '
                       f'{esc(parallel_job.get("error") or "")}</p>') if parallel_job else ''
    parallel_form = (f'<form method="post" action="/sharing/parallel"><input type="hidden" name="token" value="{esc(csrf)}">'
                     f'<input type="hidden" name="mode" value="{esc(active_mode)}">'
                     f'<label for="parallel-slots">Parallel generation slots for {esc(active_mode)}</label> '
                     f'<input id="parallel-slots" name="slots" type="number" min="1" max="8" step="1" value="{server_slots}" required> '
                     '<button type="submit">Test and apply slots</button></form>') if active_mode and active_mode != "media" and not parallel_status["busy"] else ('<p>Slot test running. Progress updates above without reloading this page.</p>' if parallel_status["busy"] else '<p>Start a text or vision model to change its generation slots.</p>')
    fields = "".join(
        f'<div class="setting"><label for="{key}">{esc(title)}</label>'
        f'<input id="{key}" name="{key}" type="number" min="0" step="1" value="{esc(settings[key])}" required>'
        f'<p class="note">{esc(description)}</p></div>'
        for key, title, description in EXPLAIN)
    client_rows = "".join(
        f'<tr><td>{esc(client["label"])}</td><td>{esc(client["created_at"])}</td>'
        f'<td>{"Revoked" if client["revoked_at"] else "Active"}</td><td>'
        + f'<a href="/sharing?{urllib.parse.urlencode({"client": client["id"]})}#client-usage">View usage</a> '
        + (f'<form method="post" action="/sharing/revoke"><input type="hidden" name="token" value="{esc(csrf)}">'
           f'<input type="hidden" name="client_id" value="{esc(client["id"])}">'
           '<button type="submit">Revoke key</button></form>' if not client["revoked_at"] else '')
        + '</td></tr>' for client in clients)
    token_notice = (f'<div class="card" role="status"><h2>Copy this client key now</h2><p>For {esc(created["label"])}. '
                    'The key is shown only on this response; save it in the agent’s secret store.</p>'
                    f'<code class="new-key">{esc(created["token"])}</code></div>') if created else ''
    body = (f'<p class="note">Detected GPU: <strong>{esc(hardware.get("gpu") or "unavailable")}</strong> · '
            f'VRAM: {esc(hardware.get("vram_mib") or 0)} MiB · RAM: {esc(hardware.get("ram_gib") or 0)} GiB · '
            f'CPU threads: {esc(hardware.get("cpu_threads") or 0)}. '
            f'The active model server currently reports <strong>{esc(server_slots)} generation slot(s)</strong>. '
            'The starting limits use detected VRAM and CPU capacity and do not assume a particular GPU model. '
            'If you customize values, hardware changes leave your choices in place until you reset them.</p>'
            + (f'<p class="message" role="status">{esc(message)}</p>' if message else '')
            + token_notice
            + '<div class="card"><h2>Model generation slots</h2>'
              '<p>The model server currently has the slot count shown above. Increasing it lets separate requests generate at the same time, '
              'subject to available GPU memory. With llama.cpp, the configured total context and KV memory are shared across slots; '
              'more slots can reduce context available to each request or prevent a large model from loading. '
              'vLLM uses its own sequence scheduler. Test one change at a time, and then check the model context and a real workload. '
              'The switcher restarts only the active model, waits for its API to become ready, saves the new setting on success, '
              'and restores the previous slot count if readiness fails. Active requests are interrupted during the test.</p>'
            + parallel_form + parallel_result + '</div>'
            + '<div class="card"><h2>How sharing works</h2>'
              '<p>Use one dedicated API key per agent. The existing shared API key remains valid for Open WebUI and older clients, '
              'but every caller using that key is treated as one client and shares its limits. New keys work with the same OpenAI-compatible <code>/v1</code> URL. '
              'The gateway forwards accepted requests to the active model and preserves streaming responses.</p>'
              '<p>Rate limits control how often a client starts requests. Active and queued limits control how much work it can hold at once. '
              'A rejected request receives HTTP 429 with Retry-After; the agent should back off and retry. '
              'Multiple simultaneous generations also require parallel slots in the model server, which consume context and GPU memory. '
              'Changing a gateway number alone cannot make a one-slot model run two generations.</p></div>'
            + '<div class="card"><h2>Admission and request limits</h2>'
            + f'<p class="note">Current values are {"customized" if settings["customized"] else "hardware defaults"}. '
              f'Hardware recommendation for the active model: {recommended["global_active"]} global active, '
              f'{recommended["per_client_active"]} active per client, {recommended["requests_per_minute"]} requests/minute per client.</p>'
            + f'<form method="post" action="/sharing/save"><input type="hidden" name="token" value="{esc(csrf)}">'
            + '<div class="settings-grid">' + fields + '</div><button type="submit">Save sharing limits</button></form>'
            + f'<form method="post" action="/sharing/reset"><input type="hidden" name="token" value="{esc(csrf)}">'
              '<button type="submit" class="secondary">Reset to detected hardware defaults</button></form></div>'
            + '<div class="card"><h2>Client API keys</h2><p>Create a separate key for each agent so its usage can be limited independently. '
              'Keys are stored as hashes; the full value is shown once. Revocation takes effect on the next request. '
              'Existing in-flight requests continue until they finish.</p>'
            + f'<form method="post" action="/sharing/client"><input type="hidden" name="token" value="{esc(csrf)}">'
              '<label for="client-label">Agent or application name</label> '
              '<input id="client-label" name="label" maxlength="64" required placeholder="e.g. deck-builder"> '
              '<button type="submit">Create client key</button></form>'
            + '<div class="scroll"><table><tr><th>Client</th><th>Created UTC</th><th>Status</th><th>Action</th></tr>'
            + '<tr><td>Shared legacy key</td><td>Before dedicated keys</td><td>Active</td><td><a href="/sharing?client=legacy#client-usage">View usage</a></td></tr>'
            + client_rows + '</table></div></div>'
            + (usage_panel(selected_usage, selected_client_label) if selected_usage else ''))
    style = """<style>.settings-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1rem}.setting{padding:.8rem;border:1px solid #344b50;border-radius:.6rem;background:#122126}.setting label{display:block;margin-bottom:.3rem}.setting input{width:8rem}.setting p{font-size:.9rem;margin:.5rem 0 0}.secondary{background:#22373a;color:#e6d3b3;border-color:#557070}.secondary:hover{background:#30484b}.new-key{display:block;overflow-wrap:anywhere;padding:1rem;background:#0b181c;border:1px solid #4d696a;border-radius:.5rem}.usage-totals{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:.8rem}.usage-totals>div{display:grid;gap:.2rem;background:#102024;border:1px solid #365056;border-radius:.7rem;padding:1rem}.usage-totals strong{font-size:clamp(1.3rem,3vw,2rem);color:#f1bf81}.usage-totals small{color:#b6c6c4}.usage-charts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:.8rem;margin-top:1rem}.usage-chart{background:#102024;border:1px solid #365056;border-radius:.7rem;padding:1rem;min-width:0}.usage-chart h3{margin:.1rem 0 .8rem}.usage-chart p{font-size:.78rem}.usage-bars{height:9rem;display:flex;align-items:end;gap:2px;border-bottom:1px solid #8ba9a1}.usage-bar{flex:1;min-width:2px;background:linear-gradient(#f1bf81,#a87645);border-radius:3px 3px 0 0}@media(max-width:900px){.usage-charts{grid-template-columns:1fr}}@media(max-width:760px){.settings-grid,.usage-totals{grid-template-columns:1fr}}</style>"""
    return shell("Client sharing and rate limits", style + body, refresh=parallel_status["busy"], wide=True)
