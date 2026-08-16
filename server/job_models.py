from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .models import MetadataResponse


JobStatus = Literal["created", "running", "completed", "failed", "interrupted"]
JobIdentifier = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        min_length=1,
        pattern=r"^[A-Za-z0-9_-]+$",
    ),
]


class JobPhoto(BaseModel):
    model_config = ConfigDict(extra="forbid")

    photo_id: JobIdentifier
    original_filename: str = Field(min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)


class CreateJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_job_id: JobIdentifier | None = None
    client: Literal["lightroom"] = "lightroom"
    plugin_version: str = Field(default="1.1.0", min_length=1)
    catalog_path: str = ""
    photos: list[JobPhoto] = Field(min_length=1)

    @model_validator(mode="after")
    def require_unique_photo_ids(self) -> "CreateJobRequest":
        photo_ids = [photo.photo_id for photo in self.photos]
        if len(photo_ids) != len(set(photo_ids)):
            raise ValueError("photos must have unique photo_id values")
        return self


class PreviewUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_base64: str = Field(min_length=1)


class JobProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str = Field(default="created", min_length=1)
    processed_photos: int = Field(default=0, ge=0)
    total_photos: int = Field(default=0, ge=0)
    current_photo_id: str | None = None


class JobPhotoResult(MetadataResponse):
    photo_id: JobIdentifier


class JobResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: JobIdentifier
    status: Literal["completed"] = "completed"
    results: list[JobPhotoResult]
    errors: list[str] = Field(default_factory=list)

