from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from PIL import Image, ImageDraw, ImageFont

from .config import int_setting, setting
from .job_models import JobPhotoResult
from .photo_metadata_adapter import (
    DEFAULT_AGENCY_ROUTING_THRESHOLDS,
    DEFAULT_HYBRID_METADATA_MODEL,
    DEFAULT_LM_STUDIO_BASE_URL,
    build_common_agency_selection_rules,
    create_lm_studio_client,
    create_resized_jpeg,
    encode_image_to_base64,
    normalize_agency_routing_thresholds,
)
from .structured_model import (
    FailureCallback,
    StructuredModelError,
    call_structured_completion,
)


AGENCY_REVIEW_SCHEMA_VERSION = 6
DEFAULT_MODEL_MAX_ATTEMPTS = 3
DEFAULT_MODEL_RETRY_MAX_TOKENS = 8000
COMMERCIAL_RECOVERY_MAX_ATTEMPTS = DEFAULT_MODEL_MAX_ATTEMPTS


def _bool_setting(path: str, default: bool, *env_names: str) -> bool:
    value = setting(path, default, *env_names)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _float_setting(path: str, default: float, *env_names: str) -> float:
    try:
        return float(setting(path, default, *env_names))
    except (TypeError, ValueError):
        return default


def _decision_response_format(
    name: str, allowed_getty_usage: list[str]
) -> dict[str, object]:
    decision_schema = {
        "type": "object",
        "properties": {
            "photo_id": {"type": "string"},
            "selected_agency": {
                "type": "string",
                "enum": ["getty", "adobe_shutterstock"],
            },
            "technical_score": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
            },
            "getty_score": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
            },
            "differentiated": {"type": "boolean"},
            "getty_usage": {"type": "string", "enum": allowed_getty_usage},
            "reason": {"type": "string"},
        },
        "required": [
            "photo_id",
            "selected_agency",
            "technical_score",
            "getty_score",
            "differentiated",
            "getty_usage",
            "reason",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "decisions": {"type": "array", "items": decision_schema}
                },
                "required": ["decisions"],
                "additionalProperties": False,
            },
        },
    }


def _agency_review_response_format() -> dict[str, object]:
    return _decision_response_format(
        "agency_review_decisions", ["commercial", "editorial"]
    )


def _commercial_recovery_response_format() -> dict[str, object]:
    return _decision_response_format("commercial_recovery_decisions", ["commercial"])


def _global_dedup_response_format(maximum_per_sequence: int) -> dict[str, object]:
    group_schema = {
        "type": "object",
        "properties": {
            "sequence_id": {"type": "string"},
            "photo_ids": {"type": "array", "items": {"type": "string"}},
            "maximum_to_keep": {
                "type": "integer",
                "minimum": 1,
                "maximum": maximum_per_sequence,
            },
            "reason": {"type": "string"},
        },
        "required": ["sequence_id", "photo_ids", "maximum_to_keep", "reason"],
        "additionalProperties": False,
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "agency_duplicate_groups",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "duplicate_groups": {"type": "array", "items": group_schema}
                },
                "required": ["duplicate_groups"],
                "additionalProperties": False,
            },
        },
    }


def _duplicate_confirmation_response_format(
    maximum_per_sequence: int,
) -> dict[str, object]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "agency_duplicate_confirmation",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "same_sequence": {"type": "boolean"},
                    "keep_photo_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": maximum_per_sequence,
                    },
                    "reason": {"type": "string"},
                },
                "required": ["same_sequence", "keep_photo_ids", "reason"],
                "additionalProperties": False,
            },
        },
    }


@dataclass(frozen=True)
class AgencyReviewDecision:
    photo_id: str
    selected_agency: str
    technical_score: int
    getty_score: int
    differentiated: bool
    reason: str
    getty_usage: str = "editorial"

    def to_dict(self) -> dict[str, object]:
        return {
            "photo_id": self.photo_id,
            "selected_agency": self.selected_agency,
            "technical_score": self.technical_score,
            "getty_score": self.getty_score,
            "differentiated": self.differentiated,
            "reason": self.reason,
            "getty_usage": self.getty_usage,
        }


@dataclass(frozen=True)
class AgencyDedupDecision:
    photo_id: str
    sequence_id: str
    keep_getty: bool
    duplicate_of: str | None
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "photo_id": self.photo_id,
            "sequence_id": self.sequence_id,
            "keep_getty": self.keep_getty,
            "duplicate_of": self.duplicate_of,
            "reason": self.reason,
        }


def review_agency_assignments(
    *,
    results: list[JobPhotoResult],
    preview_paths: dict[str, Path],
    checkpoint: dict,
    save_checkpoint: Callable[[dict], None],
    progress: Callable[[int, str | None], None] | None = None,
    diagnostics: FailureCallback | None = None,
) -> list[JobPhotoResult]:
    """Reconsider agency routing comparatively after metadata generation.

    The initial agency instructions are deliberately excluded from model input so
    an Adobe/Shutterstock choice can be promoted and a Getty choice can be
    demoted without anchoring on the first-pass answer.
    """
    if not _bool_setting(
        "metadata.agency_review.enabled", True, "PHOTO_METADATA_AGENCY_REVIEW_ENABLED"
    ):
        return results

    if not results:
        return results

    batch_size = max(
        2,
        min(
            int_setting(
                "metadata.agency_review.batch_size",
                10,
                "PHOTO_METADATA_AGENCY_REVIEW_BATCH_SIZE",
            ),
            16,
        ),
    )
    overlap = max(
        0,
        min(
            int_setting(
                "metadata.agency_review.batch_overlap",
                2,
                "PHOTO_METADATA_AGENCY_REVIEW_BATCH_OVERLAP",
            ),
            batch_size - 1,
        ),
    )
    thresholds = normalize_agency_routing_thresholds(
        setting("metadata.agency_routing_thresholds", DEFAULT_AGENCY_ROUTING_THRESHOLDS)
    )
    minimum_technical_score = max(
        0,
        min(
            int_setting(
                "metadata.agency_review.minimum_technical_score",
                75,
                "PHOTO_METADATA_AGENCY_REVIEW_MINIMUM_TECHNICAL_SCORE",
            ),
            100,
        ),
    )
    minimum_getty_score = max(
        0,
        min(
            int_setting(
                "metadata.agency_review.minimum_getty_score",
                thresholds["possible_getty_threshold"],
                "PHOTO_METADATA_AGENCY_REVIEW_MINIMUM_GETTY_SCORE",
            ),
            100,
        ),
    )
    commercial_recovery_enabled = _bool_setting(
        "metadata.agency_review.commercial_recovery.enabled",
        True,
        "PHOTO_METADATA_COMMERCIAL_RECOVERY_ENABLED",
    )
    commercial_recovery_preliminary_technical_score = max(
        minimum_technical_score,
        min(
            int_setting(
                "metadata.agency_review.commercial_recovery.preliminary_technical_score",
                80,
                "PHOTO_METADATA_COMMERCIAL_RECOVERY_PRELIMINARY_TECHNICAL_SCORE",
            ),
            100,
        ),
    )
    commercial_recovery_minimum_technical_score = max(
        minimum_technical_score,
        min(
            int_setting(
                "metadata.agency_review.commercial_recovery.minimum_technical_score",
                82,
                "PHOTO_METADATA_COMMERCIAL_RECOVERY_MINIMUM_TECHNICAL_SCORE",
            ),
            100,
        ),
    )
    commercial_recovery_preliminary_getty_score = max(
        0,
        min(
            int_setting(
                "metadata.agency_review.commercial_recovery.preliminary_getty_score",
                65,
                "PHOTO_METADATA_COMMERCIAL_RECOVERY_PRELIMINARY_GETTY_SCORE",
            ),
            100,
        ),
    )
    commercial_recovery_minimum_getty_score = max(
        minimum_getty_score,
        min(
            int_setting(
                "metadata.agency_review.commercial_recovery.minimum_getty_score",
                75,
                "PHOTO_METADATA_COMMERCIAL_RECOVERY_MINIMUM_GETTY_SCORE",
            ),
            100,
        ),
    )
    preferred_per_sequence = max(
        1,
        int_setting(
            "metadata.agency_review.preferred_getty_per_sequence",
            1,
            "PHOTO_METADATA_AGENCY_REVIEW_PREFERRED_PER_SEQUENCE",
        ),
    )
    maximum_per_sequence = max(
        preferred_per_sequence,
        int_setting(
            "metadata.agency_review.maximum_getty_per_sequence",
            2,
            "PHOTO_METADATA_AGENCY_REVIEW_MAXIMUM_PER_SEQUENCE",
        ),
    )
    target_getty_ratio = max(
        0.0,
        min(
            _float_setting(
                "metadata.agency_review.target_getty_ratio",
                0.20,
                "PHOTO_METADATA_AGENCY_REVIEW_TARGET_GETTY_RATIO",
            ),
            1.0,
        ),
    )
    model = str(
        setting(
            "metadata.agency_review.model",
            DEFAULT_HYBRID_METADATA_MODEL,
            "PHOTO_METADATA_AGENCY_REVIEW_MODEL",
        )
    ).strip()
    if not model:
        raise RuntimeError("Agency review model is not configured")
    model_max_attempts = max(
        1,
        int_setting(
            "metadata.agency_review.model_max_attempts",
            DEFAULT_MODEL_MAX_ATTEMPTS,
            "PHOTO_METADATA_AGENCY_REVIEW_MODEL_MAX_ATTEMPTS",
        ),
    )
    model_retry_max_tokens = max(
        4000,
        int_setting(
            "metadata.agency_review.model_retry_max_tokens",
            DEFAULT_MODEL_RETRY_MAX_TOKENS,
            "PHOTO_METADATA_AGENCY_REVIEW_RETRY_MAX_TOKENS",
        ),
    )

    state = _normalize_checkpoint(checkpoint)
    windows = _build_windows(results, batch_size, overlap)
    completed_photo_ids = _completed_photo_ids(state, windows)
    if progress:
        progress(len(completed_photo_ids), None)

    client = None
    by_id = {item.photo_id: item for item in results}
    for index, window in enumerate(windows, start=1):
        window_id = f"window-{index:04d}"
        expected_ids = [item.photo_id for item in window]
        saved = state["windows"].get(window_id)
        if _saved_window_is_valid(saved, expected_ids):
            continue
        if client is None:
            base_url = str(
                setting(
                    "models.lm_studio_base_url",
                    DEFAULT_LM_STUDIO_BASE_URL,
                    "LM_STUDIO_BASE_URL",
                )
            ).strip()
            client = create_lm_studio_client(base_url)
        decisions = _review_window_with_fallback(
            client=client,
            model=model,
            window=window,
            window_id=window_id,
            preview_paths=preview_paths,
            thresholds=thresholds,
            minimum_technical_score=minimum_technical_score,
            minimum_getty_score=minimum_getty_score,
            max_attempts=model_max_attempts,
            retry_max_tokens=model_retry_max_tokens,
            saved=saved,
            save_child=lambda child_id, child_ids, child_decisions: (
                _save_review_child(
                    state,
                    window_id,
                    expected_ids,
                    child_id,
                    child_ids,
                    child_decisions,
                    save_checkpoint,
                )
            ),
            diagnostics=diagnostics,
        )
        state["windows"][window_id] = {
            "photo_ids": expected_ids,
            "decisions": [decision.to_dict() for decision in decisions],
        }
        save_checkpoint(state)
        completed_photo_ids.update(expected_ids)
        if progress:
            progress(min(len(completed_photo_ids), len(results)), expected_ids[-1])

    provisional_decisions = _aggregate_decisions(
        results,
        state,
        minimum_technical_score=minimum_technical_score,
        minimum_getty_score=minimum_getty_score,
    )
    commercial_recovery_pool = [
        item
        for item in results
        if commercial_recovery_enabled
        and item.commercial
        and provisional_decisions[item.photo_id].selected_agency
        == "adobe_shutterstock"
        and provisional_decisions[item.photo_id].technical_score
        >= commercial_recovery_preliminary_technical_score
        and provisional_decisions[item.photo_id].getty_score
        >= commercial_recovery_preliminary_getty_score
    ]
    commercial_promotions: set[str] = set()
    if commercial_recovery_pool:
        recovery_windows = _build_windows(commercial_recovery_pool, batch_size, overlap)
        state.setdefault("commercial_recovery_windows", {})
        for index, window in enumerate(recovery_windows, start=1):
            window_id = f"commercial-window-{index:04d}"
            expected_ids = [item.photo_id for item in window]
            saved = state["commercial_recovery_windows"].get(window_id)
            if _saved_window_is_valid(saved, expected_ids):
                continue
            if client is None:
                base_url = str(
                    setting(
                        "models.lm_studio_base_url",
                        DEFAULT_LM_STUDIO_BASE_URL,
                        "LM_STUDIO_BASE_URL",
                    )
                ).strip()
                client = create_lm_studio_client(base_url)
            decisions = _call_commercial_recovery_model(
                client=client,
                model=model,
                window=window,
                preview_paths=preview_paths,
                minimum_technical_score=(
                    commercial_recovery_minimum_technical_score
                ),
                minimum_getty_score=commercial_recovery_minimum_getty_score,
                task=f"commercial_recovery/{window_id}",
                max_attempts=model_max_attempts,
                retry_max_tokens=model_retry_max_tokens,
                diagnostics=diagnostics,
            )
            state["commercial_recovery_windows"][window_id] = {
                "photo_ids": expected_ids,
                "decisions": [decision.to_dict() for decision in decisions],
            }
            save_checkpoint(state)
        provisional_decisions, commercial_promotions = (
            _apply_commercial_recovery_decisions(
                provisional_decisions,
                commercial_recovery_pool,
                state,
                minimum_technical_score=(
                    commercial_recovery_minimum_technical_score
                ),
                minimum_getty_score=commercial_recovery_minimum_getty_score,
            )
        )
    candidates = [
        by_id[item.photo_id]
        for item in results
        if provisional_decisions[item.photo_id].selected_agency == "getty"
    ]
    final_decisions = provisional_decisions.copy()
    if len(candidates) > 1:
        expected_ids = [item.photo_id for item in candidates]
        saved = state.get("global_finalist")
        if not _saved_dedup_window_is_valid(saved, expected_ids):
            if client is None:
                base_url = str(
                    setting(
                        "models.lm_studio_base_url",
                        DEFAULT_LM_STUDIO_BASE_URL,
                        "LM_STUDIO_BASE_URL",
                    )
                ).strip()
                client = create_lm_studio_client(base_url)
            decisions = _call_dedup_model(
                client=client,
                model=model,
                window=candidates,
                preview_paths=preview_paths,
                provisional_decisions=provisional_decisions,
                preferred_per_sequence=preferred_per_sequence,
                maximum_per_sequence=maximum_per_sequence,
                task="agency_finalist/global",
                max_attempts=model_max_attempts,
                retry_max_tokens=max(model_retry_max_tokens, 12000),
                diagnostics=diagnostics,
            )
            state["global_finalist"] = {
                "photo_ids": expected_ids,
                "decisions": [decision.to_dict() for decision in decisions],
            }
            save_checkpoint(state)
        final_decisions = _apply_dedup_decisions(
            provisional_decisions,
            candidates,
            state,
            maximum_per_sequence,
        )
    state["final"] = {
        photo_id: decision.to_dict()
        for photo_id, decision in final_decisions.items()
    }
    final_getty_count = sum(
        decision.selected_agency == "getty"
        for decision in final_decisions.values()
    )
    state["summary"] = {
        "minimum_technical_score": minimum_technical_score,
        "minimum_getty_score": minimum_getty_score,
        "qualified_candidates": len(candidates),
        "commercial_recovery_candidates": len(commercial_recovery_pool),
        "commercial_recovery_promotions": len(commercial_promotions),
        "getty_commercial_eligible": sum(
            decision.getty_usage == "commercial"
            for decision in final_decisions.values()
        ),
        "final_getty_count": final_getty_count,
        "final_getty_ratio": round(final_getty_count / len(results), 4),
        "target_getty_ratio": target_getty_ratio,
        "target_reached": final_getty_count / len(results) >= target_getty_ratio,
        "target_enforced": False,
    }
    save_checkpoint(state)
    if set(final_decisions) != set(by_id):
        raise RuntimeError("Agency review did not return a decision for every photo")
    return [
        _apply_decision(item, final_decisions[item.photo_id])
        for item in results
    ]


def _build_windows(
    results: list[JobPhotoResult], batch_size: int, overlap: int
) -> list[list[JobPhotoResult]]:
    if len(results) <= batch_size:
        return [results]
    step = max(1, batch_size - overlap)
    windows: list[list[JobPhotoResult]] = []
    start = 0
    while start < len(results):
        window = results[start : start + batch_size]
        if len(window) < 2 and windows:
            break
        windows.append(window)
        if start + batch_size >= len(results):
            break
        start += step
    last_id = results[-1].photo_id
    if last_id not in {item.photo_id for item in windows[-1]}:
        windows.append(results[-batch_size:])
    return windows


def _review_window_with_fallback(
    *,
    client,
    model: str,
    window: list[JobPhotoResult],
    window_id: str,
    preview_paths: dict[str, Path],
    thresholds: dict,
    minimum_technical_score: int,
    minimum_getty_score: int,
    max_attempts: int,
    retry_max_tokens: int,
    saved: object,
    save_child: Callable[
        [str, list[str], list[AgencyReviewDecision]], None
    ],
    diagnostics: FailureCallback | None,
) -> list[AgencyReviewDecision]:
    saved_children = (
        saved.get("children", {})
        if isinstance(saved, dict) and isinstance(saved.get("children"), dict)
        else {}
    )

    def review_segment(
        segment: list[JobPhotoResult], segment_id: str
    ) -> list[AgencyReviewDecision]:
        expected_ids = [item.photo_id for item in segment]
        saved_segment = saved_children.get(segment_id)
        if _saved_window_is_valid(saved_segment, expected_ids):
            return _parse_decisions(
                {"decisions": saved_segment.get("decisions")}, expected_ids
            )
        try:
            decisions = _call_review_model(
                client=client,
                model=model,
                window=segment,
                preview_paths=preview_paths,
                thresholds=thresholds,
                minimum_technical_score=minimum_technical_score,
                minimum_getty_score=minimum_getty_score,
                task=segment_id,
                max_attempts=max_attempts,
                retry_max_tokens=retry_max_tokens,
                diagnostics=diagnostics,
            )
        except StructuredModelError:
            if len(segment) == 1:
                raise
            midpoint = len(segment) // 2
            decisions = review_segment(segment[:midpoint], f"{segment_id}.part-01")
            decisions.extend(
                review_segment(segment[midpoint:], f"{segment_id}.part-02")
            )
        save_child(segment_id, expected_ids, decisions)
        return decisions

    if saved_children:
        if len(window) == 1:
            return review_segment(window, f"{window_id}.part-01")
        midpoint = len(window) // 2
        decisions = review_segment(window[:midpoint], f"{window_id}.part-01")
        decisions.extend(review_segment(window[midpoint:], f"{window_id}.part-02"))
        return decisions

    try:
        return _call_review_model(
            client=client,
            model=model,
            window=window,
            preview_paths=preview_paths,
            thresholds=thresholds,
            minimum_technical_score=minimum_technical_score,
            minimum_getty_score=minimum_getty_score,
            task=f"agency_review/{window_id}",
            max_attempts=max_attempts,
            retry_max_tokens=retry_max_tokens,
            diagnostics=diagnostics,
        )
    except StructuredModelError:
        if len(window) == 1:
            raise
        midpoint = len(window) // 2
        decisions = review_segment(window[:midpoint], f"{window_id}.part-01")
        decisions.extend(review_segment(window[midpoint:], f"{window_id}.part-02"))
        return decisions


def _save_review_child(
    state: dict,
    window_id: str,
    parent_photo_ids: list[str],
    child_id: str,
    child_photo_ids: list[str],
    decisions: list[AgencyReviewDecision],
    save_checkpoint: Callable[[dict], None],
) -> None:
    parent = state["windows"].get(window_id)
    if not isinstance(parent, dict) or parent.get("photo_ids") != parent_photo_ids:
        parent = {"photo_ids": parent_photo_ids, "decisions": [], "children": {}}
        state["windows"][window_id] = parent
    children = parent.setdefault("children", {})
    children[child_id] = {
        "photo_ids": child_photo_ids,
        "decisions": [decision.to_dict() for decision in decisions],
    }
    save_checkpoint(state)


def _call_review_model(
    *,
    client,
    model: str,
    window: list[JobPhotoResult],
    preview_paths: dict[str, Path],
    thresholds: dict,
    minimum_technical_score: int,
    minimum_getty_score: int,
    task: str = "agency_review",
    max_attempts: int = DEFAULT_MODEL_MAX_ATTEMPTS,
    retry_max_tokens: int = DEFAULT_MODEL_RETRY_MAX_TOKENS,
    diagnostics: FailureCallback | None = None,
) -> list[AgencyReviewDecision]:
    content: list[dict] = [
        {
            "type": "text",
            "text": _build_review_user_prompt(
                window,
                minimum_technical_score,
                minimum_getty_score,
            ),
        }
    ]
    for item in window:
        path = preview_paths[item.photo_id]
        resized = create_resized_jpeg(path, path.parent)
        content.extend(
            [
                {"type": "text", "text": f"PHOTO_ID: {item.photo_id}"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{encode_image_to_base64(resized)}"
                    },
                },
            ]
        )
    expected_ids = [item.photo_id for item in window]
    return call_structured_completion(
        client=client,
        model=model,
        messages=[
            {
                "role": "system",
                "content": _build_review_system_prompt(
                    thresholds,
                    minimum_technical_score,
                    minimum_getty_score,
                ),
            },
            {"role": "user", "content": content},
        ],
        response_format=_agency_review_response_format(),
        parser=lambda data: _parse_decisions(data, expected_ids),
        task=task,
        max_attempts=max_attempts,
        max_tokens=4000,
        retry_max_tokens=retry_max_tokens,
        on_failure=diagnostics,
    )


def _call_commercial_recovery_model(
    *,
    client,
    model: str,
    window: list[JobPhotoResult],
    preview_paths: dict[str, Path],
    minimum_technical_score: int,
    minimum_getty_score: int,
    task: str = "commercial_recovery",
    max_attempts: int = COMMERCIAL_RECOVERY_MAX_ATTEMPTS,
    retry_max_tokens: int = DEFAULT_MODEL_RETRY_MAX_TOKENS,
    diagnostics: FailureCallback | None = None,
) -> list[AgencyReviewDecision]:
    content: list[dict] = [
        {
            "type": "text",
            "text": _build_commercial_recovery_user_prompt(
                window,
                minimum_technical_score,
                minimum_getty_score,
            ),
        }
    ]
    for item in window:
        path = preview_paths[item.photo_id]
        resized = create_resized_jpeg(path, path.parent)
        content.extend(
            [
                {"type": "text", "text": f"PHOTO_ID: {item.photo_id}"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{encode_image_to_base64(resized)}"
                    },
                },
            ]
        )
    expected_ids = [item.photo_id for item in window]
    return call_structured_completion(
        client=client,
        model=model,
        messages=[
            {
                "role": "system",
                "content": _build_commercial_recovery_system_prompt(
                    minimum_technical_score,
                    minimum_getty_score,
                ),
            },
            {"role": "user", "content": content},
        ],
        response_format=_commercial_recovery_response_format(),
        parser=lambda data: _parse_decisions(data, expected_ids),
        task=task,
        max_attempts=max_attempts,
        max_tokens=3000,
        retry_max_tokens=retry_max_tokens,
        on_failure=diagnostics,
    )


def _call_dedup_model(
    *,
    client,
    model: str,
    window: list[JobPhotoResult],
    preview_paths: dict[str, Path],
    provisional_decisions: dict[str, AgencyReviewDecision],
    preferred_per_sequence: int,
    maximum_per_sequence: int,
    task: str = "agency_finalist",
    max_attempts: int = DEFAULT_MODEL_MAX_ATTEMPTS,
    retry_max_tokens: int = 12000,
    diagnostics: FailureCallback | None = None,
) -> list[AgencyDedupDecision]:
    metadata = [
        {
            "photo_id": item.photo_id,
            "technical_score": provisional_decisions[item.photo_id].technical_score,
            "getty_score": provisional_decisions[item.photo_id].getty_score,
            "title": item.title,
            "description": item.description,
        }
        for item in window
    ]
    content: list[dict] = [
        {
            "type": "text",
            "text": (
                "Identify only true visual/scene duplicates across this complete "
                "set of already qualified Getty candidates. Each contact-sheet "
                "thumbnail is labeled with its PHOTO_ID. Systematically compare "
                "every candidate across every sheet before deciding it is unique. "
                "Titles and descriptions are supporting clues only; visible "
                "evidence is authoritative:\n"
                + json.dumps(metadata, ensure_ascii=False)
            ),
        }
    ]
    for sheet_index, sheet_path in enumerate(
        _create_global_contact_sheets(window, preview_paths), start=1
    ):
        content.extend(
            [
                {"type": "text", "text": f"CONTACT_SHEET: {sheet_index}"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{encode_image_to_base64(sheet_path)}"
                    },
                },
            ]
        )
    expected_ids = [item.photo_id for item in window]
    proposed = call_structured_completion(
        client=client,
        model=model,
        messages=[
            {
                "role": "system",
                "content": _build_dedup_system_prompt(
                    preferred_per_sequence,
                    maximum_per_sequence,
                ),
            },
            {"role": "user", "content": content},
        ],
        response_format=_global_dedup_response_format(maximum_per_sequence),
        parser=lambda data: _parse_global_duplicate_groups(
            data,
            expected_ids,
            provisional_decisions,
            maximum_per_sequence,
        ),
        task=task,
        max_attempts=max_attempts,
        max_tokens=8000,
        retry_max_tokens=retry_max_tokens,
        on_failure=diagnostics,
    )
    return _confirm_global_duplicate_groups(
        client=client,
        model=model,
        candidates=window,
        preview_paths=preview_paths,
        provisional_decisions=provisional_decisions,
        proposed=proposed,
        maximum_per_sequence=maximum_per_sequence,
        task=f"{task}/confirmation",
        max_attempts=max_attempts,
        retry_max_tokens=retry_max_tokens,
        diagnostics=diagnostics,
    )


def _confirm_global_duplicate_groups(
    *,
    client,
    model: str,
    candidates: list[JobPhotoResult],
    preview_paths: dict[str, Path],
    provisional_decisions: dict[str, AgencyReviewDecision],
    proposed: list[AgencyDedupDecision],
    maximum_per_sequence: int,
    task: str = "agency_finalist/confirmation",
    max_attempts: int = DEFAULT_MODEL_MAX_ATTEMPTS,
    retry_max_tokens: int = DEFAULT_MODEL_RETRY_MAX_TOKENS,
    diagnostics: FailureCallback | None = None,
) -> list[AgencyDedupDecision]:
    by_id = {item.photo_id: item for item in candidates}
    order = {item.photo_id: index for index, item in enumerate(candidates)}
    by_sequence: dict[str, list[str]] = {}
    for decision in proposed:
        if not decision.sequence_id.startswith("unique-"):
            by_sequence.setdefault(decision.sequence_id, []).append(decision.photo_id)
    final = {decision.photo_id: decision for decision in proposed}
    for sequence_id, photo_ids in by_sequence.items():
        if len(photo_ids) < 2:
            continue
        same_sequence, keep_photo_ids, reason = _call_duplicate_confirmation_model(
            client=client,
            model=model,
            group=[by_id[photo_id] for photo_id in photo_ids],
            preview_paths=preview_paths,
            provisional_decisions=provisional_decisions,
            maximum_per_sequence=maximum_per_sequence,
            task=f"{task}/{sequence_id}",
            max_attempts=max_attempts,
            retry_max_tokens=retry_max_tokens,
            diagnostics=diagnostics,
        )
        if not same_sequence:
            for photo_id in photo_ids:
                final[photo_id] = AgencyDedupDecision(
                    photo_id=photo_id,
                    sequence_id=f"unique-{photo_id}",
                    keep_getty=True,
                    duplicate_of=None,
                    reason=reason,
                )
            continue
        keep = set(keep_photo_ids)
        winner = max(
            keep,
            key=lambda photo_id: (
                provisional_decisions[photo_id].getty_score,
                provisional_decisions[photo_id].technical_score,
                -order[photo_id],
            ),
        )
        for photo_id in photo_ids:
            kept = photo_id in keep
            final[photo_id] = AgencyDedupDecision(
                photo_id=photo_id,
                sequence_id=sequence_id,
                keep_getty=kept,
                duplicate_of=None if kept else winner,
                reason=reason,
            )
    return [final[item.photo_id] for item in candidates]


def _call_duplicate_confirmation_model(
    *,
    client,
    model: str,
    group: list[JobPhotoResult],
    preview_paths: dict[str, Path],
    provisional_decisions: dict[str, AgencyReviewDecision],
    maximum_per_sequence: int,
    task: str = "agency_duplicate_confirmation",
    max_attempts: int = DEFAULT_MODEL_MAX_ATTEMPTS,
    retry_max_tokens: int = DEFAULT_MODEL_RETRY_MAX_TOKENS,
    diagnostics: FailureCallback | None = None,
) -> tuple[bool, list[str], str]:
    content: list[dict] = [
        {
            "type": "text",
            "text": (
                "Verify from scratch whether all supplied photographs belong to "
                "the same repeated scene, subject treatment, or continuous "
                "shooting sequence and select the strongest differentiated "
                "Getty representatives. Do not assume the earlier overview was "
                "correct. Scores are supporting evidence, not a substitute for "
                "visible differentiation."
            ),
        }
    ]
    for item in group:
        path = preview_paths[item.photo_id]
        resized = create_resized_jpeg(path, path.parent)
        content.extend(
            [
                {
                    "type": "text",
                    "text": (
                        f"PHOTO_ID: {item.photo_id}; "
                        f"technical_score="
                        f"{provisional_decisions[item.photo_id].technical_score}; "
                        f"getty_score="
                        f"{provisional_decisions[item.photo_id].getty_score}"
                    ),
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{encode_image_to_base64(resized)}"
                    },
                },
            ]
        )
    expected_ids = [item.photo_id for item in group]
    return call_structured_completion(
        client=client,
        model=model,
        messages=[
            {
                "role": "system",
                "content": _build_duplicate_confirmation_system_prompt(
                    maximum_per_sequence
                ),
            },
            {"role": "user", "content": content},
        ],
        response_format=_duplicate_confirmation_response_format(
            maximum_per_sequence
        ),
        parser=lambda data: _parse_duplicate_confirmation(
            data, expected_ids, maximum_per_sequence
        ),
        task=task,
        max_attempts=max_attempts,
        max_tokens=4000,
        retry_max_tokens=retry_max_tokens,
        on_failure=diagnostics,
    )


def _create_global_contact_sheets(
    candidates: list[JobPhotoResult],
    preview_paths: dict[str, Path],
    *,
    maximum_per_sheet: int = 20,
) -> list[Path]:
    columns = 4
    cell_width = 400
    image_height = 260
    label_height = 34
    output_folder = next(iter(preview_paths.values())).parent / "_resized_for_ai"
    output_folder.mkdir(exist_ok=True)
    font = ImageFont.load_default(size=22)
    paths: list[Path] = []
    for sheet_index, start in enumerate(
        range(0, len(candidates), maximum_per_sheet), start=1
    ):
        group = candidates[start : start + maximum_per_sheet]
        rows = (len(group) + columns - 1) // columns
        sheet = Image.new(
            "RGB", (columns * cell_width, rows * (image_height + label_height)), "white"
        )
        draw = ImageDraw.Draw(sheet)
        for index, item in enumerate(group):
            column = index % columns
            row = index // columns
            left = column * cell_width
            top = row * (image_height + label_height)
            with Image.open(preview_paths[item.photo_id]) as source:
                thumbnail = source.convert("RGB")
                thumbnail.thumbnail((cell_width, image_height), Image.LANCZOS)
                image_left = left + (cell_width - thumbnail.width) // 2
                image_top = top + (image_height - thumbnail.height) // 2
                sheet.paste(thumbnail, (image_left, image_top))
            draw.rectangle(
                (left, top + image_height, left + cell_width, top + image_height + label_height),
                fill="white",
            )
            draw.text(
                (left + 8, top + image_height + 8),
                item.photo_id,
                fill="black",
                font=font,
            )
        path = output_folder / f"agency_global_finalists_{sheet_index:02d}.jpg"
        sheet.save(path, "JPEG", quality=90)
        paths.append(path)
    return paths


def _build_dedup_system_prompt(
    preferred_per_sequence: int,
    maximum_per_sequence: int,
) -> str:
    return f"""
You are performing duplicate-only curation of Getty-qualified photographs.
Every supplied image has already passed hard technical and Getty-merit gates.
Do not rescore agency eligibility and do not demote an image merely because an
unrelated photograph is stronger.

SEQUENCE-DIVERSITY RULES:
- Group near-duplicates and photographs from the same continuous shoot, setting,
  repeated subject treatment, or substantially overlapping visual story and
  buyer use, even when framing or timing differs.
- Do not group photographs solely because they share a location or broad topic.
  Different subjects and genuinely different stories remain separate.
- Look across every contact sheet; a sequence may span distant PHOTO_IDs.
- Check repeated people, distinctive clothing, boats, structures, foreground
  patterns, horizons, weather, and camera position before labeling a frame unique.
- Prefer {preferred_per_sequence} strongest representative per repeated sequence.
- Keep up to {maximum_per_sequence} from one sequence only when each has a
  materially different action, story, scale, composition, atmosphere, or buyer use.
- Different subjects or genuinely different scenes must all remain Getty.
- If demoting a duplicate, duplicate_of must identify the stronger supplied frame.
- Never demote for distribution targets, quotas, or comparison with unrelated work.

Return valid JSON only. Include only repeated groups; omit every unique image:
{{
  "duplicate_groups": [
    {{
      "sequence_id": "short-shared-sequence-label",
      "photo_ids": ["photo-id-1", "photo-id-2"],
      "maximum_to_keep": 1,
      "reason": "short visual duplication reason"
    }}
  ]
}}

maximum_to_keep must be {preferred_per_sequence} for near-duplicates. It may be
up to {maximum_per_sequence} only when that many frames have materially different
actions, stories, scales, compositions, atmospheres, or buyer uses. A PHOTO_ID
may appear in at most one group. Return {{"duplicate_groups": []}} when all
images are unique. Return no markdown or extra text.
""".strip()


def _build_duplicate_confirmation_system_prompt(maximum_per_sequence: int) -> str:
    return f"""
You are verifying one proposed duplicate group using full individual images.
Judge visible evidence from scratch.

- same_sequence is true when the photographs show the same subject, setting,
  repeated treatment, continuous shoot, or substantially overlapping visual
  story and buyer use.
- Different subjects or unrelated scenes must return false, even when they share
  a location, color palette, or broad theme.
- When true, keep_photo_ids must contain the strongest visible representative.
  Keep one for near-duplicates. Keep a second only when it provides a materially
  different action, story, scale, composition, atmosphere, or buyer use.
- Choose keep_photo_ids from the supplied PHOTO_IDs and never keep more than
  {maximum_per_sequence}.

Return valid JSON only:
{{
  "same_sequence": true,
  "keep_photo_ids": ["photo-id"],
  "reason": "short visible-evidence explanation"
}}

When same_sequence is false, return keep_photo_ids as an empty array. Return no
markdown or extra text.
""".strip()


def _build_review_system_prompt(
    thresholds: dict,
    minimum_technical_score: int,
    minimum_getty_score: int,
) -> str:
    return f"""
You are the final comparative photo editor for stock-agency routing.
Review every supplied image from scratch. You are not given the initial agency
decision and must not assume it should be preserved.

{build_common_agency_selection_rules(thresholds)}

COMPARATIVE SEQUENCE RULES:
- Compare all supplied frames as a sequence or neighboring set.
- An image initially sent to Adobe/Shutterstock may need Getty; an initial Getty
  image may need Adobe/Shutterstock. Judge only the evidence supplied here.
- Rank technical execution, subject clarity, timing, authentic story,
  composition, atmosphere, and visual memorability.
- Getty selections within a repeated sequence must be meaningfully
  differentiated from each other, not merely minor variations.
- Being the best image in a weak sequence is not sufficient for Getty.
- Do not impose a Getty quota. Select no Getty images when none independently
  passes a Getty eligibility gate.
- Conversely, do not let a sequence default entirely to Adobe/Shutterstock when
  one or more frames independently pass a Getty gate.
- The image is authoritative. Titles and descriptions are supporting context and
  may contain errors. Never combine attributes belonging to different people.
- Editorial status is a usage constraint, not a measure of Getty quality.
- Apply the same Getty quality and merit standards regardless of licensing type.
- Commercially licensable photographs can qualify through exceptional
  atmosphere, composition, visual interpretation, rarity, timing, or access.
- Do not favor photographs merely because they contain people or appear
  editorial. Do not penalize photographs merely because they contain no people
  or appear commercially licensable.

GETTY-SPECIFIC USAGE:
- Return getty_usage for every photograph, regardless of selected_agency.
- Use "commercial" when no person is reasonably recognizable and there is no
  recognizable protected brand, logo, trademark, release-sensitive property,
  or other genuine commercial-use restriction.
- Tiny incidental distant people whose identities cannot reasonably be
  determined do not automatically require Getty Editorial and should return
  getty_usage "commercial" when no other restriction is visible.
- Use "editorial" when a person is recognizable from their face, distinctive
  appearance, clothing, context, or another identifying characteristic, or when
  recognizable protected branding/property creates a release requirement.
- Getty usage is agency-specific. Do not apply a stricter Adobe/Shutterstock
  visible-person rule when determining getty_usage.

HARD QUALITY GATES:
- Score technical usability independently from Getty merit.
- technical_score covers focus, exposure, noise, processing, crop, and overall
  professional usability. It must be an integer from 0 to 100.
- A photo cannot be Getty below technical_score {minimum_technical_score}.
- A photo cannot be Getty below getty_score {minimum_getty_score}.
- Never raise either score to improve distribution or fill a target.
- A weak photo remains Adobe/Shutterstock even when it is the strongest frame in
  its sequence.

Return one decision for every PHOTO_ID as valid JSON only:
{{
  "decisions": [
    {{
      "photo_id": "photo-id",
      "selected_agency": "getty",
      "technical_score": 0,
      "getty_score": 0,
      "differentiated": true,
      "getty_usage": "commercial",
      "reason": "short visible-evidence reason"
    }}
  ]
}}

technical_score and getty_score must be integers from 0 to 100.
selected_agency must be exactly "getty" or "adobe_shutterstock".
getty_usage must be exactly "commercial" or "editorial". Return no markdown or
extra text.
""".strip()


def _build_commercial_recovery_system_prompt(
    minimum_technical_score: int,
    minimum_getty_score: int,
) -> str:
    return f"""
You are performing a premium-commercial recovery review for stock-agency
routing. Every supplied photograph is commercially licensable and was not
selected for Getty in a broader mixed-content review. Reconsider each image from
scratch against premium commercial photography standards.

PREMIUM COMMERCIAL GETTY GATE:
- Technical execution must be exceptional and score at least
  {minimum_technical_score}: sharp focus, controlled exposure, clean processing,
  deliberate composition, and professional usability.
- Getty merit must score at least {minimum_getty_score}.
- The image must offer a memorable visual interpretation, exceptional atmosphere
  or light, rare timing or access, unusually strong graphic composition, or a
  polished and distinctive commercial concept.
- Clear buyer utility supports a decision but is never sufficient by itself.
- Attractive, clean, or useful commodity stock remains Adobe/Shutterstock when
  it is routine, generic, easily repeated, or visually interchangeable.
- Do not require people, documentary narrative, or editorial context.
- Do not impose a quota and never raise a score to improve distribution.
- Judge the image itself. Titles and descriptions are supporting context only.

Return one decision for every PHOTO_ID as valid JSON only:
{{
  "decisions": [
    {{
      "photo_id": "photo-id",
      "selected_agency": "getty",
      "technical_score": 0,
      "getty_score": 0,
      "differentiated": true,
      "getty_usage": "commercial",
      "reason": "short visible-evidence reason"
    }}
  ]
}}

technical_score and getty_score must be integers from 0 to 100.
selected_agency must be exactly "getty" or "adobe_shutterstock".
getty_usage must be exactly "commercial" for every supplied recovery candidate.
Return no markdown or extra text.
""".strip()


def _build_commercial_recovery_user_prompt(
    window: list[JobPhotoResult],
    minimum_technical_score: int,
    minimum_getty_score: int,
) -> str:
    metadata = [
        {
            "photo_id": item.photo_id,
            "title": item.title,
            "description": item.description,
        }
        for item in window
    ]
    return (
        "Compare these premium-commercial candidates and reassess each one "
        "independently. "
        f"The hard technical threshold is {minimum_technical_score} and the hard "
        f"Getty merit threshold is {minimum_getty_score}. The metadata excludes "
        "all prior agency choices and scores:\n"
        + json.dumps(metadata, ensure_ascii=False)
    )


def _build_review_user_prompt(
    window: list[JobPhotoResult],
    minimum_technical_score: int,
    minimum_getty_score: int,
) -> str:
    metadata = [
        {
            "photo_id": item.photo_id,
            "title": item.title,
            "description": item.description,
        }
        for item in window
    ]
    return (
        "Compare the attached photos and return the final agency review. "
        f"The hard technical threshold is {minimum_technical_score} and the hard "
        f"Getty merit threshold is {minimum_getty_score}. "
        "Visible evidence and differentiation must support the decision. "
        "The following metadata deliberately excludes every initial agency choice "
        "and every editorial/commercial routing flag:\n"
        + json.dumps(metadata, ensure_ascii=False)
    )


def _parse_decisions(data: dict, expected_ids: list[str]) -> list[AgencyReviewDecision]:
    raw_decisions = data.get("decisions")
    if not isinstance(raw_decisions, list):
        raise ValueError("Agency review response must contain a decisions array")
    decisions: list[AgencyReviewDecision] = []
    seen: set[str] = set()
    for raw in raw_decisions:
        if not isinstance(raw, dict):
            raise ValueError("Each agency review decision must be an object")
        photo_id = str(raw.get("photo_id") or "").strip()
        agency = str(raw.get("selected_agency") or "").strip().lower()
        getty_usage = str(raw.get("getty_usage") or "").strip().lower()
        if photo_id not in expected_ids or photo_id in seen:
            raise ValueError(f"Unexpected or duplicate agency review photo_id: {photo_id}")
        if agency not in {"getty", "adobe_shutterstock"}:
            raise ValueError(f"Invalid selected_agency for {photo_id}: {agency}")
        if getty_usage not in {"commercial", "editorial"}:
            raise ValueError(f"Invalid getty_usage for {photo_id}: {getty_usage}")
        if not isinstance(raw.get("differentiated"), bool):
            raise ValueError(f"differentiated must be true/false for {photo_id}")
        try:
            technical_score = int(raw.get("technical_score"))
            score = int(raw.get("getty_score"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid technical or Getty score for {photo_id}") from exc
        if not 0 <= technical_score <= 100:
            raise ValueError(f"Technical score out of range for {photo_id}")
        if not 0 <= score <= 100:
            raise ValueError(f"Getty score out of range for {photo_id}")
        reason = str(raw.get("reason") or "").strip()
        if not reason:
            raise ValueError(f"Agency review reason is missing for {photo_id}")
        decisions.append(
            AgencyReviewDecision(
                photo_id=photo_id,
                selected_agency=agency,
                technical_score=technical_score,
                getty_score=score,
                differentiated=raw["differentiated"],
                reason=reason,
                getty_usage=getty_usage,
            )
        )
        seen.add(photo_id)
    if seen != set(expected_ids):
        missing = sorted(set(expected_ids) - seen)
        raise ValueError("Agency review omitted photo IDs: " + ", ".join(missing))
    return decisions


def _parse_global_duplicate_groups(
    data: dict,
    expected_ids: list[str],
    provisional: dict[str, AgencyReviewDecision],
    maximum_per_sequence: int,
) -> list[AgencyDedupDecision]:
    raw_groups = data.get("duplicate_groups")
    if not isinstance(raw_groups, list):
        raise ValueError("Global deduplication response must contain duplicate_groups")
    expected = set(expected_ids)
    order = {photo_id: index for index, photo_id in enumerate(expected_ids)}
    assigned: set[str] = set()
    decisions: dict[str, AgencyDedupDecision] = {}
    for raw in raw_groups:
        if not isinstance(raw, dict):
            raise ValueError("Each duplicate group must be an object")
        sequence_id = str(raw.get("sequence_id") or "").strip()
        if not sequence_id:
            raise ValueError("Duplicate group sequence_id is missing")
        raw_photo_ids = raw.get("photo_ids")
        if not isinstance(raw_photo_ids, list):
            raise ValueError(f"photo_ids must be an array for {sequence_id}")
        photo_ids = [str(value).strip() for value in raw_photo_ids]
        if len(photo_ids) < 2 or len(photo_ids) != len(set(photo_ids)):
            raise ValueError(f"Duplicate group must contain unique photo IDs: {sequence_id}")
        invalid = set(photo_ids) - expected
        if invalid:
            raise ValueError(
                f"Unexpected duplicate photo IDs for {sequence_id}: "
                + ", ".join(sorted(invalid))
            )
        overlap = set(photo_ids) & assigned
        if overlap:
            raise ValueError(
                "Photo IDs cannot appear in multiple duplicate groups: "
                + ", ".join(sorted(overlap))
            )
        maximum_to_keep = raw.get("maximum_to_keep")
        if isinstance(maximum_to_keep, bool) or not isinstance(maximum_to_keep, int):
            raise ValueError(f"maximum_to_keep must be an integer for {sequence_id}")
        if not 1 <= maximum_to_keep <= maximum_per_sequence:
            raise ValueError(f"Invalid maximum_to_keep for {sequence_id}")
        reason = str(raw.get("reason") or "").strip()
        if not reason:
            raise ValueError(f"Duplicate group reason is missing for {sequence_id}")
        ranked = sorted(
            photo_ids,
            key=lambda photo_id: (
                provisional[photo_id].getty_score,
                provisional[photo_id].technical_score,
                -order[photo_id],
            ),
            reverse=True,
        )
        keep = set(ranked[:maximum_to_keep])
        winner = ranked[0]
        for photo_id in photo_ids:
            kept = photo_id in keep
            decisions[photo_id] = AgencyDedupDecision(
                photo_id=photo_id,
                sequence_id=sequence_id,
                keep_getty=kept,
                duplicate_of=None if kept else winner,
                reason=reason,
            )
        assigned.update(photo_ids)
    for photo_id in expected_ids:
        if photo_id not in decisions:
            decisions[photo_id] = AgencyDedupDecision(
                photo_id=photo_id,
                sequence_id=f"unique-{photo_id}",
                keep_getty=True,
                duplicate_of=None,
                reason="No repeated sequence identified in the global review",
            )
    return [decisions[photo_id] for photo_id in expected_ids]


def _parse_duplicate_confirmation(
    data: dict,
    expected_ids: list[str],
    maximum_per_sequence: int,
) -> tuple[bool, list[str], str]:
    same_sequence = data.get("same_sequence")
    if not isinstance(same_sequence, bool):
        raise ValueError("same_sequence must be true/false")
    raw_keep_photo_ids = data.get("keep_photo_ids")
    if not isinstance(raw_keep_photo_ids, list):
        raise ValueError("keep_photo_ids must be an array")
    keep_photo_ids = [str(value).strip() for value in raw_keep_photo_ids]
    if len(keep_photo_ids) != len(set(keep_photo_ids)):
        raise ValueError("keep_photo_ids must contain unique photo IDs")
    invalid = set(keep_photo_ids) - set(expected_ids)
    if invalid:
        raise ValueError(
            "Unexpected keep_photo_ids: " + ", ".join(sorted(invalid))
        )
    if same_sequence:
        if not 1 <= len(keep_photo_ids) <= maximum_per_sequence:
            raise ValueError("Invalid confirmed keep_photo_ids count")
    elif keep_photo_ids:
        raise ValueError("keep_photo_ids must be empty for unrelated photographs")
    reason = str(data.get("reason") or "").strip()
    if not reason:
        raise ValueError("Duplicate confirmation reason is missing")
    return same_sequence, keep_photo_ids, reason


def _parse_dedup_decisions(
    data: dict, expected_ids: list[str]
) -> list[AgencyDedupDecision]:
    raw_decisions = data.get("decisions")
    if not isinstance(raw_decisions, list):
        raise ValueError("Agency deduplication response must contain a decisions array")
    decisions: list[AgencyDedupDecision] = []
    seen: set[str] = set()
    for raw in raw_decisions:
        if not isinstance(raw, dict):
            raise ValueError("Each agency deduplication decision must be an object")
        photo_id = str(raw.get("photo_id") or "").strip()
        if photo_id not in expected_ids or photo_id in seen:
            raise ValueError(f"Unexpected or duplicate finalist photo_id: {photo_id}")
        if not isinstance(raw.get("keep_getty"), bool):
            raise ValueError(f"keep_getty must be true/false for {photo_id}")
        sequence_id = str(raw.get("sequence_id") or "").strip()
        if not sequence_id:
            raise ValueError(f"sequence_id is missing for {photo_id}")
        keep_getty = raw["keep_getty"]
        duplicate_of_value = raw.get("duplicate_of")
        duplicate_of = (
            str(duplicate_of_value).strip()
            if duplicate_of_value not in (None, "")
            else None
        )
        if keep_getty:
            duplicate_of = None
        elif duplicate_of not in expected_ids or duplicate_of == photo_id:
            raise ValueError(f"Invalid duplicate_of for {photo_id}: {duplicate_of}")
        reason = str(raw.get("reason") or "").strip()
        if not reason:
            raise ValueError(f"Agency deduplication reason is missing for {photo_id}")
        decisions.append(
            AgencyDedupDecision(
                photo_id,
                sequence_id,
                keep_getty,
                duplicate_of,
                reason,
            )
        )
        seen.add(photo_id)
    if seen != set(expected_ids):
        missing = sorted(set(expected_ids) - seen)
        raise ValueError("Agency deduplication omitted photo IDs: " + ", ".join(missing))
    return decisions


def _apply_dedup_decisions(
    provisional: dict[str, AgencyReviewDecision],
    candidates: list[JobPhotoResult],
    state: dict,
    maximum_per_sequence: int,
) -> dict[str, AgencyReviewDecision]:
    candidate_ids = {item.photo_id for item in candidates}
    parent = {photo_id: photo_id for photo_id in candidate_ids}
    reasons: dict[tuple[str, str], str] = {}
    rejected: set[str] = set()

    def find(photo_id: str) -> str:
        while parent[photo_id] != photo_id:
            parent[photo_id] = parent[parent[photo_id]]
            photo_id = parent[photo_id]
        return photo_id

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    saved_global = state.get("global_finalist")
    saved_decisions = [saved_global] if isinstance(saved_global, dict) else []
    for saved in saved_decisions:
        expected_ids = saved.get("photo_ids", [])
        parsed = _parse_dedup_decisions(
            {"decisions": saved.get("decisions", [])}, expected_ids
        )
        by_sequence: dict[str, list[str]] = {}
        for decision in parsed:
            if decision.photo_id in candidate_ids:
                by_sequence.setdefault(decision.sequence_id, []).append(
                    decision.photo_id
                )
            if (
                not decision.keep_getty
                and decision.photo_id in candidate_ids
                and decision.duplicate_of in candidate_ids
            ):
                rejected.add(decision.photo_id)
                union(decision.photo_id, decision.duplicate_of)
                reasons[(decision.photo_id, decision.duplicate_of)] = decision.reason
        for members in by_sequence.values():
            for photo_id in members[1:]:
                union(members[0], photo_id)

    groups: dict[str, list[str]] = {}
    for photo_id in candidate_ids:
        groups.setdefault(find(photo_id), []).append(photo_id)

    final = provisional.copy()
    order = {item.photo_id: index for index, item in enumerate(candidates)}
    for members in groups.values():
        ranked = sorted(
            members,
            key=lambda photo_id: (
                provisional[photo_id].getty_score,
                provisional[photo_id].technical_score,
                -order[photo_id],
            ),
            reverse=True,
        )
        accepted = [photo_id for photo_id in ranked if photo_id not in rejected]
        keep = set((accepted or ranked)[:maximum_per_sequence])
        winner = ranked[0]
        for photo_id in members:
            if photo_id in keep:
                continue
            duplicate_reason = next(
                (
                    reason
                    for (source, target), reason in reasons.items()
                    if source == photo_id and target in members
                ),
                f"Duplicate of stronger qualified frame {winner}",
            )
            previous = provisional[photo_id]
            final[photo_id] = AgencyReviewDecision(
                photo_id=photo_id,
                selected_agency="adobe_shutterstock",
                technical_score=previous.technical_score,
                getty_score=previous.getty_score,
                differentiated=False,
                reason=f"{duplicate_reason}; retained {winner}"[:500],
                getty_usage=previous.getty_usage,
            )
    return final


def _apply_commercial_recovery_decisions(
    provisional: dict[str, AgencyReviewDecision],
    recovery_pool: list[JobPhotoResult],
    state: dict,
    *,
    minimum_technical_score: int,
    minimum_getty_score: int,
) -> tuple[dict[str, AgencyReviewDecision], set[str]]:
    pool_ids = {item.photo_id for item in recovery_pool}
    collected: dict[str, list[AgencyReviewDecision]] = {
        photo_id: [] for photo_id in pool_ids
    }
    for saved in state.get("commercial_recovery_windows", {}).values():
        expected = saved.get("photo_ids", [])
        for decision in _parse_decisions(
            {"decisions": saved.get("decisions", [])}, expected
        ):
            if decision.photo_id in collected:
                collected[decision.photo_id].append(decision)

    final = provisional.copy()
    promoted: set[str] = set()
    for item in recovery_pool:
        decisions = collected[item.photo_id]
        if not decisions:
            raise RuntimeError(
                f"No commercial recovery decision for {item.photo_id}"
            )
        technical_score = round(
            sum(decision.technical_score for decision in decisions) / len(decisions)
        )
        qualifying = [
            decision
            for decision in decisions
            if decision.selected_agency == "getty"
            and decision.technical_score >= minimum_technical_score
            and decision.getty_score >= minimum_getty_score
            and decision.differentiated
            and decision.getty_usage == "commercial"
        ]
        if technical_score < minimum_technical_score or not qualifying:
            continue
        score = max(decision.getty_score for decision in qualifying)
        reasons = list(dict.fromkeys(decision.reason for decision in qualifying))
        final[item.photo_id] = AgencyReviewDecision(
            photo_id=item.photo_id,
            selected_agency="getty",
            technical_score=technical_score,
            getty_score=score,
            differentiated=True,
            reason=("premium commercial recovery: " + " | ".join(reasons))[:500],
            getty_usage="commercial",
        )
        promoted.add(item.photo_id)
    return final, promoted


def _aggregate_decisions(
    results: list[JobPhotoResult],
    state: dict,
    *,
    minimum_technical_score: int,
    minimum_getty_score: int,
) -> dict[str, AgencyReviewDecision]:
    collected: dict[str, list[AgencyReviewDecision]] = {
        item.photo_id: [] for item in results
    }
    for saved in state["windows"].values():
        expected = saved.get("photo_ids", [])
        for decision in _parse_decisions(
            {"decisions": saved.get("decisions", [])}, expected
        ):
            if decision.photo_id in collected:
                collected[decision.photo_id].append(decision)

    final: dict[str, AgencyReviewDecision] = {}
    for item in results:
        decisions = collected[item.photo_id]
        if not decisions:
            raise RuntimeError(f"No agency review decision for {item.photo_id}")
        technical_score = round(
            sum(decision.technical_score for decision in decisions) / len(decisions)
        )
        qualifying = [
            decision
            for decision in decisions
            if decision.selected_agency == "getty"
            and decision.technical_score >= minimum_technical_score
            and decision.getty_score >= minimum_getty_score
            and decision.differentiated
        ]
        selected_agency = (
            "getty"
            if technical_score >= minimum_technical_score and qualifying
            else "adobe_shutterstock"
        )
        score = max(
            (decision.getty_score for decision in qualifying),
            default=round(
                sum(decision.getty_score for decision in decisions) / len(decisions)
            ),
        )
        evidence = qualifying or decisions
        reasons = list(dict.fromkeys(decision.reason for decision in evidence))
        getty_usage = (
            "commercial"
            if all(decision.getty_usage == "commercial" for decision in decisions)
            else "editorial"
        )
        final[item.photo_id] = AgencyReviewDecision(
            photo_id=item.photo_id,
            selected_agency=selected_agency,
            technical_score=technical_score,
            getty_score=score,
            differentiated=bool(qualifying),
            reason=" | ".join(reasons)[:500],
            getty_usage=getty_usage,
        )
    return final


def _apply_decision(
    result: JobPhotoResult, decision: AgencyReviewDecision
) -> JobPhotoResult:
    editorial = result.editorial
    if decision.selected_agency == "getty":
        instruction = (
            "agency:getty_editorial" if editorial else "agency:getty_commercial"
        )
    else:
        instruction = (
            "agency:shutterstock_editorial"
            if editorial
            else "agency:adobe_shutterstock"
        )
    review_note = (
        f"agency_review:{decision.selected_agency}; technical={decision.technical_score}; "
        f"getty={decision.getty_score}; getty_usage={decision.getty_usage}; "
        f"{decision.reason}"
    )
    return result.model_copy(
        update={
            "agency_instruction": instruction,
            "editorial": editorial,
            "commercial": not editorial,
            "reason": f"{result.reason}; {review_note}"[:1000],
        }
    )


def _normalize_checkpoint(checkpoint: dict) -> dict:
    schema_version = checkpoint.get("schema_version")
    if schema_version not in {5, AGENCY_REVIEW_SCHEMA_VERSION}:
        return {"schema_version": AGENCY_REVIEW_SCHEMA_VERSION, "windows": {}}
    windows = checkpoint.get("windows")
    if not isinstance(windows, dict):
        windows = {}
    return {
        "schema_version": AGENCY_REVIEW_SCHEMA_VERSION,
        "windows": windows,
        **(
            {"commercial_recovery_windows": checkpoint["commercial_recovery_windows"]}
            if isinstance(checkpoint.get("commercial_recovery_windows"), dict)
            else {}
        ),
        **(
            {"global_finalist": checkpoint["global_finalist"]}
            if schema_version == AGENCY_REVIEW_SCHEMA_VERSION
            and isinstance(checkpoint.get("global_finalist"), dict)
            else {}
        ),
        **(
            {"finalist_windows": checkpoint["finalist_windows"]}
            if isinstance(checkpoint.get("finalist_windows"), dict)
            else {}
        ),
        **({"final": checkpoint["final"]} if isinstance(checkpoint.get("final"), dict) else {}),
    }


def _saved_window_is_valid(saved: object, expected_ids: list[str]) -> bool:
    if not isinstance(saved, dict) or saved.get("photo_ids") != expected_ids:
        return False
    try:
        _parse_decisions({"decisions": saved.get("decisions")}, expected_ids)
    except ValueError:
        return False
    return True


def _saved_dedup_window_is_valid(saved: object, expected_ids: list[str]) -> bool:
    if not isinstance(saved, dict) or saved.get("photo_ids") != expected_ids:
        return False
    try:
        _parse_dedup_decisions({"decisions": saved.get("decisions")}, expected_ids)
    except ValueError:
        return False
    return True


def _completed_photo_ids(state: dict, windows: Iterable[list[JobPhotoResult]]) -> set[str]:
    completed: set[str] = set()
    for index, window in enumerate(windows, start=1):
        window_id = f"window-{index:04d}"
        expected_ids = [item.photo_id for item in window]
        if _saved_window_is_valid(state["windows"].get(window_id), expected_ids):
            completed.update(expected_ids)
    return completed
