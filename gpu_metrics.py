"""Sample NVIDIA GPU counters from an active GPU container and draw a live SVG."""
import collections
import datetime as dt
import html
import threading
import time

from benchmarks import MODELS, docker_api, docker_exec


QUERY = ["nvidia-smi", "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
         "--format=csv,noheader,nounits"]


def parse_samples(output, timestamp):
    samples = []
    for line in output.splitlines():
        parts = [part.strip() for part in line.split(",", 6)]
        if len(parts) != 7:
            continue
        try:
            index, name = int(parts[0]), parts[1]
            util, used, total = [float(part) for part in parts[2:5]]
            temp = float(parts[5]) if parts[5] not in ("N/A", "[N/A]") else None
            power = float(parts[6]) if parts[6] not in ("N/A", "[N/A]") else None
            if total <= 0 or not 0 <= used <= total or not 0 <= util <= 100:
                continue
            samples.append({"index": index, "name": name, "time": timestamp, "util": util,
                            "used": used, "total": total, "temp": temp, "power": power})
        except ValueError:
            continue
    return samples


class GPUMonitor:
    def __init__(self):
        self.lock = threading.Lock()
        self.history = collections.defaultdict(lambda: collections.deque(maxlen=120))
        self.last_sample = float("-inf")
        self.last_success = 0
        self.error = None

    def sample(self):
        with self.lock:
            if time.monotonic() - self.last_sample < 5:
                return
            self.last_sample = time.monotonic()
            try:
                containers = docker_api("GET", "/containers/json?all=0")
                names = {name.lstrip("/") for item in containers for name in item.get("Names", [])}
                candidates = [container for _, container in MODELS.values() if container in names]
                candidates.extend(sorted(name for name in names if name.startswith("local-ai-hf-") and name not in candidates))
                output = ""
                for container in candidates:
                    try:
                        output = docker_exec(container, QUERY)
                        if output:
                            break
                    except (RuntimeError, KeyError):
                        continue
                samples = parse_samples(output, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
                for item in samples:
                    self.history[item["index"]].append(item)
                if samples:
                    self.last_success = time.monotonic()
                self.error = None if samples else "GPU counters unavailable while no GPU workload is running"
            except (RuntimeError, KeyError) as exc:
                self.error = type(exc).__name__ + ": " + str(exc)[:120]

    def render(self):
        self.sample()
        with self.lock:
            histories = {key: list(values) for key, values in self.history.items()}
            if time.monotonic() - self.last_success > 20:
                histories = {}
            error = self.error
        cards = []
        for index, records in sorted(histories.items()):
            if not records:
                continue
            last = records[-1]
            def points(value):
                return " ".join(f"{55 + i * 720 / max(1, len(records)-1):.1f},{210 - value(item) * 1.7:.1f}"
                                for i, item in enumerate(records))
            util = points(lambda item: item["util"])
            memory = points(lambda item: 100 * item["used"] / item["total"])
            label = html.escape(last["name"])
            y_grid = ''.join(f'<path d="M55 {210 - level * 1.7:.1f}H775" class="plot-grid"/>'
                             f'<text x="48" y="{214 - level * 1.7:.1f}" text-anchor="end">{level}%</text>'
                             for level in (0, 25, 50, 75, 100))
            x_grid = ''.join(f'<path d="M{x} 40V210" class="plot-grid"/>' for x in (55, 235, 415, 595, 775))
            x_labels = ''.join(f'<text x="{x}" y="232" text-anchor="middle">'
                               f'{html.escape(dt.datetime.fromisoformat(records[position]["time"]).strftime("%H:%M:%S"))}</text>'
                               for x, position in ((55, 0), (415, (len(records) - 1) // 2), (775, len(records) - 1)))
            cards.append(f'''<section><h2>GPU {index}: {label}</h2>
                <div class="stats"><span>GPU use <strong>{last["util"]:.0f}%</strong></span>
                <span>VRAM <strong>{last["used"]:.0f} / {last["total"]:.0f} MiB</strong></span>
                <span>Temperature <strong>{f'{last["temp"]:.0f} °C' if last["temp"] is not None else 'NA'}</strong></span>
                <span>Power <strong>{f'{last["power"]:.0f} W' if last["power"] is not None else 'NA'}</strong></span></div>
                <svg viewBox="0 0 800 245" role="img" aria-label="GPU utilization and VRAM use over the last {len(records) * 5} seconds, UTC time on the horizontal axis and percent on the vertical axis">
                {y_grid}{x_grid}{x_labels}
                <polyline points="{util}" fill="none" stroke="#f2bd79" stroke-width="3"/>
                <polyline points="{memory}" fill="none" stroke="#71cdbf" stroke-width="3"/></svg>
                <p class="legend"><span class="gpu-dot"></span> GPU use <span class="memory-dot"></span> VRAM use
                <small>Latest sample: {html.escape(last["time"])} UTC</small></p></section>''')
        empty = '<p class="note">' + html.escape(error or "Collecting GPU samples…") + '</p>' if not cards else ''
        return f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta http-equiv="refresh" content="5">
            <meta name="viewport" content="width=device-width,initial-scale=1"><title>Live GPU status</title>
            <style>html{{color-scheme:dark}}body{{margin:0;background:#17262a;color:#eaf2ee;font:14px/1.4 system-ui,sans-serif}}
            section{{margin:0 0 1rem}}h2{{font-size:1rem;margin:.25rem 0 .65rem}}.stats{{display:flex;gap:.6rem;flex-wrap:wrap}}
            .stats span{{padding:.4rem .65rem;border:1px solid #385156;border-radius:.45rem;background:#102125}}
            strong{{color:#fff2d9}}svg{{display:block;width:100%;height:auto;max-height:245px;margin:.45rem 0}}.plot-grid{{stroke:#4c6467;stroke-width:1;stroke-dasharray:3 5;fill:none}}
            text{{fill:#b7c9c7;font:12px system-ui}}.legend{{display:flex;align-items:center;gap:.45rem;margin:0}}
            .legend small{{margin-left:auto;color:#a9bdba}}.gpu-dot,.memory-dot{{display:inline-block;width:.65rem;height:.2rem;background:#f2bd79}}
            .memory-dot{{background:#71cdbf;margin-left:1rem}}.note{{color:#b7c9c7}}</style>
            <body>{''.join(cards)}{empty}</body></html>'''
