"""Durable, owner-scoped jobs for CPU and GPU media tasks."""
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import uuid


CPU_KINDS = frozenset({"pdf", "ocr", "transcription", "speech"})
GPU_KINDS = frozenset({"image", "image_edit"})
KINDS = CPU_KINDS | GPU_KINDS


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class JobStore:
    def __init__(self, db_path, artifact_dir):
        self.db_path = str(db_path)
        self.artifact_dir = Path(artifact_dir)
        self.artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.artifact_dir.chmod(0o700)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                started_at TEXT, completed_at TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                stage TEXT NOT NULL, request_json TEXT NOT NULL, input_name TEXT,
                result_name TEXT, result_type TEXT, result_json TEXT, error TEXT,
                backend_ref TEXT)""")
            db.execute("CREATE INDEX IF NOT EXISTS jobs_pending ON jobs(status,kind,created_at)")
            # A single worker process owns this database. On restart, resume unfinished work.
            db.execute("UPDATE jobs SET status='queued', stage='resuming after worker restart', "
                       "updated_at=? WHERE status='running'", (utc_now(),))

    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def _save_file(self, name, payload):
        target = self.artifact_dir / name
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
        except BaseException:
            target.unlink(missing_ok=True)
            raise

    def submit(self, owner, kind, request, payload=b""):
        if kind not in KINDS or not owner or not isinstance(request, dict):
            raise ValueError("Invalid job")
        if not isinstance(payload, bytes):
            raise ValueError("Job input must be bytes")
        ident = str(uuid.uuid4())
        input_name = ident + ".input" if payload else None
        if input_name:
            self._save_file(input_name, payload)
        now = utc_now()
        try:
            with self.connect() as db:
                db.execute("""INSERT INTO jobs
                    (id,owner,kind,status,created_at,updated_at,stage,request_json,input_name)
                    VALUES(?,?,?,'queued',?,?,'waiting',?,?)""",
                    (ident, owner, kind, now, now, json.dumps(request), input_name))
        except BaseException:
            if input_name:
                (self.artifact_dir / input_name).unlink(missing_ok=True)
            raise
        return self.get(ident, owner)

    def get(self, ident, owner=None):
        try:
            valid = isinstance(ident, str) and uuid.UUID(ident).version == 4
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Invalid job ID")
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (ident,)).fetchone()
        if not row or (owner is not None and row["owner"] != owner):
            return None
        value = dict(row)
        value["request"] = json.loads(value.pop("request_json"))
        value["result"] = json.loads(value.pop("result_json")) if value.get("result_json") else None
        return value

    def claim(self, kinds):
        if not kinds or not set(kinds) <= KINDS:
            raise ValueError("Invalid job kinds")
        placeholders = ",".join("?" for _ in kinds)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(f"SELECT id FROM jobs WHERE status='queued' AND kind IN ({placeholders}) "
                             "ORDER BY rowid LIMIT 1", tuple(kinds)).fetchone()
            if row:
                db.execute("UPDATE jobs SET status='running', stage='starting', started_at=COALESCE(started_at,?), "
                           "updated_at=?, attempts=attempts+1 WHERE id=?", (utc_now(), utc_now(), row["id"]))
        return self.get(row["id"]) if row else None

    def update(self, ident, **values):
        allowed = {"stage", "backend_ref", "status", "result_name", "result_type", "result_json", "error", "completed_at"}
        if not values or set(values) - allowed:
            raise ValueError("Invalid job update")
        values["updated_at"] = utc_now()
        columns = ",".join(f"{name}=?" for name in values)
        with self.connect() as db:
            db.execute(f"UPDATE jobs SET {columns} WHERE id=?", (*values.values(), ident))

    def complete(self, ident, result=None, artifact=None, content_type=None):
        name = ident + ".result" if artifact is not None else None
        if name:
            (self.artifact_dir / name).unlink(missing_ok=True)
            self._save_file(name, artifact)
        self.update(ident, status="completed", stage="ready", completed_at=utc_now(),
                    result_name=name, result_type=content_type,
                    result_json=json.dumps(result) if result is not None else None)
        input_name = ident + ".input"
        (self.artifact_dir / input_name).unlink(missing_ok=True)

    def fail(self, ident, reason):
        self.update(ident, status="failed", stage="failed", completed_at=utc_now(), error=str(reason)[:500])
        (self.artifact_dir / (ident + ".input")).unlink(missing_ok=True)

    def artifact(self, job):
        name = job.get("result_name")
        if name != job["id"] + ".result":
            return None
        return (self.artifact_dir / name).read_bytes()

    def input(self, job):
        if job.get("input_name") != job["id"] + ".input":
            return b""
        return (self.artifact_dir / job["input_name"]).read_bytes()

    def list(self, owner, limit=50):
        with self.connect() as db:
            rows = db.execute("SELECT id FROM jobs WHERE owner=? ORDER BY rowid DESC LIMIT ?",
                              (owner, min(max(int(limit), 1), 100))).fetchall()
        return [self.get(row["id"], owner) for row in rows]
