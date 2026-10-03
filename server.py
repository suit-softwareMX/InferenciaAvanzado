"""Small, durable inference gateway. One process and one GPU worker per database."""

import hmac
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, ValidationError


MAX_BODY = 50_000
MAX_QUEUED = 100
MODEL_TIMEOUT = 60
POLL_SECONDS = 0.5
TASKS = {"translate", "review", "detect_language", "proofread"}
logger = logging.getLogger("uvicorn.error")
TRANSLATION_SCHEMA = {
    "type": "object", "properties": {"fields": {"type": "object", "additionalProperties": {"type": "string"}}},
    "required": ["fields"], "additionalProperties": False,
}
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {"issues": {"type": "array", "items": {"type": "object", "properties": {
        "field": {"type": "string"}, "severity": {"type": "string", "enum": ["low", "medium", "high"]},
        "category": {"type": "string", "enum": ["meaning", "tone", "terminology", "data"]},
        "message": {"type": "string", "maxLength": 500},
    }, "required": ["field", "severity", "category", "message"], "additionalProperties": False}}},
    "required": ["issues"], "additionalProperties": False,
}
DETECTION_SCHEMA = {"type": "object", "properties": {"locale": {"type": "string", "enum": ["es", "en", "unknown"]}}, "required": ["locale"], "additionalProperties": False}


def now():
    return datetime.now(timezone.utc).isoformat()


class JobRequest(BaseModel):
    task: str
    input: dict


def validate_input(task, data):
    if task not in TASKS:
        raise ValueError("Tarea desconocida")
    fields = data.get("fields")
    if not isinstance(fields, dict) or not 1 <= len(fields) <= 60:
        raise ValueError("Se requieren entre 1 y 60 campos")
    if any(not isinstance(key, str) or not key or len(key) > 200 or not isinstance(value, str)
           or not value.strip() or len(value) > 4000 for key, value in fields.items()):
        raise ValueError("Los campos deben ser textos no vacíos de hasta 4000 caracteres")
    if sum(len(value) for value in fields.values()) > 4000:
        raise ValueError("El texto total supera 4000 caracteres")
    source, target = data.get("source_locale"), data.get("target_locale")
    if task in {"translate", "review"}:
        if (source, target) not in {("es", "en"), ("en", "es")}:
            raise ValueError("Solo se admite español e inglés en direcciones opuestas")
    elif task == "proofread":
        if source not in {"es", "en"} or target is not None:
            raise ValueError("La corrección requiere un idioma español o inglés")
    elif source is not None or target is not None:
        raise ValueError("La detección no requiere idioma")
    if task == "review":
        translated = data.get("translated_fields")
        if not isinstance(translated, dict) or translated.keys() != fields.keys() or any(
            not isinstance(value, str) or not value.strip() or len(value) > 4000 for value in translated.values()
        ) or sum(len(value) for value in translated.values()) > 4000:
            raise ValueError("La revisión requiere los mismos campos traducidos")
    elif "translated_fields" in data:
        raise ValueError("translated_fields no corresponde a traducción")
    if set(data) - {"fields", "source_locale", "target_locale", "translated_fields"}:
        raise ValueError("La solicitud contiene parámetros no admitidos")


def validate_result(task, result, field_ids):
    if not isinstance(result, dict):
        raise ValueError("invalid_model_output")
    if task in {"translate", "proofread"}:
        fields = result.get("fields")
        if set(result) != {"fields"} or not isinstance(fields, dict) or fields.keys() != field_ids or any(
            not isinstance(value, str) or not value.strip() or len(value) > 4000 for value in fields.values()
        ):
            raise ValueError("invalid_model_output")
    elif task == "detect_language":
        if set(result) != {"locale"} or not isinstance(result["locale"], str) or result["locale"] not in {"es", "en", "unknown"}:
            raise ValueError("invalid_model_output")
    else:
        issues = result.get("issues")
        if set(result) != {"issues"} or not isinstance(issues, list) or len(issues) > 30:
            raise ValueError("invalid_model_output")
        for issue in issues:
            if not isinstance(issue, dict) or set(issue) != {"field", "severity", "category", "message"} or \
               not isinstance(issue["field"], str) or issue["field"] not in field_ids or \
               not isinstance(issue["severity"], str) or issue["severity"] not in {"low", "medium", "high"} or \
               not isinstance(issue["category"], str) or issue["category"] not in {"meaning", "tone", "terminology", "data"} or \
               not isinstance(issue["message"], str) or not issue["message"].strip() or len(issue["message"]) > 500:
                raise ValueError("invalid_model_output")
    return result


class Store:
    def __init__(self, filename):
        self.filename = Path(filename)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.filename, timeout=10)
        try:
            connection.row_factory = sqlite3.Row
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self):
        self.filename.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, project TEXT NOT NULL, task TEXT NOT NULL,
                input_json TEXT NOT NULL, status TEXT NOT NULL,
                result_json TEXT, error TEXT, created_at TEXT NOT NULL,
                started_at TEXT, finished_at TEXT, model TEXT)""")
            if "model" not in {row[1] for row in db.execute("PRAGMA table_info(jobs)")}:
                db.execute("ALTER TABLE jobs ADD COLUMN model TEXT")
            db.execute("UPDATE jobs SET status='queued', started_at=NULL WHERE status='running'")

    def enqueue(self, project, task, data):
        job_id = str(uuid4())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
            if count >= MAX_QUEUED:
                raise OverflowError("queue_full")
            db.execute("INSERT INTO jobs (id,project,task,input_json,status,created_at) VALUES (?,?,?,?,?,?)",
                       (job_id, project, task, json.dumps(data, ensure_ascii=False), "queued", now()))
        return job_id

    def get(self, job_id, project):
        with self.connect() as db:
            row = db.execute("SELECT id,task,status,result_json,error,created_at,started_at,finished_at,model "
                             "FROM jobs WHERE id=? AND project=?", (job_id, project)).fetchone()
        if row is None:
            return None
        return {"id": row["id"], "task": row["task"], "status": row["status"],
                "result": json.loads(row["result_json"]) if row["result_json"] else None,
                "error": row["error"], "created_at": row["created_at"],
                "started_at": row["started_at"], "finished_at": row["finished_at"], "model": row["model"]}

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id,project,task,input_json FROM jobs WHERE status='queued' "
                             "ORDER BY created_at,id LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE jobs SET status='running',started_at=? WHERE id=?", (now(), row["id"]))
        return dict(row) if row else None

    def finish(self, job_id, result=None, error=None, model=None):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status=?,result_json=?,error=?,finished_at=?,model=? WHERE id=?",
                       ("failed" if error else "succeeded", json.dumps(result, ensure_ascii=False) if result else None,
                        error, now(), model, job_id))

    def set_model(self, job_id, model):
        with self.connect() as db:
            db.execute("UPDATE jobs SET model=? WHERE id=?", (model, job_id))


class Ollama:
    def __init__(self, url, models, preferred=None):
        self.url = url.rstrip("/")
        self.models = models
        self.preferred = preferred or {}

    def installed(self):
        with urllib.request.urlopen(f"{self.url}/api/tags", timeout=0.5) as response:
            return {model["name"] for model in json.load(response)["models"]}

    def model_status(self):
        names = self.installed()
        return {task: {"active": self.preferred.get(task) if self.preferred.get(task) in names else fallback if fallback in names else None,
                       "preferred_available": bool(self.preferred.get(task) and self.preferred[task] in names),
                       "using_fallback": fallback in names and self.preferred.get(task) not in names}
                for task, fallback in self.models.items()}

    def available(self):
        try:
            with urllib.request.urlopen(f"{self.url}/api/tags", timeout=0.5):
                return True
        except (OSError, TimeoutError):
            return False

    def models_ready(self):
        try:
            return all(item["active"] for item in self.model_status().values())
        except (OSError, TimeoutError, KeyError, ValueError):
            return False

    def run(self, task, data, project, model=None):
        if task == "translate":
            instruction = f"Translate each field from {data['source_locale']} to {data['target_locale']} naturally. Preserve field IDs, meaning, names, numbers, contact details and claims. Return only JSON."
        elif task == "proofread":
            instruction = f"Correct spelling, punctuation and clear grammar in {data['source_locale']}. Preserve field IDs, meaning, names, numbers, links and tone. Make no stylistic rewrites. Return the corrected complete fields as JSON."
        elif task == "detect_language":
            instruction = "Detect whether the text is Spanish or English. For short, ambiguous, mixed or other-language text answer unknown. Return only JSON."
        else:
            instruction = "Compare translated fields with originals. Report only verifiable mistakes in meaning, data, grammar or tone; no invented issues. Return only JSON."
        if project == "auditaxes":
            instruction += " Context: AUDITAXES provides audit, tax, financial advisory and international trade services. Use a sober, precise institutional tone."
        schema = {"translate": TRANSLATION_SCHEMA, "review": REVIEW_SCHEMA,
                  "detect_language": DETECTION_SCHEMA, "proofread": TRANSLATION_SCHEMA}[task]
        payload = {"model": model or self.models[task], "stream": False, "format": schema,
                   "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 2048},
                   "messages": [{"role": "system", "content": instruction},
                                {"role": "user", "content": json.dumps(data, ensure_ascii=False)}]}
        request = urllib.request.Request(f"{self.url}/api/chat", json.dumps(payload).encode(),
                                         {"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=MODEL_TIMEOUT) as response:
                body = json.load(response)
        except (OSError, TimeoutError, urllib.error.HTTPError) as exc:
            raise RuntimeError("model_unavailable") from exc
        try:
            return json.loads(body["message"]["content"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid_model_output") from exc


def process_one(store, engine):
    job = store.claim()
    if not job:
        return False
    started = time.monotonic()
    try:
        data = json.loads(job["input_json"])
        status = engine.model_status() if hasattr(engine, "model_status") else {}
        model = status.get(job["task"], {}).get("active")
        if model:
            store.set_model(job["id"], model)
        result = None
        last_error = None
        for _attempt in range(2):
            try:
                candidate = engine.run(job["task"], data, job["project"], model) if model else engine.run(job["task"], data, job["project"])
                result = validate_result(job["task"], candidate, data["fields"].keys())
                break
            except ValueError as exc:
                last_error = exc
                if str(exc) != "invalid_model_output":
                    raise
        if result is None:
            raise last_error or ValueError("invalid_model_output")
        store.finish(job["id"], result=result, model=model)
        status = "succeeded"
    except Exception as exc:
        error = str(exc) if str(exc) in {"model_unavailable", "invalid_model_output"} else "inference_failed"
        store.finish(job["id"], error=error, model=locals().get("model"))
        status = "failed"
    logger.info("job=%s project=%s task=%s status=%s duration_ms=%d", job["id"], job["project"],
                job["task"], status, int((time.monotonic() - started) * 1000))
    return True


def create_app(store=None, engine=None, api_keys=None, start_worker=True):
    store = store or Store(os.getenv("INFERENCE_DB", "./data/jobs.sqlite3"))
    tasks = {"translate": "TRANSLATE", "review": "REVIEW", "detect_language": "DETECT", "proofread": "PROOFREAD"}
    fallback = os.getenv("FALLBACK_MODEL", "qwen3:4b-instruct-2507-q4_K_M")
    engine = engine or Ollama(os.getenv("OLLAMA_URL", "http://127.0.0.1:11434"),
        {task: os.getenv(f"{prefix}_FALLBACK_MODEL", os.getenv(f"{prefix}_MODEL", fallback)) for task, prefix in tasks.items()},
        {task: os.getenv(f"{prefix}_PREFERRED_MODEL", os.getenv("PREFERRED_MODEL")) for task, prefix in tasks.items()})
    if api_keys is None:
        api_keys = json.loads(os.getenv("INFERENCE_API_KEYS", "{}"))
    if not isinstance(api_keys, dict) or not api_keys or any(
        not isinstance(project, str) or not project or not isinstance(key, str) or len(key) < 24
        for project, key in api_keys.items()
    ) or len(set(api_keys.values())) != len(api_keys):
        raise RuntimeError("Configure INFERENCE_API_KEYS con claves únicas de al menos 24 caracteres")

    stop = threading.Event()

    def work():
        # ponytail: one worker per SQLite file; use a distributed queue only if multiple GPU workers become necessary.
        while not stop.is_set():
            try:
                if not getattr(engine, "models_ready", engine.available)():
                    stop.wait(2)
                    continue
                if not process_one(store, engine):
                    stop.wait(POLL_SECONDS)
            except sqlite3.Error:
                logger.exception("inference worker database error")
                stop.wait(2)

    @asynccontextmanager
    async def lifespan(_app):
        store.initialize()
        worker = threading.Thread(target=work, daemon=True, name="inference-worker") if start_worker else None
        if worker:
            worker.start()
        try:
            yield
        finally:
            stop.set()
            if worker:
                worker.join(timeout=MODEL_TIMEOUT + 2)

    app = FastAPI(title="Inference Gateway", version="1.0", lifespan=lifespan)

    def project(authorization: str | None = Header(default=None)):
        token = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
        for name, key in api_keys.items():
            if hmac.compare_digest(token, key):
                return name
        raise HTTPException(401, "Clave de API inválida")

    @app.post("/v1/jobs", status_code=202)
    async def submit(request: Request, owner: str = Depends(project)):
        body = await request.body()
        if len(body) > MAX_BODY:
            raise HTTPException(413, "Solicitud demasiado grande")
        try:
            job = JobRequest.model_validate_json(body)
            validate_input(job.task, job.input)
        except ValidationError as exc:
            raise HTTPException(422, "Formato de trabajo inválido") from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc
        try:
            job_id = store.enqueue(owner, job.task, job.input)
        except OverflowError as exc:
            raise HTTPException(429, "Cola llena") from exc
        return {"id": job_id, "status": "queued"}

    @app.get("/v1/jobs/{job_id}")
    def get_job(job_id: str, owner: str = Depends(project)):
        job = store.get(job_id, owner)
        if job is None:
            raise HTTPException(404, "Trabajo no encontrado")
        return job

    @app.get("/healthz")
    def health():
        try:
            with store.connect() as db:
                db.execute("SELECT 1")
            db_ok = True
        except sqlite3.Error:
            db_ok = False
        online = engine.available()
        try:
            models = engine.model_status() if online and hasattr(engine, "model_status") else {}
        except (OSError, TimeoutError, KeyError, ValueError):
            models = {}
        return {"api": "ok" if db_ok else "unavailable", "database": db_ok,
                "ollama": online, "models_ready": getattr(engine, "models_ready", engine.available)(), "models": models}

    return app


app = create_app() if os.getenv("INFERENCE_API_KEYS") else None
