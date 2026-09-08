from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from .job_models import CreateJobRequest, JobPhotoResult, JobProgress, JobResult, JobStatus


_STORE_LOCK = Lock()


class JobConflictError(Exception):
    pass


class PreviewConflictError(Exception):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def job_dir(self, job_id: str) -> Path:
        return self.root / self._safe_job_id(job_id)

    def exists(self, job_id: str) -> bool:
        return (self.job_dir(job_id) / "job.json").is_file()

    def create_job(self, request: CreateJobRequest) -> tuple[str, bool]:
        with _STORE_LOCK:
            job_id = self._safe_job_id(request.client_job_id or uuid4().hex)
            directory = self.job_dir(job_id)
            fingerprint = self._request_fingerprint(request)
            if directory.exists():
                existing = self._read_json(directory / "job.json")
                if existing.get("_fingerprint") != fingerprint:
                    raise JobConflictError(
                        f"Job ID {job_id!r} already exists with different input"
                    )
                return job_id, True

            (directory / "previews").mkdir(parents=True)
            now = utc_now()
            payload = request.model_dump(mode="json")
            payload["_fingerprint"] = fingerprint
            payload["_created_at"] = now
            self._write_json(directory / "job.json", payload)
            self._write_json(directory / "uploads.json", {})
            self._write_json(directory / "checkpoint.json", {})
            self._write_json(
                directory / "agency_review.json",
                {"schema_version": 1, "windows": {}},
            )
            self._write_json(
                directory / "status.json",
                {
                    "job_id": job_id,
                    "status": "created",
                    "progress": JobProgress(
                        total_photos=len(request.photos)
                    ).model_dump(),
                    "errors": [],
                    "resumable": True,
                    "created_at": now,
                    "updated_at": now,
                    "started_at": None,
                    "completed_at": None,
                },
            )
            return job_id, False

    def get_request(self, job_id: str) -> CreateJobRequest:
        payload = self._read_json(self.job_dir(job_id) / "job.json")
        return CreateJobRequest.model_validate(
            {key: value for key, value in payload.items() if not key.startswith("_")}
        )

    def get_status(self, job_id: str) -> dict[str, Any]:
        return self._read_json(self.job_dir(job_id) / "status.json")

    def set_status(
        self,
        job_id: str,
        status: JobStatus,
        progress: JobProgress,
        *,
        errors: list[str] | None = None,
        resumable: bool | None = None,
    ) -> None:
        with _STORE_LOCK:
            previous = self.get_status(job_id)
            now = utc_now()
            started_at = previous.get("started_at")
            completed_at = previous.get("completed_at")
            if status == "running" and not started_at:
                started_at = now
            if status == "completed":
                completed_at = now
            self._write_json(
                self.job_dir(job_id) / "status.json",
                {
                    "job_id": job_id,
                    "status": status,
                    "progress": progress.model_dump(mode="json"),
                    "errors": previous.get("errors", []) if errors is None else errors,
                    "resumable": status != "completed" if resumable is None else resumable,
                    "created_at": previous.get("created_at", now),
                    "updated_at": now,
                    "started_at": started_at,
                    "completed_at": completed_at,
                },
            )

    def save_preview(
        self, job_id: str, photo_id: str, image_base64: str
    ) -> tuple[str, bool]:
        try:
            data = base64.b64decode(image_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("Preview is not valid base64") from exc
        return self.save_preview_bytes(job_id, photo_id, data)

    def save_preview_bytes(
        self, job_id: str, photo_id: str, data: bytes
    ) -> tuple[str, bool]:
        request = self.get_request(job_id)
        if photo_id not in {photo.photo_id for photo in request.photos}:
            raise KeyError(photo_id)
        if len(data) < 3 or data[:3] != b"\xff\xd8\xff":
            raise ValueError("Preview is not a JPEG image")

        digest = hashlib.sha256(data).hexdigest()
        with _STORE_LOCK:
            uploads_path = self.job_dir(job_id) / "uploads.json"
            uploads = self._read_json(uploads_path)
            previous = uploads.get(photo_id)
            if previous:
                if previous != digest:
                    raise PreviewConflictError(
                        f"Preview for photo {photo_id!r} already has different content"
                    )
                return digest, True
            self._write_bytes(self.preview_path(job_id, photo_id), data)
            uploads[photo_id] = digest
            self._write_json(uploads_path, uploads)
            return digest, False

    def preview_path(self, job_id: str, photo_id: str) -> Path:
        return self.job_dir(job_id) / "previews" / f"{self._safe_component(photo_id)}.jpg"

    def uploaded_photo_ids(self, job_id: str) -> list[str]:
        return sorted(self._read_json(self.job_dir(job_id) / "uploads.json"))

    def require_complete_upload(self, job_id: str) -> None:
        expected = {photo.photo_id for photo in self.get_request(job_id).photos}
        missing = sorted(expected - set(self.uploaded_photo_ids(job_id)))
        if missing:
            raise ValueError("Missing previews for photo IDs: " + ", ".join(missing))

    def load_checkpoint(self, job_id: str) -> dict[str, JobPhotoResult]:
        payload = self._read_json(self.job_dir(job_id) / "checkpoint.json")
        return {
            photo_id: JobPhotoResult.model_validate(result)
            for photo_id, result in payload.items()
        }

    def save_checkpoint(self, job_id: str, result: JobPhotoResult) -> None:
        with _STORE_LOCK:
            path = self.job_dir(job_id) / "checkpoint.json"
            payload = self._read_json(path)
            serialized = result.model_dump(mode="json")
            existing = payload.get(result.photo_id)
            if existing is not None and existing != serialized:
                raise JobConflictError(
                    f"Checkpoint for photo {result.photo_id!r} cannot be replaced"
                )
            payload[result.photo_id] = serialized
            self._write_json(path, payload)

    def load_agency_review(self, job_id: str) -> dict[str, Any]:
        path = self.job_dir(job_id) / "agency_review.json"
        if not path.is_file():
            return {"schema_version": 1, "windows": {}}
        return self._read_json(path)

    def save_agency_review(self, job_id: str, review: dict[str, Any]) -> None:
        with _STORE_LOCK:
            self._write_json(self.job_dir(job_id) / "agency_review.json", review)

    def save_model_failure(self, job_id: str, event: dict[str, Any]) -> None:
        with _STORE_LOCK:
            path = self.job_dir(job_id) / "model_failures.json"
            payload = self._read_json(path) if path.is_file() else {"events": []}
            events = payload.get("events")
            if not isinstance(events, list):
                events = []
            events.append({"recorded_at": utc_now(), **event})
            payload["events"] = events[-100:]
            self._write_json(path, payload)

    def save_result(self, result: JobResult) -> None:
        expected = [photo.photo_id for photo in self.get_request(result.job_id).photos]
        actual = [item.photo_id for item in result.results]
        if actual != expected or result.errors:
            raise ValueError("Completed result does not match the job photo order")
        self._write_json(
            self.job_dir(result.job_id) / "result.json",
            result.model_dump(mode="json"),
        )

    def load_result(self, job_id: str) -> JobResult:
        return JobResult.model_validate(
            self._read_json(self.job_dir(job_id) / "result.json")
        )

    def mark_running_jobs_interrupted(self) -> list[str]:
        interrupted: list[str] = []
        for directory in self.root.iterdir():
            if not directory.is_dir() or not (directory / "status.json").is_file():
                continue
            try:
                status = self.get_status(directory.name)
                if status.get("status") == "running":
                    self.set_status(
                        directory.name,
                        "interrupted",
                        JobProgress.model_validate(status.get("progress", {})),
                        errors=["Server restarted while the job was running"],
                        resumable=True,
                    )
                    interrupted.append(directory.name)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return interrupted

    def delete_job(self, job_id: str) -> dict[str, Any]:
        with _STORE_LOCK:
            directory = self.job_dir(job_id)
            if not (directory / "job.json").is_file():
                raise KeyError(job_id)
            status = self.get_status(job_id)
            if status.get("status") == "running":
                raise JobConflictError("A running job cannot be deleted")
            shutil.rmtree(directory)
            return {"job_id": job_id, "deleted": True}

    @staticmethod
    def _safe_job_id(value: str) -> str:
        safe = JobStore._safe_component(value)
        if not safe or safe != value:
            raise ValueError("job_id may contain only letters, numbers, dashes, and underscores")
        return safe

    @staticmethod
    def _safe_component(value: str) -> str:
        return "".join(
            character
            for character in str(value)
            if character.isalnum() or character in "-_"
        )

    @staticmethod
    def _request_fingerprint(request: CreateJobRequest) -> str:
        payload = request.model_dump(mode="json")
        payload.pop("client_job_id", None)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        JobStore._write_bytes(
            path,
            (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
        )

    @staticmethod
    def _write_bytes(path: Path, data: bytes) -> None:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
