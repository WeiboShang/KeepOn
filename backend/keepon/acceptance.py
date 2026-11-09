from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import ntpath
import os
import stat
from pathlib import Path, PureWindowsPath
from typing import Any

from aiosqlite import Connection
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .database import Database
from .util import dumps, loads, now, uid

logger = logging.getLogger(__name__)
MAX_JSON_BYTES = 2 * 1024 * 1024
SCHEMA_KEYS = {
    "type", "required", "properties", "additionalProperties", "enum", "items",
    "minItems", "maxItems", "minLength", "maxLength", "minimum", "maximum",
    "exclusiveMinimum", "exclusiveMaximum", "minProperties", "maxProperties",
}


def _schema_subset(schema: Any, depth: int = 0) -> None:
    if depth > 16 or not isinstance(schema, dict) or set(schema) - SCHEMA_KEYS:
        raise ValueError("Schema contains unsupported keywords or nesting")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise ValueError("Schema properties must be an object")
    for child in properties.values():
        _schema_subset(child, depth + 1)
    for key in ("items", "additionalProperties"):
        if key in schema and not (key == "additionalProperties" and isinstance(schema[key], bool)):
            _schema_subset(schema[key], depth + 1)


def normalize_checks(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 20:
        raise ValueError("Provide 1–20 acceptance checks")
    checks = []
    ids = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Each acceptance check must be an object")
        cid = item.get("id") or uid()
        label, kind, config = item.get("label"), item.get("kind"), item.get("config", {})
        if not isinstance(cid, str) or len(cid) > 80 or cid in ids:
            raise ValueError("Acceptance check IDs must be unique")
        if not isinstance(label, str) or not label.strip() or len(label) > 500 or not isinstance(config, dict):
            raise ValueError("Provide a label and valid configuration for each check")
        if kind == "file_exists" and set(config) == {"path"}:
            _relative_parts(config["path"])
        elif kind == "json_schema" and set(config) == {"filename", "schema"}:
            _filename(config["filename"])
            schema = config["schema"]
            _schema_subset(schema)
            try:
                if len(json.dumps(schema, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 32 * 1024:
                    raise ValueError("JSON Schema exceeds 32 KiB")
                Draft202012Validator.check_schema(schema)
            except (SchemaError, RecursionError) as exc:
                raise ValueError("Invalid JSON Schema") from exc
        elif kind == "artifact_published" and set(config) == {"kind", "identifier"}:
            if config["kind"] == "file":
                _filename(config["identifier"])
            elif config["kind"] != "url" or not isinstance(config["identifier"], str) or not config["identifier"].strip():
                raise ValueError("Provide the published filename or URL artifact title")
            if len(config["identifier"]) > 500:
                raise ValueError("Artifact identifier is too long")
        elif kind != "manual" or config:
            raise ValueError("Unsupported acceptance check or configuration")
        ids.add(cid)
        checks.append({"id": cid, "label": label.strip(), "kind": kind, "config": config})
    return checks


def _relative_parts(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ValueError("Provide a workspace-relative file path")
    windows_path = PureWindowsPath(value)
    parts = tuple(value.replace("\\", "/").split("/"))
    is_reserved = getattr(ntpath, "isreserved", lambda part: PureWindowsPath(part).is_reserved())
    if windows_path.anchor or any(
        part in {"", ".", ".."} or part.endswith((" ", ".")) or is_reserved(part)
        or any(char in '<>:"|?*' or ord(char) < 32 for char in part) for part in parts
    ):
        raise ValueError("File path must stay inside the workspace")
    return parts


def _filename(value: Any) -> None:
    if len(_relative_parts(value)) != 1:
        raise ValueError("Provide an exact artifact filename without directories")


def _regular_file(root: Path, parts: tuple[str, ...]) -> tuple[Path, Any]:
    path = root.resolve()
    for part in parts:
        path = path / part
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("Linked path")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Not a regular file")
    return path, info


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("Duplicate JSON key")
    return value


def _check_json_depth(value: Any, depth: int = 0) -> None:
    if depth > 64 or isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Unsupported JSON value or nesting")
    if isinstance(value, (dict, list)):
        for child in value.values() if isinstance(value, dict) else value:
            _check_json_depth(child, depth + 1)


class AcceptanceService:
    def __init__(self, db: Database, workspace: Path, artifacts: Path):
        self.db = db
        self.workspace = workspace
        self.artifacts = artifacts

    async def bind(
        self, db: Connection, run: dict[str, Any], task_id: str, version: Any, *, replaces: str | None = None,
    ) -> None:
        task = await self._task(db, task_id)
        if not task or task.get("owner_conversation_id") != run["conversation_id"]:
            raise KeyError("Task not found in this conversation")
        if task["status"] not in {"active", "blocked"} or not task.get("acceptance_checks"):
            raise ValueError("Task is not ready for acceptance")
        if type(version) is not int or version != task["spec_revision"]:
            raise ValueError("Task Spec changed. Reload it and start a new run")
        latest = await self._latest(db, task_id)
        if latest and latest["status"] == "running":
            raise ValueError("An acceptance run is already active for this task")
        if replaces and (not latest or latest["run_id"] != replaces):
            raise ValueError("Only the latest acceptance run can be recovered")
        run_id = run["id"]
        task["run_ids"].append(run_id)
        task["spec_bindings"][run_id] = version
        await self._save_task(db, task)
        payload = {
            "version": 1, "checks": task["acceptance_checks"], "attempts": [], "reviews": {},
            "task_title": task["title"], "goal": task["goal"], "constraints": task.get("constraints", []),
        }
        await db.execute(
            "INSERT INTO task_run_acceptance VALUES(?,?,?,?,?,?,?)",
            (run_id, task_id, version, latest["submission"] + 1 if latest else 1, "running", dumps(payload), now()),
        )

    async def restore(self) -> None:
        stamp = now()
        async with self.db.transaction() as db:
            await db.execute(
                "UPDATE runs SET status='interrupted',stop_reason='host_restart',updated_at=?,completed_at=? "
                "WHERE status IN ('pending','running') AND id IN (SELECT run_id FROM task_run_acceptance)",
                (stamp, stamp),
            )
            await db.execute("UPDATE task_run_acceptance SET status='not_evaluated',updated_at=? WHERE status='running'", (stamp,))
            await db.execute("UPDATE task_run_acceptance SET status='queued',updated_at=? WHERE status='checking'", (stamp,))

    async def finish_run(self, db: Connection, run_id: str, status: str) -> bool:
        cursor = await db.execute(
            "UPDATE task_run_acceptance SET status=?,updated_at=? WHERE run_id=? AND status='running'",
            ("queued" if status == "completed" else "not_evaluated", now(), run_id),
        )
        return cursor.rowcount > 0

    async def get(self, run_id: str) -> dict[str, Any] | None:
        async with self.db.connect() as db:
            row = await self._row(db, run_id)
            if not row:
                return None
            task = await self._task(db, row["task_id"])
            latest = await self._latest(db, row["task_id"])
            return {
                **{key: row[key] for key in ("run_id", "task_id", "spec_revision", "status", "updated_at")},
                **loads(row["data_json"], {}),
                "current_spec_revision": task["spec_revision"],
                "current": self._is_current(row, task, latest),
                "open": task["status"] in {"active", "blocked"},
            }

    async def latest_run_id(self, task_id: str) -> str | None:
        async with self.db.connect() as db:
            row = await self._latest(db, task_id)
            return row["run_id"] if row else None

    async def process_next(self) -> str | None:
        async with self.db.transaction() as db:
            row = await (await db.execute(
                "SELECT * FROM task_run_acceptance WHERE status='queued' ORDER BY updated_at LIMIT 1",
            )).fetchone()
            if not row:
                return None
            run_id = row["run_id"]
            await db.execute("UPDATE task_run_acceptance SET status='checking',updated_at=? WHERE run_id=?", (now(), run_id))
        payload = loads(row["data_json"], {})
        try:
            artifacts = await self.db.fetchall(
                "SELECT data_json FROM artifacts WHERE json_extract(data_json,'$.run_id')=?", (run_id,),
            )
            results = await asyncio.to_thread(self._evaluate, payload["checks"], [loads(a["data_json"], {}) for a in artifacts])
        except Exception:
            logger.exception("Acceptance check failed for run %s", run_id)
            results = [{"criterion_id": check["id"], "status": "error", "code": "check_error", "checked_at": now()}
                       for check in payload["checks"]]
        statuses = {result["status"] for result in results}
        if "error" in statuses:
            status = "check_error"
        elif "failed" in statuses:
            status = "needs_changes"
        elif "pending_review" in statuses:
            status = "awaiting_review"
        else:
            status = "passed"
        payload["attempts"].append({"checked_at": now(), "results": results})
        async with self.db.transaction() as db:
            await self._write(db, row, status, payload)
            if status == "passed":
                await self._complete_task(db, row)
        return run_id

    async def review(self, run_id: str, criterion_id: str, decision: str, version: Any, note: Any) -> None:
        if decision not in {"pass", "reject"} or not isinstance(note, str) or len(note) > 1000:
            raise ValueError("Invalid review decision or note")
        note = note.strip()
        if decision == "reject" and not note:
            raise ValueError("A rejection reason is required")
        async with self.db.transaction() as db:
            row = await self._row(db, run_id)
            if not row:
                raise KeyError("Acceptance report not found")
            payload = loads(row["data_json"], {})
            old = payload["reviews"].get(criterion_id)
            if old and old["decision"] == decision and old["note"] == note:
                return
            await self._require_current(db, row, payload, "awaiting_review", version)
            manual = {check["id"] for check in payload["checks"] if check["kind"] == "manual"}
            if criterion_id not in manual or old:
                raise ValueError("This criterion cannot be reviewed")
            payload["reviews"][criterion_id] = {"decision": decision, "note": note, "reviewed_at": now()}
            status = "needs_changes" if decision == "reject" else "awaiting_review"
            if status == "awaiting_review" and manual <= payload["reviews"].keys():
                status = "passed"
                await self._complete_task(db, row)
            await self._write(db, row, status, payload)

    async def retry(self, run_id: str, version: Any) -> None:
        async with self.db.transaction() as db:
            row = await self._row(db, run_id)
            if not row:
                raise KeyError("Acceptance report not found")
            payload = loads(row["data_json"], {})
            await self._require_current(db, row, payload, "check_error", version)
            await self._write(db, row, "queued", payload)

    async def _require_current(
        self, db: Connection, row: Any, payload: dict[str, Any], status: str, version: Any,
    ) -> None:
        task = await self._task(db, row["task_id"])
        latest = await self._latest(db, row["task_id"])
        if row["status"] != status or type(version) is not int or payload["version"] != version:
            raise ValueError("Acceptance report changed. Refresh and try again")
        if not self._is_current(row, task, latest) or task["status"] not in {"active", "blocked"}:
            raise ValueError("This report is no longer open for acceptance")

    async def _complete_task(self, db: Connection, row: Any) -> None:
        task = await self._task(db, row["task_id"])
        latest = await self._latest(db, row["task_id"])
        run = await (await db.execute("SELECT status FROM runs WHERE id=?", (row["run_id"],))).fetchone()
        if self._is_current(row, task, latest) and task["status"] in {"active", "blocked"} and run["status"] == "completed":
            task.update(status="completed", completed_at=now())
            await self._save_task(db, task)

    @staticmethod
    def _is_current(row: Any, task: dict[str, Any], latest: Any) -> bool:
        return row["spec_revision"] == task["spec_revision"] and row["run_id"] == latest["run_id"]

    @staticmethod
    async def _row(db: Connection, run_id: str) -> Any:
        return await (await db.execute("SELECT * FROM task_run_acceptance WHERE run_id=?", (run_id,))).fetchone()

    @staticmethod
    async def _latest(db: Connection, task_id: str) -> Any:
        return await (await db.execute(
            "SELECT * FROM task_run_acceptance WHERE task_id=? ORDER BY submission DESC LIMIT 1", (task_id,),
        )).fetchone()

    @staticmethod
    async def _task(db: Connection, task_id: str) -> dict[str, Any]:
        row = await (await db.execute("SELECT data_json FROM tasks WHERE id=?", (task_id,))).fetchone()
        return loads(row["data_json"], {}) if row else {}

    @staticmethod
    async def _save_task(db: Connection, task: dict[str, Any]) -> None:
        task.update(revision=int(task.get("revision", 1)) + 1, updated_at=now())
        await db.execute("UPDATE tasks SET data_json=?,updated_at=? WHERE id=?", (dumps(task), task["updated_at"], task["id"]))

    @staticmethod
    async def _write(db: Connection, row: Any, status: str, payload: dict[str, Any]) -> None:
        payload["version"] += 1
        await db.execute(
            "UPDATE task_run_acceptance SET status=?,data_json=?,updated_at=? WHERE run_id=?",
            (status, dumps(payload), now(), row["run_id"]),
        )

    def _evaluate(self, checks: list[dict[str, Any]], artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results = []
        for check in checks:
            try:
                status, code, evidence = self._evaluate_one(check, artifacts)
            except FileNotFoundError:
                status, code, evidence = "failed", "file_missing", {}
            except (ValueError, NotADirectoryError):
                status, code, evidence = "failed", "unsafe_or_nonregular_file", {}
            except OSError:
                status, code, evidence = "error", "file_unreadable", {}
            results.append({"criterion_id": check["id"], "status": status, "code": code, "checked_at": now(), "evidence": evidence})
        return results

    def _evaluate_one(self, check: dict[str, Any], artifacts: list[dict[str, Any]]) -> tuple[str, str, dict[str, Any]]:
        kind, config = check["kind"], check["config"]
        if kind == "manual":
            return "pending_review", "manual_review", {}
        if kind == "file_exists":
            _, info = _regular_file(self.workspace, _relative_parts(config["path"]))
            return "passed", "file_exists", {"path": config["path"], "size_bytes": info.st_size}
        artifact_kind = "file" if kind == "json_schema" else config["kind"]
        identifier = config["filename"] if kind == "json_schema" else config["identifier"]
        field = "filename" if artifact_kind == "file" else "title"
        matches = [artifact for artifact in artifacts if artifact.get("kind") == artifact_kind and artifact.get(field) == identifier]
        if not matches:
            return "failed", "artifact_missing", {}
        if len(matches) > 1:
            return "failed", "artifact_ambiguous", {}
        artifact = matches[0]
        evidence = {"artifact_id": artifact["id"], "published_at": artifact["created_at"], "identifier": identifier}
        if artifact_kind == "url":
            return "passed", "url_registered", evidence
        path, info = _regular_file(self.artifacts, (artifact["id"], identifier))
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(path, flags), "rb") as file:
            if not path.resolve().is_relative_to(self.artifacts.resolve()) or not os.path.samestat(info, os.fstat(file.fileno())):
                return "failed", "unsafe_or_nonregular_file", evidence
            if kind == "artifact_published":
                return "passed", "artifact_published", evidence
            if info.st_size > MAX_JSON_BYTES:
                return "failed", "json_too_large", evidence
            data = file.read(MAX_JSON_BYTES + 1)
        if len(data) > MAX_JSON_BYTES:
            return "failed", "json_too_large", evidence
        sha256 = hashlib.sha256(data).hexdigest()
        if sha256 != artifact.get("sha256"):
            return "failed", "artifact_integrity", evidence
        evidence["sha256"] = sha256
        try:
            value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_keys)
            _check_json_depth(value)
        except (ValueError, RecursionError):
            return "failed", "invalid_json", evidence
        error = next(Draft202012Validator(config["schema"]).iter_errors(value), None)
        if error is not None:
            evidence["schema_path"] = "/" + "/".join(str(part) for part in error.absolute_schema_path)
            # Object keys are input data. Only show keys named by the trusted schema.
            declared = {str(part) for part in error.absolute_schema_path}
            evidence["field_path"] = "".join(
                "/" + (str(part).replace("~", "~0").replace("/", "~1")[:160] if isinstance(part, int) or part in declared else "*")
                for part in error.absolute_path
            ) or "/"
            evidence["rule"] = str(error.validator)
            return "failed", "schema_mismatch", evidence
        return "passed", "schema_valid", evidence
