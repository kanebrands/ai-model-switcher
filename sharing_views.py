"""Explain and render client fairness settings without browser JavaScript."""
import datetime as dt
import html

from benchmark_views import shell
from sharing import FIELDS, defaults


def esc(value):
    return html.escape(str(value), quote=True)


def usage_graph(usage, key, title, suffix="", compact=False):
    points = usage["points"]
    peak = max((point[key] or 0 for point in points), default=0) or 1
    start = dt.datetime.fromisoformat(usage["start"])
    span = (dt.datetime.fromisoformat(usage["end"]) - start) / len(points)
    date_format = "%m/%d" if span * len(points) >= dt.timedelta(days=1) else "%H:%M"
    markers = ((start, 40), (start + span * len(points) / 2, 180), (start + span * len(points), 320))
    grid = ''.join(f'<line x1="40" y1="{y}" x2="320" y2="{y}" class="plot-grid"/>'
                   f'<text x="35" y="{y + 3}" text-anchor="end">{esc(f"{value:,.1f}" if isinstance(value, float) else f"{value:,}")}</text>'
                   for y, value in ((12, peak), (45, round(peak / 2, 1)), (78, 0)))
    grid += ''.join(f'<line x1="{x}" y1="12" x2="{x}" y2="78" class="plot-grid"/>'
                    f'<text x="{x}" y="99" text-anchor="middle">{esc(moment.strftime(date_format))}</text>'
                    for moment, x in markers)
    columns = []
    for number, point in enumerate(points):
        value = point[key]
        label = "unmeasured" if value is None else f"{value:,.2f}{suffix}" if isinstance(value, float) else f"{value:,}{suffix}"
        bucket = (start + span * number).strftime("%Y-%m-%d %H:%M UTC")
        height = max(0, min(66, (value or 0) / peak * 66))
        columns.append(f'<rect x="{42 + number * 276 / len(points):.1f}" y="{78 - height:.1f}" '
                       f'width="{max(2, 276 / len(points) - 1):.1f}" height="{height:.1f}" class="plot-bar">'
                       f'<title>{esc(bucket)}: {esc(label)}</title></rect>')
    return (f'<div class="usage-chart{" compact" if compact else ""}"><h3>{esc(title)}</h3>'
            f'<svg viewBox="0 0 350 108" role="img" aria-label="{esc(title)} from {esc(usage["start"])} to {esc(usage["end"])}; y axis 0 to {esc(peak)}">'
            + grid + "".join(columns) + '</svg>' + ('' if compact else '<p class="note">UTC time; hover bars for bucket values.</p>') + '</div>')


def window_form(usage, action):
    amounts = "".join(f'<option value="{amount}"{" selected" if amount == usage["amount"] else ""}>{amount}</option>'
                      for amount in sorted({*range(1, 91), usage["amount"]}))
    units = "".join(f'<option value="{unit}"{" selected" if unit == usage["unit"] else ""}>{unit.title()}</option>'
                    for unit in ("minutes", "hours", "days"))
    return (f'<form class="usage-window" method="get" action="{esc(action)}">'
            f'<label>Last <select name="all_amount" aria-label="Usage window amount">{amounts}</select></label>'
            f'<label>Unit <select name="all_unit" aria-label="Usage time unit">{units}</select></label>'
            '<button type="submit">Update window</button></form>')


def usage_panel(usage, label, embedded=False, action="/sharing", show_window=True):
    totals = usage["totals"]
    rate = f'{totals["tokens_per_second"]:,.2f}' if totals["tokens_per_second"] is not None else "Unmeasured"
    panel_id = "all-key-usage" if embedded else "client-usage"
    container = '<div class="all-key-usage"' if embedded else '<section class="card"'
    close = '</div>' if embedded else '</section>'
    return (f'{container} id="{panel_id}"><h2>Usage for {esc(label)}</h2>'
            '<p class="note">Totals use token counts reported by the serving engine. Streaming Chat Completions request usage explicitly. '
            'When a model or API route omits token usage, that request remains in the request count but its tokens are unmeasured. '
            'Tokens/s is reported output tokens divided by measured end-to-end request seconds, weighted across requests; '
            'it is not a GPU kernel speed measurement.</p>'
            + (window_form(usage, action) if show_window else '')
            + f'<p class="note">{esc(usage["start"])} to {esc(usage["end"])} UTC</p>'
            '<div class="usage-totals">'
            f'<div><span>Input tokens</span><strong>{totals["input_tokens"]:,}</strong><small>{totals["input_measured"]:,} of {totals["requests"]:,} requests measured</small></div>'
            f'<div><span>Output tokens</span><strong>{totals["output_tokens"]:,}</strong><small>{totals["output_measured"]:,} of {totals["requests"]:,} requests measured</small></div>'
            f'<div><span>Effective output tokens/s</span><strong>{rate}</strong><small>Across measured responses</small></div>'
            '</div><div class="usage-charts">'
            + usage_graph(usage, "input_tokens", "Input tokens")
            + usage_graph(usage, "output_tokens", "Output tokens")
            + usage_graph(usage, "tokens_per_second", "Effective output tokens/s", " tokens/s")
            + '</div>' + close)


def compact_usage(usage):
    totals = usage["totals"]
    rate = f'{totals["tokens_per_second"]:,.2f}' if totals["tokens_per_second"] is not None else "Unmeasured"
    return ('<div class="client-usage-mini"><div class="mini-totals">'
            f'<div>Input tokens <strong>{totals["input_tokens"]:,}</strong><small>{totals["input_measured"]:,}/{totals["requests"]:,} measured</small></div>'
            f'<div>Output tokens <strong>{totals["output_tokens"]:,}</strong><small>{totals["output_measured"]:,}/{totals["requests"]:,} measured</small></div>'
            f'<div>Output tokens/s <strong>{rate}</strong><small>End-to-end rate</small></div>'
            '</div><div class="mini-charts">'
            + usage_graph(usage, "input_tokens", "Input tokens", compact=True)
            + usage_graph(usage, "output_tokens", "Output tokens", compact=True)
            + usage_graph(usage, "tokens_per_second", "Output tokens/s", compact=True)
            + '</div></div>')


USAGE_STYLE = """.usage-window{display:flex;align-items:end;flex-wrap:wrap;gap:.6rem;margin:.7rem 0}.usage-window label{display:grid;gap:.2rem;font-size:.83rem;color:#c5d5d2}.usage-window select{box-sizing:border-box;width:9rem;min-width:9rem;max-width:9rem;height:2.65rem}.usage-window button{margin:0;min-height:2.65rem}.usage-totals{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:.8rem}.usage-totals>div{display:grid;gap:.2rem;background:#102024;border:1px solid #365056;border-radius:.7rem;padding:1rem}.usage-totals strong{font-size:clamp(1.3rem,3vw,2rem);color:#f1bf81}.usage-totals small{color:#b6c6c4}.usage-charts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:.8rem;margin-top:1rem}.usage-chart{background:#102024;border:1px solid #365056;border-radius:.7rem;padding:1rem;min-width:0}.usage-chart h3{margin:.1rem 0 .5rem}.usage-chart p{font-size:.78rem}.usage-chart svg{display:block;width:100%;height:auto}.usage-chart svg text{fill:#b6c8c5;font-size:9px;font-family:system-ui,sans-serif}.usage-chart .plot-grid{stroke:#50666a;stroke-width:.7;stroke-dasharray:2 3}.usage-chart .plot-bar{fill:#e9b977}.client-usage-mini{padding:.2rem 0}.mini-totals,.mini-charts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:.5rem}.mini-totals>div{display:flex;align-items:baseline;gap:.35rem;flex-wrap:wrap;font-size:.78rem;color:#b6c6c4}.mini-totals strong{font-size:1rem;color:#f1bf81}.mini-totals small{font-size:.7rem}.mini-charts{margin-top:.4rem}.mini-charts .usage-chart{padding:.35rem .5rem;border-radius:.4rem}.mini-charts .usage-chart h3{font-size:.74rem;margin:0}.mini-charts .usage-chart svg{max-height:92px}.client-usage-row>td{padding:.5rem .8rem .7rem;background:#102024}.client-main-row>td{border-bottom:0}@media(max-width:900px){.usage-charts{grid-template-columns:1fr}}@media(max-width:760px){.usage-totals,.mini-charts,.mini-totals{grid-template-columns:1fr}}"""


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
          parallel_status=None, created=None, message="", all_usage=None, client_usage=None):
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
    client_usage = client_usage or {}
    def client_row(client):
        action = (f'<form method="post" action="/sharing/revoke"><input type="hidden" name="token" value="{esc(csrf)}">'
                  f'<input type="hidden" name="client_id" value="{esc(client["id"])}">'
                  '<button type="submit">Revoke key</button></form>') if not client["revoked_at"] else ''
        return (f'<tr class="client-main-row"><td>{esc(client["label"])}</td><td>{esc(client["created_at"])}</td>'
                f'<td>{"Revoked" if client["revoked_at"] else "Active"}</td><td>{action}</td></tr>'
                + (f'<tr class="client-usage-row"><td colspan="4">{compact_usage(client_usage[client["id"]])}</td></tr>'
                   if client["id"] in client_usage else ''))
    client_rows = "".join(client_row(client) for client in clients)
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
            + (window_form(all_usage, "/sharing") if all_usage else '')
            + '<p class="note">The selected window applies to every key below and the all-key totals. Token counts are reported by the serving engine; unreported tokens remain unmeasured.</p>'
            + '<div class="scroll"><table><tr><th>Client</th><th>Created UTC</th><th>Status</th><th>Action</th></tr>'
            + '<tr class="client-main-row"><td>Shared legacy key</td><td>Before dedicated keys</td><td>Active</td><td></td></tr>'
            + (f'<tr class="client-usage-row"><td colspan="4">{compact_usage(client_usage["legacy"])}</td></tr>' if "legacy" in client_usage else '')
            + client_rows + '</table></div>'
            + (usage_panel(all_usage, "all API keys", embedded=True, show_window=False) if all_usage else '') + '</div>')
    style = """<style>.settings-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1rem}.setting{padding:.8rem;border:1px solid #344b50;border-radius:.6rem;background:#122126}.setting label{display:block;margin-bottom:.3rem}.setting input{width:8rem}.setting p{font-size:.9rem;margin:.5rem 0 0}.secondary{background:#22373a;color:#e6d3b3;border-color:#557070}.secondary:hover{background:#30484b}.new-key{display:block;overflow-wrap:anywhere;padding:1rem;background:#0b181c;border:1px solid #4d696a;border-radius:.5rem}.all-key-usage{margin-top:1.5rem;padding-top:1.2rem;border-top:1px solid #365056}@media(max-width:760px){.settings-grid{grid-template-columns:1fr}}</style>""" + '<style>' + USAGE_STYLE + '</style>'
    return shell("Client sharing and rate limits", style + body, refresh=parallel_status["busy"], wide=True)
