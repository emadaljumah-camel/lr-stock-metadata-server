from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class MetadataJsonRequest(BaseModel):
    image_base64: str
    original_filename: str
    context: dict[str, Any] = Field(default_factory=dict)


class MetadataResponse(BaseModel):
    title: str
    description: str
    keywords: list[str]
    agency_instruction: str
    editorial: bool
    commercial: bool
    reason: str

