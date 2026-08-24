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


AGENCY_REVIEW_SCHEMA_VERSION = 2


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


@dataclass(frozen=True)
class AgencyReviewDecision:
    photo_id: str
    selected_agency: str
    technical_score: int
    getty_score: int
    differentiated: bool
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "photo_id": self.photo_id,
            "selected_agency": self.selected_agency,
            "technical_score": self.technical_score,
            "getty_score": self.getty_score,
            "differentiated": self.differentiated,
            "reason": self.reason,
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
            minimum_technical_score=minimum_technical_score,
            minimum_getty_score=minimum_getty_score,
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
            if _saved_dedup_window_is_valid(saved, expected_ids):
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
            decisions = _call_dedup_model(
                client=client,
                model=model,
                window=window,
                preview_paths=preview_paths,
                provisional_decisions=provisional_decisions,
                preferred_per_sequence=preferred_per_sequence,
                maximum_per_sequence=maximum_per_sequence,
            )
            state["finalist_windows"][window_id] = {
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


def _call_review_model(
    *,
    client,
    model: str,
    window: list[JobPhotoResult],
    preview_paths: dict[str, Path],
    thresholds: dict,
    minimum_technical_score: int,
    minimum_getty_score: int,
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
    try:
        response = client.chat.completions.create(
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


def _call_dedup_model(
    *,
    client,
    model: str,
    window: list[JobPhotoResult],
    preview_paths: dict[str, Path],
    provisional_decisions: dict[str, AgencyReviewDecision],
    preferred_per_sequence: int,
    maximum_per_sequence: int,
) -> list[AgencyDedupDecision]:
    metadata = [
        {
            "photo_id": item.photo_id,
            "title": item.title,
            "description": item.description,
            "technical_score": provisional_decisions[item.photo_id].technical_score,
            "getty_score": provisional_decisions[item.photo_id].getty_score,
            "qualification_reason": provisional_decisions[item.photo_id].reason,
        }
        for item in window
    ]
    content: list[dict] = [
        {
            "type": "text",
            "text": (
                "Identify only true visual/scene duplicates among these already "
                "qualified Getty candidates:\n"
                + json.dumps(metadata, ensure_ascii=False)
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
    try:
        response = client.chat.completions.create(
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
            max_tokens=3000,
            temperature=0.1,
        )
        raw = _chat_response_text(response)
        if not raw:
            raise ValueError("Agency deduplication model returned an empty response")
        data, _ = _extract_local_json_from_text(raw)
        return _parse_dedup_decisions(data, [item.photo_id for item in window])
    except Exception as exc:
        raise RuntimeError(f"Agency finalist deduplication failed: {exc}") from exc


def _build_dedup_system_prompt(
    preferred_per_sequence: int,
    maximum_per_sequence: int,
) -> str:
    return f"""
You are performing duplicate-only curation of Getty-qualified photographs.
Every supplied image has already passed hard technical and Getty-merit gates.
Do not rescore agency eligibility and do not demote an image merely because an
unrelated photograph is stronger.

DEDUPLICATION RULES:
- Group only photographs showing the same scene, moment, subject treatment, or
  near-identical composition.
- Prefer {preferred_per_sequence} strongest representative per repeated sequence.
- Keep up to {maximum_per_sequence} from one sequence only when each has a
  materially different action, story, scale, composition, atmosphere, or buyer use.
- Different subjects or genuinely different scenes must all remain Getty.
- If demoting a duplicate, duplicate_of must identify the stronger supplied frame.
- Never demote for distribution targets, quotas, or comparison with unrelated work.

Return valid JSON only with one decision for every PHOTO_ID:
{{
  "decisions": [
    {{
      "photo_id": "photo-id",
      "sequence_id": "short-shared-sequence-label",
      "keep_getty": true,
      "duplicate_of": null,
      "reason": "short visual duplication reason"
    }}
  ]
}}

For keep_getty false, duplicate_of must be another supplied PHOTO_ID. Return no
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
      "reason": "short visible-evidence reason"
    }}
  ]
}}

technical_score and getty_score must be integers from 0 to 100.
selected_agency must be exactly "getty" or "adobe_shutterstock". Return no
markdown or extra text.
""".strip()


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
            "editorial": item.editorial,
        }
        for item in window
    ]
    return (
        "Compare the attached photos and return the final agency review. "
        f"The hard technical threshold is {minimum_technical_score} and the hard "
        f"Getty merit threshold is {minimum_getty_score}. "
        "Visible evidence and differentiation must support the decision. "
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
                photo_id,
                agency,
                technical_score,
                score,
                raw["differentiated"],
                reason,
            )
        )
        seen.add(photo_id)
    if seen != set(expected_ids):
        missing = sorted(set(expected_ids) - seen)
        raise ValueError("Agency review omitted photo IDs: " + ", ".join(missing))
    return decisions


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

    for saved in state.get("finalist_windows", {}).values():
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
            )
    return final


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
        final[item.photo_id] = AgencyReviewDecision(
            photo_id=item.photo_id,
            selected_agency=selected_agency,
            technical_score=technical_score,
            getty_score=score,
            differentiated=bool(qualifying),
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
        f"agency_review:{decision.selected_agency}; technical={decision.technical_score}; "
        f"getty={decision.getty_score}; "
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
