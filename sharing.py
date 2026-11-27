"""Persist portable API sharing limits and distinct client credentials."""
import datetime as dt
import hashlib
import hmac
import secrets
import sqlite3
import uuid


FIELDS = ("requests_per_minute", "burst", "per_client_active", "per_client_queued",
          "global_active", "global_queued", "queue_wait_seconds", "max_output_tokens",
          "max_request_kib")
LIMITS = {"requests_per_minute": (1, 600), "burst": (1, 100),
          "per_client_active": (1, 8), "per_client_queued": (0, 20),
          "global_active": (1, 16), "global_queued": (0, 100),
          "queue_wait_seconds": (1, 120), "max_output_tokens": (256, 32768),
          "max_request_kib": (64, 16384)}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def defaults(hardware, server_slots=1):
    """Conservative starting policy derived from available VRAM, CPU and loaded server."""
    vram = max(0, int(hardware.get("vram_mib") or 0))
    cpu = max(1, int(hardware.get("cpu_threads") or 1))
    capacity = max(1, min(8, vram // 12288, cpu // 4))
    slots = min(capacity, max(1, int(server_slots)))
    return {"requests_per_minute": 30 if cpu < 8 else 60, "burst": 4,
            "per_client_active": 1, "per_client_queued": 2,
            "global_active": slots, "global_queued": max(4, slots * 4),
            "queue_wait_seconds": 30, "max_output_tokens": 4096 if vram < 16384 else 8192,
            "max_request_kib": 2048}


class SharingManager:
    def __init__(self, db_path, read_only=False):
        self.db_path = db_path
        self.read_only = read_only
        if read_only:
            return
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS sharing_settings (
                id INTEGER PRIMARY KEY CHECK(id=1), requests_per_minute INTEGER NOT NULL,
                burst INTEGER NOT NULL, per_client_active INTEGER NOT NULL,
                per_client_queued INTEGER NOT NULL, global_active INTEGER NOT NULL,
                global_queued INTEGER NOT NULL, queue_wait_seconds INTEGER NOT NULL,
                max_output_tokens INTEGER NOT NULL, max_request_kib INTEGER NOT NULL,
                hardware_fingerprint TEXT, customized INTEGER NOT NULL DEFAULT 0)""")
            db.execute("""CREATE TABLE IF NOT EXISTS sharing_clients (
                id TEXT PRIMARY KEY, label TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL, revoked_at TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS api_usage (
                id INTEGER PRIMARY KEY, client_id TEXT NOT NULL, started_at TEXT NOT NULL,
                status INTEGER NOT NULL, duration_seconds REAL NOT NULL,
                input_tokens INTEGER, output_tokens INTEGER)""")
            db.execute("CREATE INDEX IF NOT EXISTS api_usage_client_time ON api_usage(client_id,started_at)")
            db.execute("CREATE INDEX IF NOT EXISTS api_usage_time ON api_usage(started_at)")

    def connect(self):
        db = (sqlite3.connect("file:" + self.db_path + "?mode=ro", uri=True, timeout=20)
              if self.read_only else sqlite3.connect(self.db_path, timeout=20))
        db.row_factory = sqlite3.Row
        return db

    def settings(self, hardware=None, server_slots=1):
        with self.connect() as db:
            row = db.execute("SELECT * FROM sharing_settings WHERE id=1").fetchone()
            if not row or (hardware is not None and not row["customized"] and
                           (row["hardware_fingerprint"] != hardware.get("fingerprint") or
                            row["global_active"] != defaults(hardware, server_slots)["global_active"])):
                values = defaults(hardware or {}, server_slots)
                if self.read_only:
                    return {**values, "hardware_fingerprint": None, "customized": 0}
                db.execute("""INSERT INTO sharing_settings
                    (id,requests_per_minute,burst,per_client_active,per_client_queued,
                     global_active,global_queued,queue_wait_seconds,max_output_tokens,
                     max_request_kib,hardware_fingerprint,customized)
                    VALUES(1,?,?,?,?,?,?,?,?,?,?,0)
                    ON CONFLICT(id) DO UPDATE SET
                    requests_per_minute=excluded.requests_per_minute, burst=excluded.burst,
                    per_client_active=excluded.per_client_active,
                    per_client_queued=excluded.per_client_queued,
                    global_active=excluded.global_active, global_queued=excluded.global_queued,
                    queue_wait_seconds=excluded.queue_wait_seconds,
                    max_output_tokens=excluded.max_output_tokens,
                    max_request_kib=excluded.max_request_kib,
                    hardware_fingerprint=excluded.hardware_fingerprint, customized=0""",
                           (*[values[key] for key in FIELDS], (hardware or {}).get("fingerprint")))
                row = db.execute("SELECT * FROM sharing_settings WHERE id=1").fetchone()
            return dict(row)

    def save_settings(self, values):
        if set(values) != set(FIELDS):
            raise ValueError("Provide every sharing setting")
        parsed = {}
        for key in FIELDS:
            value = values[key]
            if not str(value).isdecimal():
                raise ValueError(f"{key} must be a whole number")
            number = int(value)
            low, high = LIMITS[key]
            if not low <= number <= high:
                raise ValueError(f"{key} must be {low}–{high}")
            parsed[key] = number
        if parsed["per_client_active"] > parsed["global_active"]:
            raise ValueError("Per-client active requests cannot exceed the global limit")
        with self.connect() as db:
            db.execute("UPDATE sharing_settings SET " + ", ".join(key + "=?" for key in FIELDS) +
                       ", customized=1 WHERE id=1", tuple(parsed[key] for key in FIELDS))
        return parsed

    def reset(self, hardware, server_slots):
        values = defaults(hardware, server_slots)
        with self.connect() as db:
            db.execute("UPDATE sharing_settings SET " + ", ".join(key + "=?" for key in FIELDS) +
                       ", hardware_fingerprint=?, customized=0 WHERE id=1",
                       (*[values[key] for key in FIELDS], hardware.get("fingerprint")))
        return values

    def restore_settings(self, previous):
        with self.connect() as db:
            db.execute("UPDATE sharing_settings SET " + ", ".join(key + "=?" for key in FIELDS) +
                       ", hardware_fingerprint=?, customized=? WHERE id=1",
                       (*[previous[key] for key in FIELDS], previous["hardware_fingerprint"], previous["customized"]))

    def clients(self):
        with self.connect() as db:
            rows = db.execute("SELECT id,label,created_at,revoked_at FROM sharing_clients ORDER BY created_at DESC, rowid DESC").fetchall()
        return [dict(row) for row in rows]

    def create_client(self, label):
        label = label.strip()
        if not 1 <= len(label) <= 64 or any(ord(char) < 32 for char in label):
            raise ValueError("Client name must be 1–64 printable characters")
        token = "lai_" + secrets.token_urlsafe(32)
        client_id = str(uuid.uuid4())
        with self.connect() as db:
            db.execute("INSERT INTO sharing_clients(id,label,token_hash,created_at) VALUES(?,?,?,?)",
                       (client_id, label, hashlib.sha256(token.encode()).hexdigest(), now()))
        return {"id": client_id, "label": label, "token": token}

    def revoke(self, client_id):
        with self.connect() as db:
            result = db.execute("UPDATE sharing_clients SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                                (now(), client_id))
            return result.rowcount == 1

    def identify(self, token, legacy_key):
        if not token or len(token) > 256:
            return None
        if hmac.compare_digest(token, legacy_key):
            return "legacy"
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.connect() as db:
            row = db.execute("SELECT id FROM sharing_clients WHERE token_hash=? AND revoked_at IS NULL", (digest,)).fetchone()
        return row["id"] if row else None

    def record_usage(self, client_id, started_at, status, duration_seconds, input_tokens=None, output_tokens=None):
        """Store request metadata and engine-reported tokens, never keys or prompts."""
        if self.read_only:
            raise RuntimeError("Usage recording requires writable storage")
        if not client_id or any(value is not None and (not isinstance(value, int) or value < 0)
                                for value in (input_tokens, output_tokens)):
            raise ValueError("Invalid usage record")
        with self.connect() as db:
            db.execute("INSERT INTO api_usage(client_id,started_at,status,duration_seconds,input_tokens,output_tokens) "
                       "VALUES(?,?,?,?,?,?)",
                       (client_id, started_at, int(status), max(0, float(duration_seconds)), input_tokens, output_tokens))

    def usage_summary(self, client_id, amount=24, unit="hours", buckets=30):
        """Aggregate one key, or all recorded keys, over a chosen UTC window."""
        seconds_per = {"minutes": 60, "hours": 3600, "days": 86400}
        if unit not in seconds_per or not isinstance(amount, int) or not 1 <= amount or amount * seconds_per[unit] > 90 * 86400:
            raise ValueError("Choose a window from one minute through 90 days")
        if not isinstance(buckets, int) or not 1 <= buckets <= 120:
            raise ValueError("Invalid graph resolution")
        end = dt.datetime.now(dt.timezone.utc)
        start = end - dt.timedelta(seconds=amount * seconds_per[unit])
        start_text = start.isoformat(timespec="seconds")
        end_text = end.isoformat(timespec="seconds")
        bucket_seconds = amount * seconds_per[unit] / buckets
        client_filter = "" if client_id == "all" else "client_id=? AND "
        parameters = ((start_text, bucket_seconds, start_text, end_text) if client_id == "all" else
                      (start_text, bucket_seconds, client_id, start_text, end_text))
        with self.connect() as db:
            rows = db.execute("""SELECT CAST((julianday(started_at)-julianday(?))*86400/? AS INTEGER) AS bucket,
                COUNT(*) AS requests, COUNT(input_tokens) AS input_measured,
                COUNT(output_tokens) AS output_measured,
                COALESCE(SUM(input_tokens),0) AS input_tokens,
                COALESCE(SUM(output_tokens),0) AS output_tokens,
                COALESCE(SUM(CASE WHEN output_tokens IS NOT NULL THEN duration_seconds ELSE 0 END),0) AS measured_seconds
                FROM api_usage WHERE """ + client_filter + "started_at>=? AND started_at<=? GROUP BY bucket",
                parameters).fetchall()
        points = [{"requests": 0, "input_measured": 0, "output_measured": 0,
                   "input_tokens": 0, "output_tokens": 0, "measured_seconds": 0.0, "tokens_per_second": None}
                  for _ in range(buckets)]
        for row in rows:
            position = max(0, min(buckets - 1, row["bucket"]))
            point = points[position]
            for key in ("requests", "input_measured", "output_measured", "input_tokens", "output_tokens", "measured_seconds"):
                point[key] += row[key]
        for point in points:
            point["tokens_per_second"] = (round(point["output_tokens"] / point["measured_seconds"], 2)
                                           if point["output_measured"] and point["measured_seconds"] > 0 else None)
        totals = {key: sum(point[key] for point in points)
                  for key in ("requests", "input_measured", "output_measured", "input_tokens", "output_tokens", "measured_seconds")}
        totals["tokens_per_second"] = (round(totals["output_tokens"] / totals["measured_seconds"], 2)
                                        if totals["output_measured"] and totals["measured_seconds"] > 0 else None)
        return {"client_id": client_id, "amount": amount, "unit": unit, "start": start_text,
                "end": end_text, "totals": totals, "points": points}
