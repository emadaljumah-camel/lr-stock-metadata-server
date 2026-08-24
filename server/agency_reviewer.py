from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .config import int_setting, setting
from .job_models import JobPhotoResult
from .photo_metadata_adapter import (
    DEFAULT_AGENCY_ROUTING_THRESHOLDS,
    DEFAULT_HYBRID_METADATA_MODEL,
    DEFAULT_LM_STUDIO_BASE_URL,
    _chat_response_text,
    _extract_local_json_from_text,
    build_common_agency_selection_rules,
    create_lm_studio_client,
    create_resized_jpeg,
    encode_image_to_base64,
    normalize_agency_routing_thresholds,
)


AGENCY_REVIEW_SCHEMA_VERSION = 1


def _bool_setting(path: str, default: bool, *env_names: str) -> bool:
    value = setting(path, default, *env_names)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AgencyReviewDecision:
    photo_id: str
    selected_agency: str
    getty_score: int
    differentiated: bool
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "photo_id": self.photo_id,
            "selected_agency": self.selected_agency,
            "getty_score": self.getty_score,
            "differentiated": self.differentiated,
            "reason": self.reason,
        }


def review_agency_assignments(
    *,
    results: list[JobPhotoResult],
    preview_paths: dict[str, Path],
    checkpoint: dict,
    save_checkpoint: Callable[[dict], None],
    progress: Callable[[int, str | None], None] | None = None,
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
    model = str(
        setting(
            "metadata.agency_review.model",
            DEFAULT_HYBRID_METADATA_MODEL,
            "PHOTO_METADATA_AGENCY_REVIEW_MODEL",
        )
    ).strip()
    if not model:
        raise RuntimeError("Agency review model is not configured")

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
        decisions = _call_review_model(
            client=client,
            model=model,
            window=window,
            preview_paths=preview_paths,
            thresholds=thresholds,
        )
        state["windows"][window_id] = {
            "photo_ids": expected_ids,
            "decisions": [decision.to_dict() for decision in decisions],
        }
        save_checkpoint(state)
        completed_photo_ids.update(expected_ids)
        if progress:
            progress(min(len(completed_photo_ids), len(results)), expected_ids[-1])

    provisional_decisions = _aggregate_decisions(results, state, thresholds)
    candidates = [
        by_id[item.photo_id]
        for item in results
        if provisional_decisions[item.photo_id].selected_agency == "getty"
    ]
    final_decisions = provisional_decisions.copy()
    if len(candidates) > 1:
        finalist_windows = _build_windows(candidates, batch_size, overlap)
        state.setdefault("finalist_windows", {})
        for index, window in enumerate(finalist_windows, start=1):
            window_id = f"finalist-window-{index:04d}"
            expected_ids = [item.photo_id for item in window]
            saved = state["finalist_windows"].get(window_id)
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
            decisions = _call_review_model(
                client=client,
                model=model,
                window=window,
                preview_paths=preview_paths,
                thresholds=thresholds,
            )
            state["finalist_windows"][window_id] = {
                "photo_ids": expected_ids,
                "decisions": [decision.to_dict() for decision in decisions],
            }
            save_checkpoint(state)
        final_decisions.update(
            _aggregate_decisions(
                candidates,
                state,
                thresholds,
                windows_key="finalist_windows",
            )
        )
    state["final"] = {
        photo_id: decision.to_dict()
        for photo_id, decision in final_decisions.items()
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


def _call_review_model(
    *,
    client,
    model: str,
    window: list[JobPhotoResult],
    preview_paths: dict[str, Path],
    thresholds: dict,
) -> list[AgencyReviewDecision]:
    content: list[dict] = [
        {
            "type": "text",
            "text": _build_review_user_prompt(window, thresholds),
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
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _build_review_system_prompt(thresholds)},
                {"role": "user", "content": content},
            ],
            max_tokens=4000,
            temperature=0.1,
        )
        raw = _chat_response_text(response)
        if not raw:
            raise ValueError("Agency review model returned an empty response")
        data, _ = _extract_local_json_from_text(raw)
        return _parse_decisions(data, [item.photo_id for item in window])
    except Exception as exc:
        raise RuntimeError(f"Agency review failed: {exc}") from exc


def _build_review_system_prompt(thresholds: dict) -> str:
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

Return one decision for every PHOTO_ID as valid JSON only:
{{
  "decisions": [
    {{
      "photo_id": "photo-id",
      "selected_agency": "getty",
      "getty_score": 0,
      "differentiated": true,
      "reason": "short visible-evidence reason"
    }}
  ]
}}

getty_score must be an integer from 0 to 100. selected_agency must be exactly
"getty" or "adobe_shutterstock". Return no markdown or extra text.
""".strip()


def _build_review_user_prompt(
    window: list[JobPhotoResult], thresholds: dict
) -> str:
    metadata = [
        {
            "photo_id": item.photo_id,
            "title": item.title,
            "description": item.description,
            "editorial": item.editorial,
        }
        for item in window
    ]
    return (
        "Compare the attached photos and return the final agency review. "
        f"The possible Getty threshold is {thresholds['possible_getty_threshold']}, "
        "but visible evidence and differentiation must support the decision. "
        "The following metadata deliberately excludes every initial agency choice:\n"
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
        if photo_id not in expected_ids or photo_id in seen:
            raise ValueError(f"Unexpected or duplicate agency review photo_id: {photo_id}")
        if agency not in {"getty", "adobe_shutterstock"}:
            raise ValueError(f"Invalid selected_agency for {photo_id}: {agency}")
        if not isinstance(raw.get("differentiated"), bool):
            raise ValueError(f"differentiated must be true/false for {photo_id}")
        try:
            score = int(raw.get("getty_score"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid Getty score for {photo_id}") from exc
        if not 0 <= score <= 100:
            raise ValueError(f"Getty score out of range for {photo_id}")
        reason = str(raw.get("reason") or "").strip()
        if not reason:
            raise ValueError(f"Agency review reason is missing for {photo_id}")
        decisions.append(
            AgencyReviewDecision(photo_id, agency, score, raw["differentiated"], reason)
        )
        seen.add(photo_id)
    if seen != set(expected_ids):
        missing = sorted(set(expected_ids) - seen)
        raise ValueError("Agency review omitted photo IDs: " + ", ".join(missing))
    return decisions


def _aggregate_decisions(
    results: list[JobPhotoResult],
    state: dict,
    thresholds: dict,
    *,
    windows_key: str = "windows",
) -> dict[str, AgencyReviewDecision]:
    collected: dict[str, list[AgencyReviewDecision]] = {
        item.photo_id: [] for item in results
    }
    for saved in state[windows_key].values():
        expected = saved.get("photo_ids", [])
        for decision in _parse_decisions(
            {"decisions": saved.get("decisions", [])}, expected
        ):
            if decision.photo_id in collected:
                collected[decision.photo_id].append(decision)

    final: dict[str, AgencyReviewDecision] = {}
    threshold = thresholds["possible_getty_threshold"]
    for item in results:
        decisions = collected[item.photo_id]
        if not decisions:
            raise RuntimeError(f"No agency review decision for {item.photo_id}")
        score = round(sum(decision.getty_score for decision in decisions) / len(decisions))
        getty_votes = sum(decision.selected_agency == "getty" for decision in decisions)
        differentiated_votes = sum(decision.differentiated for decision in decisions)
        selected_agency = (
            "getty"
            if score >= threshold
            and getty_votes * 2 >= len(decisions)
            and differentiated_votes * 2 >= len(decisions)
            else "adobe_shutterstock"
        )
        reasons = list(dict.fromkeys(decision.reason for decision in decisions))
        final[item.photo_id] = AgencyReviewDecision(
            photo_id=item.photo_id,
            selected_agency=selected_agency,
            getty_score=score,
            differentiated=differentiated_votes * 2 >= len(decisions),
            reason=" | ".join(reasons)[:500],
        )
    return final


def _apply_decision(
    result: JobPhotoResult, decision: AgencyReviewDecision
) -> JobPhotoResult:
    if decision.selected_agency == "getty":
        instruction = "agency:getty_editorial" if result.editorial else "agency:getty_commercial"
    else:
        instruction = "agency:shutterstock_editorial" if result.editorial else "agency:adobe_shutterstock"
    review_note = (
        f"agency_review:{decision.selected_agency}; score={decision.getty_score}; "
        f"{decision.reason}"
    )
    return result.model_copy(
        update={
            "agency_instruction": instruction,
            "reason": f"{result.reason}; {review_note}"[:1000],
        }
    )


def _normalize_checkpoint(checkpoint: dict) -> dict:
    if checkpoint.get("schema_version") != AGENCY_REVIEW_SCHEMA_VERSION:
        return {"schema_version": AGENCY_REVIEW_SCHEMA_VERSION, "windows": {}}
    windows = checkpoint.get("windows")
    if not isinstance(windows, dict):
        windows = {}
    return {
        "schema_version": AGENCY_REVIEW_SCHEMA_VERSION,
        "windows": windows,
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


def _completed_photo_ids(state: dict, windows: Iterable[list[JobPhotoResult]]) -> set[str]:
    completed: set[str] = set()
    for index, window in enumerate(windows, start=1):
        window_id = f"window-{index:04d}"
        expected_ids = [item.photo_id for item in window]
        if _saved_window_is_valid(state["windows"].get(window_id), expected_ids):
            completed.update(expected_ids)
    return completed
