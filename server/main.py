from __future__ import annotations

import base64
import binascii
import shutil
import tempfile
import logging
from pathlib import Path
from typing import Annotated

from fastapi import Body, FastAPI, File, Form, Header, HTTPException, UploadFile

from .config import UPLOAD_DIR
from .config import JOBS_DIR, MAX_JOB_WORKERS
from .job_models import CreateJobRequest, PreviewUploadRequest
from .job_runner import JobRunner
from .job_store import JobConflictError, JobStore, PreviewConflictError
from .models import MetadataJsonRequest, MetadataResponse
from .photo_metadata_adapter import generate_metadata_for_image


app = FastAPI(
    title="Lightroom Stock Metadata Server",
    version="1.1.0",
    root_path="/metadata",
)

LOGGER = logging.getLogger("lr-stock-metadata")
JOB_STORE = JobStore(JOBS_DIR)
JOB_RUNNER = JobRunner(JOB_STORE, MAX_JOB_WORKERS, LOGGER)


@app.on_event("startup")
def recover_interrupted_jobs() -> None:
    for job_id in JOB_STORE.mark_running_jobs_interrupted():
        LOGGER.warning("job_interrupted_after_restart job_id=%s", job_id)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _require_job(job_id: str) -> None:
    try:
        exists = JOB_STORE.exists(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Unknown job") from exc
    if not exists:
        raise HTTPException(status_code=404, detail="Unknown job")


@app.post("/jobs")
def create_job(request: CreateJobRequest) -> dict[str, object]:
    try:
        job_id, reused = JOB_STORE.create_job(request)
    except (JobConflictError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "job_id": job_id,
        "status": JOB_STORE.get_status(job_id)["status"],
        "reused": reused,
    }


@app.post("/jobs/{job_id}/photos/{photo_id}/preview")
def upload_job_preview(
    job_id: str, photo_id: str, request: PreviewUploadRequest
) -> dict[str, object]:
    _require_job(job_id)
    try:
        digest, reused = JOB_STORE.save_preview(
            job_id, photo_id, request.image_base64
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown photo_id for this job") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PreviewConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"job_id": job_id, "photo_id": photo_id, "sha256": digest, "reused": reused}


@app.post("/jobs/{job_id}/photos/{photo_id}/preview/binary")
def upload_binary_job_preview(
    job_id: str,
    photo_id: str,
    data: bytes = Body(media_type="image/jpeg"),
    content_type: str | None = Header(default=None),
) -> dict[str, object]:
    _require_job(job_id)
    if (content_type or "").split(";", 1)[0].strip().lower() != "image/jpeg":
        raise HTTPException(
            status_code=415, detail="Preview Content-Type must be image/jpeg"
        )
    try:
        digest, reused = JOB_STORE.save_preview_bytes(job_id, photo_id, data)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="Unknown photo_id for this job"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PreviewConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"job_id": job_id, "photo_id": photo_id, "sha256": digest, "reused": reused}


@app.get("/jobs/{job_id}/uploads")
def uploaded_job_photos(job_id: str) -> dict[str, object]:
    _require_job(job_id)
    return {"job_id": job_id, "uploaded_photo_ids": JOB_STORE.uploaded_photo_ids(job_id)}


@app.post("/jobs/{job_id}/run")
def run_job(job_id: str) -> dict[str, object]:
    _require_job(job_id)
    try:
        return JOB_RUNNER.queue(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/jobs/{job_id}/status")
def job_status(job_id: str) -> dict[str, object]:
    _require_job(job_id)
    return JOB_STORE.get_status(job_id)


@app.get("/jobs/{job_id}/result")
def job_result(job_id: str) -> dict[str, object]:
    _require_job(job_id)
    status = JOB_STORE.get_status(job_id)
    if status.get("status") != "completed":
        return status
    try:
        return JOB_STORE.load_result(job_id).model_dump(mode="json")
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail="Stored result is invalid") from exc


@app.delete("/jobs/{job_id}")
def delete_job(job_id: str) -> dict[str, object]:
    try:
        return JOB_STORE.delete_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown job") from exc
    except JobConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _generate_or_500(image_path: str, original_filename: str, context: dict) -> dict:
    try:
        return generate_metadata_for_image(image_path, original_filename, context)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/generate-metadata", response_model=MetadataResponse)
async def generate_metadata(
    image: Annotated[UploadFile, File()],
    original_filename: Annotated[str, Form()],
    capture_time: Annotated[str | None, Form()] = None,
    camera_model: Annotated[str | None, Form()] = None,
    lens: Annotated[str | None, Form()] = None,
    iso: Annotated[str | None, Form()] = None,
    gps_latitude: Annotated[str | None, Form()] = None,
    gps_longitude: Annotated[str | None, Form()] = None,
    country: Annotated[str | None, Form()] = None,
    city: Annotated[str | None, Form()] = None,
    state: Annotated[str | None, Form()] = None,
    sublocation: Annotated[str | None, Form()] = None,
) -> dict:
    suffix = Path(image.filename or original_filename).suffix or ".jpg"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=UPLOAD_DIR) as tmp:
        shutil.copyfileobj(image.file, tmp)
        image_path = tmp.name

    context = {
        "capture_time": capture_time,
        "camera_model": camera_model,
        "lens": lens,
        "iso": iso,
        "gps_latitude": gps_latitude,
        "gps_longitude": gps_longitude,
        "country": country,
        "city": city,
        "state": state,
        "sublocation": sublocation,
    }
    return _generate_or_500(image_path, original_filename, context)


@app.post("/generate-metadata-json", response_model=MetadataResponse)
def generate_metadata_json(payload: MetadataJsonRequest) -> dict:
    try:
        image_bytes = base64.b64decode(payload.image_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid image_base64: {exc}") from exc

    suffix = Path(payload.original_filename).suffix or ".jpg"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=UPLOAD_DIR) as tmp:
        tmp.write(image_bytes)
        image_path = tmp.name

    return _generate_or_500(image_path, payload.original_filename, payload.context)
