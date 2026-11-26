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
