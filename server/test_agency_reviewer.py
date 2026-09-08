from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from .agency_reviewer import (
    AGENCY_REVIEW_SCHEMA_VERSION,
    AgencyDedupDecision,
    AgencyReviewDecision,
    _call_review_model,
    _call_commercial_recovery_model,
    _build_review_system_prompt,
    _build_review_user_prompt,
    _build_windows,
    _completed_photo_ids,
    _aggregate_decisions,
    _build_dedup_system_prompt,
    _confirm_global_duplicate_groups,
    _normalize_checkpoint,
    _parse_decisions,
    _parse_duplicate_confirmation,
    _parse_global_duplicate_groups,
    _review_window_with_fallback,
    _save_review_child,
    review_agency_assignments,
)
from .job_models import JobPhotoResult
from .structured_model import StructuredModelError, StructuredModelFailure


def result(photo_id: str, instruction: str, *, editorial: bool = True) -> JobPhotoResult:
    return JobPhotoResult(
        photo_id=photo_id,
        title=f"Title {photo_id}",
        description=f"Description {photo_id}",
        keywords=["test"],
        agency_instruction=instruction,
        editorial=editorial,
        commercial=not editorial,
        reason="first pass",
    )


class AgencyReviewerTests(unittest.TestCase):
    def test_normal_review_retries_with_schema_and_expands_truncated_output(self) -> None:
        client = MagicMock()
        malformed = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="length",
                    message=SimpleNamespace(content='{"decisions":[}'),
                )
            ]
        )
        valid = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        content=(
                            '{"decisions":[{"photo_id":"photo-a",'
                            '"selected_agency":"getty","technical_score":90,'
                            '"getty_score":85,"differentiated":true,'
                            '"getty_usage":"editorial","reason":"strong image"}]}'
                        )
                    ),
                )
            ]
        )
        client.chat.completions.create.side_effect = [malformed, valid]

        with (
            patch(
                "server.agency_reviewer.create_resized_jpeg",
                return_value=Path("resized.jpg"),
            ),
            patch(
                "server.agency_reviewer.encode_image_to_base64",
                return_value="encoded",
            ),
        ):
            decisions = _call_review_model(
                client=client,
                model="test-model",
                window=[result("photo-a", "agency:shutterstock_editorial")],
                preview_paths={"photo-a": Path("unused.jpg")},
                thresholds={},
                minimum_technical_score=75,
                minimum_getty_score=73,
                retry_max_tokens=8000,
            )

        self.assertEqual(decisions[0].photo_id, "photo-a")
        self.assertEqual(client.chat.completions.create.call_count, 2)
        calls = client.chat.completions.create.call_args_list
        self.assertEqual(calls[0].kwargs["max_tokens"], 4000)
        self.assertEqual(calls[1].kwargs["max_tokens"], 8000)
        self.assertEqual(calls[1].kwargs["temperature"], 0.0)
        schema = calls[0].kwargs["response_format"]["json_schema"]
        self.assertTrue(schema["strict"])
        usage = schema["schema"]["properties"]["decisions"]["items"][
            "properties"
        ]["getty_usage"]
        self.assertEqual(usage["enum"], ["commercial", "editorial"])

    def test_failed_window_splits_and_reuses_partial_child_checkpoints(self) -> None:
        results = [
            result(f"photo-{index}", "agency:shutterstock_editorial")
            for index in range(4)
        ]
        state = {"schema_version": AGENCY_REVIEW_SCHEMA_VERSION, "windows": {}}

        def call_model(**kwargs):
            window = kwargs["window"]
            if len(window) == 4:
                failure = StructuredModelFailure(
                    task=kwargs["task"],
                    attempt=3,
                    max_attempts=3,
                    error="invalid JSON",
                    finish_reason="stop",
                    max_tokens=4000,
                    raw_response="{broken}",
                )
                raise StructuredModelError(kwargs["task"], [failure])
            return [
                AgencyReviewDecision(
                    item.photo_id,
                    "adobe_shutterstock",
                    80,
                    60,
                    False,
                    "routine",
                )
                for item in window
            ]

        def save_child(child_id, child_ids, decisions):
            _save_review_child(
                state,
                "window-0001",
                [item.photo_id for item in results],
                child_id,
                child_ids,
                decisions,
                lambda checkpoint: None,
            )

        common = {
            "client": MagicMock(),
            "model": "test-model",
            "window": results,
            "window_id": "window-0001",
            "preview_paths": {
                item.photo_id: Path("unused.jpg") for item in results
            },
            "thresholds": {},
            "minimum_technical_score": 75,
            "minimum_getty_score": 73,
            "max_attempts": 3,
            "retry_max_tokens": 8000,
            "save_child": save_child,
            "diagnostics": None,
        }
        with patch("server.agency_reviewer._call_review_model", side_effect=call_model):
            decisions = _review_window_with_fallback(saved=None, **common)

        self.assertEqual([item.photo_id for item in decisions], [
            "photo-0", "photo-1", "photo-2", "photo-3"
        ])
        saved = state["windows"]["window-0001"]
        self.assertEqual(len(saved["children"]), 2)

        with patch("server.agency_reviewer._call_review_model") as model_call:
            resumed = _review_window_with_fallback(saved=saved, **common)
        model_call.assert_not_called()
        self.assertEqual(
            [item.photo_id for item in resumed],
            ["photo-0", "photo-1", "photo-2", "photo-3"],
        )

    def test_large_schema_six_checkpoint_resumes_at_first_missing_window(self) -> None:
        results = [
            result(f"photo-{index:03d}", "agency:shutterstock_editorial")
            for index in range(283)
        ]
        windows = _build_windows(results, 10, 2)
        checkpoint = {
            "schema_version": AGENCY_REVIEW_SCHEMA_VERSION,
            "windows": {},
        }
        for index, window in enumerate(windows[:33], start=1):
            checkpoint["windows"][f"window-{index:04d}"] = {
                "photo_ids": [item.photo_id for item in window],
                "decisions": [
                    AgencyReviewDecision(
                        item.photo_id,
                        "adobe_shutterstock",
                        80,
                        60,
                        False,
                        "routine",
                    ).to_dict()
                    for item in window
                ],
            }

        state = _normalize_checkpoint(checkpoint)
        completed = _completed_photo_ids(state, windows)
        first_missing = next(
            index
            for index, window in enumerate(windows, start=1)
            if not set(item.photo_id for item in window).issubset(completed)
        )

        self.assertEqual(len(completed), 266)
        self.assertEqual(first_missing, 34)
        self.assertEqual(state["schema_version"], 6)

    def test_commercial_recovery_retries_malformed_json(self) -> None:
        client = MagicMock()
        malformed = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"decisions":[{"photo_id":"photo-a" "selected_agency":"getty"}]}'
                    )
                )
            ]
        )
        valid = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=(
                            '{"decisions":[{"photo_id":"photo-a",'
                            '"selected_agency":"getty","technical_score":90,'
                            '"getty_score":85,"differentiated":true,'
                            '"getty_usage":"commercial","reason":"strong image"}]}'
                        )
                    )
                )
            ]
        )
        client.chat.completions.create.side_effect = [malformed, valid]

        with (
            patch(
                "server.agency_reviewer.create_resized_jpeg",
                return_value=Path("resized.jpg"),
            ),
            patch(
                "server.agency_reviewer.encode_image_to_base64",
                return_value="encoded",
            ),
        ):
            decisions = _call_commercial_recovery_model(
                client=client,
                model="test-model",
                window=[
                    result(
                        "photo-a",
                        "agency:adobe_shutterstock",
                        editorial=False,
                    )
                ],
                preview_paths={"photo-a": Path("unused.jpg")},
                minimum_technical_score=82,
                minimum_getty_score=75,
            )

        self.assertEqual(decisions[0].photo_id, "photo-a")
        self.assertEqual(client.chat.completions.create.call_count, 2)
        for call in client.chat.completions.create.call_args_list:
            response_format = call.kwargs["response_format"]
            self.assertEqual(response_format["type"], "json_schema")
            json_schema = response_format["json_schema"]
            self.assertTrue(json_schema["strict"])
            decision_schema = json_schema["schema"]["properties"]["decisions"][
                "items"
            ]
            self.assertFalse(decision_schema["additionalProperties"])
            self.assertEqual(
                decision_schema["properties"]["getty_usage"]["enum"],
                ["commercial"],
            )

    def test_commercial_recovery_stops_after_three_invalid_responses(self) -> None:
        client = MagicMock()
        malformed = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='{"decisions":[}')
                )
            ]
        )
        client.chat.completions.create.side_effect = [malformed] * 3

        with (
            patch(
                "server.agency_reviewer.create_resized_jpeg",
                return_value=Path("resized.jpg"),
            ),
            patch(
                "server.agency_reviewer.encode_image_to_base64",
                return_value="encoded",
            ),
            self.assertRaisesRegex(RuntimeError, "after 3 attempts"),
        ):
            _call_commercial_recovery_model(
                client=client,
                model="test-model",
                window=[
                    result(
                        "photo-a",
                        "agency:adobe_shutterstock",
                        editorial=False,
                    )
                ],
                preview_paths={"photo-a": Path("unused.jpg")},
                minimum_technical_score=82,
                minimum_getty_score=75,
            )

        self.assertEqual(client.chat.completions.create.call_count, 3)

    def test_duplicate_confirmation_requires_explicit_valid_keepers(self) -> None:
        self.assertEqual(
            _parse_duplicate_confirmation(
                {
                    "same_sequence": True,
                    "keep_photo_ids": ["photo-b"],
                    "reason": "same subject and moment",
                },
                ["photo-a", "photo-b"],
                2,
            ),
            (True, ["photo-b"], "same subject and moment"),
        )
        self.assertEqual(
            _parse_duplicate_confirmation(
                {
                    "same_sequence": False,
                    "keep_photo_ids": [],
                    "reason": "different subjects",
                },
                ["photo-a", "photo-b"],
                2,
            ),
            (False, [], "different subjects"),
        )
        with self.assertRaisesRegex(ValueError, "Unexpected keep_photo_ids"):
            _parse_duplicate_confirmation(
                {
                    "same_sequence": True,
                    "keep_photo_ids": ["photo-unknown"],
                    "reason": "invalid keeper",
                },
                ["photo-a", "photo-b"],
                2,
            )

    def test_full_image_confirmation_selects_visible_keeper_over_top_score(self) -> None:
        candidates = [
            result("photo-a", "agency:shutterstock_editorial"),
            result("photo-b", "agency:shutterstock_editorial"),
            result("photo-c", "agency:shutterstock_editorial"),
        ]
        provisional = {
            "photo-a": AgencyReviewDecision(
                "photo-a", "getty", 90, 92, True, "highest score"
            ),
            "photo-b": AgencyReviewDecision(
                "photo-b", "getty", 86, 85, True, "best visible frame"
            ),
            "photo-c": AgencyReviewDecision(
                "photo-c", "getty", 84, 82, True, "minor variation"
            ),
        }
        proposed = [
            AgencyDedupDecision(pid, "same-shoot", True, None, "same shoot")
            for pid in ("photo-a", "photo-b", "photo-c")
        ]

        with patch(
            "server.agency_reviewer._call_duplicate_confirmation_model",
            return_value=(True, ["photo-b"], "photo-b is the strongest composition"),
        ):
            decisions = _confirm_global_duplicate_groups(
                client=object(),
                model="test-model",
                candidates=candidates,
                preview_paths={item.photo_id: Path("unused.jpg") for item in candidates},
                provisional_decisions=provisional,
                proposed=proposed,
                maximum_per_sequence=2,
            )

        by_id = {decision.photo_id: decision for decision in decisions}
        self.assertTrue(by_id["photo-b"].keep_getty)
        self.assertFalse(by_id["photo-a"].keep_getty)
        self.assertFalse(by_id["photo-c"].keep_getty)
        self.assertEqual(by_id["photo-a"].duplicate_of, "photo-b")

    def test_sequence_prompt_groups_overlapping_treatments_not_broad_topics(self) -> None:
        prompt = _build_dedup_system_prompt(1, 2)

        self.assertIn("substantially overlapping visual story", prompt)
        self.assertIn("Do not group photographs solely", prompt)

    def test_global_duplicate_groups_expand_to_complete_ranked_decisions(self) -> None:
        provisional = {
            "photo-a": AgencyReviewDecision(
                "photo-a", "getty", 88, 90, True, "strongest"
            ),
            "photo-b": AgencyReviewDecision(
                "photo-b", "getty", 87, 84, True, "variation"
            ),
            "photo-c": AgencyReviewDecision(
                "photo-c", "getty", 86, 82, True, "unrelated"
            ),
        }

        decisions = _parse_global_duplicate_groups(
            {
                "duplicate_groups": [
                    {
                        "sequence_id": "same-scene",
                        "photo_ids": ["photo-a", "photo-b"],
                        "maximum_to_keep": 1,
                        "reason": "same subject and moment",
                    }
                ]
            },
            ["photo-a", "photo-b", "photo-c"],
            provisional,
            2,
        )

        by_id = {decision.photo_id: decision for decision in decisions}
        self.assertTrue(by_id["photo-a"].keep_getty)
        self.assertFalse(by_id["photo-b"].keep_getty)
        self.assertEqual(by_id["photo-b"].duplicate_of, "photo-a")
        self.assertTrue(by_id["photo-c"].keep_getty)
        self.assertTrue(by_id["photo-c"].sequence_id.startswith("unique-"))

    def test_windows_overlap_and_cover_all_photos(self) -> None:
        results = [
            result(f"photo-{index}", "agency:adobe_shutterstock")
            for index in range(7)
        ]

        windows = _build_windows(results, batch_size=4, overlap=2)

        self.assertEqual(
            [[item.photo_id for item in window] for window in windows],
            [
                ["photo-0", "photo-1", "photo-2", "photo-3"],
                ["photo-2", "photo-3", "photo-4", "photo-5"],
                ["photo-4", "photo-5", "photo-6"],
            ],
        )

    def test_second_pass_promotes_and_demotes_without_initial_agency_anchoring(self) -> None:
        results = [
            result("photo-promote", "agency:shutterstock_editorial"),
            result("photo-demote", "agency:getty_editorial"),
            result("photo-commercial", "agency:adobe_shutterstock", editorial=False),
        ]
        saved: list[dict] = []
        decisions = [
            AgencyReviewDecision(
                "photo-promote", "getty", 86, 91, True, "strong documentary action"
            ),
            AgencyReviewDecision(
                "photo-demote",
                "adobe_shutterstock",
                84,
                62,
                False,
                "routine variation",
            ),
            AgencyReviewDecision(
                "photo-commercial",
                "getty",
                86,
                88,
                True,
                "exceptional commercial atmosphere",
                getty_usage="commercial",
            ),
        ]
        finalist_decisions = [
            AgencyDedupDecision(
                "photo-promote",
                "documentary",
                True,
                None,
                "distinct editorial scene",
            ),
            AgencyDedupDecision(
                "photo-commercial",
                "commercial",
                True,
                None,
                "distinct commercial scene",
            ),
        ]

        with (
            patch("server.agency_reviewer._call_review_model", return_value=decisions),
            patch(
                "server.agency_reviewer._call_dedup_model",
                return_value=finalist_decisions,
            ),
        ):
            reviewed = review_agency_assignments(
                results=results,
                preview_paths={
                    item.photo_id: Path(f"{item.photo_id}.jpg") for item in results
                },
                checkpoint={},
                save_checkpoint=lambda state: saved.append(state.copy()),
            )

        self.assertEqual(reviewed[0].agency_instruction, "agency:getty_editorial")
        self.assertEqual(reviewed[1].agency_instruction, "agency:shutterstock_editorial")
        self.assertEqual(reviewed[2].agency_instruction, "agency:getty_commercial")
        self.assertIn("getty=91", reviewed[0].reason)
        self.assertTrue(saved)

    def test_review_prompt_hides_usage_flags_and_supports_commercial_getty(self) -> None:
        results = [
            result("photo-a", "agency:shutterstock_editorial"),
            result("photo-b", "agency:adobe_shutterstock", editorial=False),
        ]

        user_prompt = _build_review_user_prompt(results, 75, 73)
        system_prompt = _build_review_system_prompt({}, 75, 73)

        self.assertNotIn('"editorial":', user_prompt)
        self.assertNotIn('"commercial":', user_prompt)
        self.assertIn(
            "Commercially licensable photographs can qualify", system_prompt
        )
        self.assertIn("Tiny incidental distant people", system_prompt)
        self.assertIn('getty_usage "commercial"', system_prompt)

    def test_agency_review_preserves_initial_editorial_flags(self) -> None:
        results = [
            result("photo-distant-people", "agency:shutterstock_editorial")
        ]
        decisions = [
            AgencyReviewDecision(
                "photo-distant-people",
                "getty",
                85,
                79,
                True,
                "tiny people are not recognizable",
                getty_usage="commercial",
            )
        ]

        with patch("server.agency_reviewer._call_review_model", return_value=decisions):
            reviewed = review_agency_assignments(
                results=results,
                preview_paths={"photo-distant-people": Path("unused.jpg")},
                checkpoint={},
                save_checkpoint=lambda state: None,
            )

        self.assertEqual(reviewed[0].agency_instruction, "agency:getty_editorial")
        self.assertTrue(reviewed[0].editorial)
        self.assertFalse(reviewed[0].commercial)

    def test_agency_review_preserves_initial_commercial_flags(self) -> None:
        results = [
            result(
                "photo-commercial",
                "agency:adobe_shutterstock",
                editorial=False,
            )
        ]
        decisions = [
            AgencyReviewDecision(
                "photo-commercial",
                "getty",
                85,
                79,
                True,
                "reviewer considered usage editorial",
                getty_usage="editorial",
            )
        ]

        with patch("server.agency_reviewer._call_review_model", return_value=decisions):
            reviewed = review_agency_assignments(
                results=results,
                preview_paths={"photo-commercial": Path("unused.jpg")},
                checkpoint={},
                save_checkpoint=lambda state: None,
            )

        self.assertEqual(reviewed[0].agency_instruction, "agency:getty_commercial")
        self.assertFalse(reviewed[0].editorial)
        self.assertTrue(reviewed[0].commercial)

    def test_overlapping_getty_usage_disagreement_defaults_to_editorial(self) -> None:
        item = result("photo-usage", "agency:shutterstock_editorial")
        commercial = AgencyReviewDecision(
            "photo-usage", "getty", 85, 80, True, "not recognizable",
            getty_usage="commercial",
        )
        editorial = AgencyReviewDecision(
            "photo-usage", "getty", 85, 80, True, "recognizable clothing",
            getty_usage="editorial",
        )
        state = {
            "windows": {
                "window-0001": {
                    "photo_ids": ["photo-usage"],
                    "decisions": [commercial.to_dict()],
                },
                "window-0002": {
                    "photo_ids": ["photo-usage"],
                    "decisions": [editorial.to_dict()],
                },
            }
        }

        aggregated = _aggregate_decisions(
            [item], state, minimum_technical_score=75, minimum_getty_score=73
        )

        self.assertEqual(aggregated["photo-usage"].getty_usage, "editorial")

    def test_review_decision_requires_valid_getty_usage(self) -> None:
        payload = {
            "decisions": [
                {
                    "photo_id": "photo-a",
                    "selected_agency": "getty",
                    "technical_score": 85,
                    "getty_score": 80,
                    "differentiated": True,
                    "reason": "strong image",
                }
            ]
        }

        with self.assertRaisesRegex(ValueError, "Invalid getty_usage"):
            _parse_decisions(payload, ["photo-a"])

    def test_getty_score_below_absolute_threshold_is_not_promoted(self) -> None:
        results = [result("photo-best-of-weak-set", "agency:shutterstock_editorial")]
        decisions = [
            AgencyReviewDecision(
                "photo-best-of-weak-set",
                "getty",
                88,
                69,
                True,
                "best frame but routine",
            )
        ]

        with patch("server.agency_reviewer._call_review_model", return_value=decisions):
            reviewed = review_agency_assignments(
                results=results,
                preview_paths={"photo-best-of-weak-set": Path("unused.jpg")},
                checkpoint={},
                save_checkpoint=lambda state: None,
            )

        self.assertEqual(reviewed[0].agency_instruction, "agency:shutterstock_editorial")

    def test_premium_commercial_recovery_promotes_prefiltered_candidate(self) -> None:
        results = [
            result("photo-premium", "agency:adobe_shutterstock", editorial=False),
            result("photo-routine", "agency:adobe_shutterstock", editorial=False),
        ]
        first_pass = [
            AgencyReviewDecision(
                "photo-premium", "adobe_shutterstock", 80, 60, False, "near miss",
                getty_usage="commercial",
            ),
            AgencyReviewDecision(
                "photo-routine", "adobe_shutterstock", 80, 59, False, "routine",
                getty_usage="commercial",
            ),
        ]
        recovery = [
            AgencyReviewDecision(
                "photo-premium",
                "getty",
                85,
                78,
                True,
                "exceptional graphic commercial treatment",
                getty_usage="commercial",
            )
        ]
        saved: list[dict] = []

        with (
            patch(
                "server.agency_reviewer._call_review_model",
                return_value=first_pass,
            ),
            patch(
                "server.agency_reviewer._call_commercial_recovery_model",
                return_value=recovery,
            ) as recovery_call,
        ):
            reviewed = review_agency_assignments(
                results=results,
                preview_paths={item.photo_id: Path("unused.jpg") for item in results},
                checkpoint={},
                save_checkpoint=lambda state: saved.append(state.copy()),
            )

        recovery_call.assert_called_once()
        self.assertEqual(reviewed[0].agency_instruction, "agency:getty_commercial")
        self.assertFalse(reviewed[0].editorial)
        self.assertTrue(reviewed[0].commercial)
        self.assertEqual(reviewed[1].agency_instruction, "agency:adobe_shutterstock")
        self.assertFalse(reviewed[1].editorial)
        self.assertTrue(reviewed[1].commercial)
        self.assertEqual(saved[-1]["summary"]["commercial_recovery_candidates"], 1)
        self.assertEqual(saved[-1]["summary"]["commercial_recovery_promotions"], 1)

    def test_premium_commercial_recovery_cannot_bypass_hard_gates(self) -> None:
        results = [
            result("photo-borderline", "agency:adobe_shutterstock", editorial=False)
        ]
        first_pass = [
            AgencyReviewDecision(
                "photo-borderline", "adobe_shutterstock", 84, 68, False, "near miss",
                getty_usage="commercial",
            )
        ]
        recovery = [
            AgencyReviewDecision(
                "photo-borderline", "getty", 81, 90, True, "visually unusual",
                getty_usage="commercial",
            )
        ]

        with (
            patch(
                "server.agency_reviewer._call_review_model",
                return_value=first_pass,
            ),
            patch(
                "server.agency_reviewer._call_commercial_recovery_model",
                return_value=recovery,
            ),
        ):
            reviewed = review_agency_assignments(
                results=results,
                preview_paths={"photo-borderline": Path("unused.jpg")},
                checkpoint={},
                save_checkpoint=lambda state: None,
            )

        self.assertEqual(reviewed[0].agency_instruction, "agency:adobe_shutterstock")

    def test_initial_editorial_photo_cannot_enter_commercial_recovery(self) -> None:
        results = [result("photo-editorial", "agency:shutterstock_editorial")]
        first_pass = [
            AgencyReviewDecision(
                "photo-editorial",
                "adobe_shutterstock",
                84,
                70,
                False,
                "commercial-looking near miss",
                getty_usage="commercial",
            )
        ]
        saved: list[dict] = []

        with (
            patch(
                "server.agency_reviewer._call_review_model",
                return_value=first_pass,
            ),
            patch(
                "server.agency_reviewer._call_commercial_recovery_model"
            ) as recovery_call,
        ):
            reviewed = review_agency_assignments(
                results=results,
                preview_paths={"photo-editorial": Path("unused.jpg")},
                checkpoint={},
                save_checkpoint=lambda state: saved.append(state.copy()),
            )

        recovery_call.assert_not_called()
        self.assertEqual(reviewed[0].agency_instruction, "agency:shutterstock_editorial")
        self.assertEqual(saved[-1]["summary"]["commercial_recovery_candidates"], 0)

    def test_technically_weak_photo_is_not_promoted(self) -> None:
        results = [result("photo-soft", "agency:shutterstock_editorial")]
        saved: list[dict] = []
        decisions = [
            AgencyReviewDecision(
                "photo-soft", "getty", 70, 90, True, "rare but visibly soft"
            )
        ]

        with patch("server.agency_reviewer._call_review_model", return_value=decisions):
            reviewed = review_agency_assignments(
                results=results,
                preview_paths={"photo-soft": Path("unused.jpg")},
                checkpoint={},
                save_checkpoint=lambda state: saved.append(state.copy()),
            )

        self.assertEqual(reviewed[0].agency_instruction, "agency:shutterstock_editorial")
        self.assertEqual(saved[-1]["summary"]["final_getty_count"], 0)
        self.assertFalse(saved[-1]["summary"]["target_reached"])
        self.assertFalse(saved[-1]["summary"]["target_enforced"])

    def test_finalist_review_demotes_cross_window_duplicate(self) -> None:
        results = [
            result("photo-a", "agency:shutterstock_editorial"),
            result("photo-b", "agency:shutterstock_editorial"),
        ]
        first_pass = [
            AgencyReviewDecision("photo-a", "getty", 88, 90, True, "strongest action"),
            AgencyReviewDecision("photo-b", "getty", 86, 85, True, "similar action"),
        ]
        finalist_pass = [
            AgencyDedupDecision(
                "photo-a", "action-sequence", True, None, "strongest representative"
            ),
            AgencyDedupDecision(
                "photo-b", "action-sequence", False, "photo-a", "repeats photo-a"
            ),
        ]

        with (
            patch("server.agency_reviewer._call_review_model", return_value=first_pass),
            patch(
                "server.agency_reviewer._call_dedup_model",
                return_value=finalist_pass,
            ),
        ):
            reviewed = review_agency_assignments(
                results=results,
                preview_paths={item.photo_id: Path("unused.jpg") for item in results},
                checkpoint={},
                save_checkpoint=lambda state: None,
            )

        self.assertEqual(reviewed[0].agency_instruction, "agency:getty_editorial")
        self.assertEqual(reviewed[1].agency_instruction, "agency:shutterstock_editorial")

    def test_finalist_review_keeps_qualified_unrelated_scenes(self) -> None:
        results = [
            result("photo-labor", "agency:shutterstock_editorial"),
            result("photo-storm", "agency:shutterstock_editorial"),
            result("photo-sport", "agency:shutterstock_editorial"),
        ]
        first_pass = [
            AgencyReviewDecision("photo-labor", "getty", 85, 88, True, "labor story"),
            AgencyReviewDecision("photo-storm", "getty", 87, 86, True, "rare weather"),
            AgencyReviewDecision("photo-sport", "getty", 84, 82, True, "peak action"),
        ]
        finalist_pass = [
            AgencyDedupDecision(
                "photo-labor", "labor", True, None, "different labor scene"
            ),
            AgencyDedupDecision(
                "photo-storm", "storm", True, None, "different weather scene"
            ),
            AgencyDedupDecision(
                "photo-sport", "sport", True, None, "different action scene"
            ),
        ]

        with (
            patch("server.agency_reviewer._call_review_model", return_value=first_pass),
            patch("server.agency_reviewer._call_dedup_model", return_value=finalist_pass),
        ):
            reviewed = review_agency_assignments(
                results=results,
                preview_paths={item.photo_id: Path("unused.jpg") for item in results},
                checkpoint={},
                save_checkpoint=lambda state: None,
            )

        self.assertTrue(
            all(item.agency_instruction == "agency:getty_editorial" for item in reviewed)
        )

    def test_finalist_review_enforces_two_image_sequence_maximum(self) -> None:
        results = [
            result("photo-a", "agency:shutterstock_editorial"),
            result("photo-b", "agency:shutterstock_editorial"),
            result("photo-c", "agency:shutterstock_editorial"),
        ]
        first_pass = [
            AgencyReviewDecision("photo-a", "getty", 88, 90, True, "wide story"),
            AgencyReviewDecision("photo-b", "getty", 87, 86, True, "close action"),
            AgencyReviewDecision("photo-c", "getty", 86, 82, True, "minor variation"),
        ]
        finalist_pass = [
            AgencyDedupDecision("photo-a", "same-scene", True, None, "wide story"),
            AgencyDedupDecision("photo-b", "same-scene", True, None, "close action"),
            AgencyDedupDecision("photo-c", "same-scene", True, None, "third variation"),
        ]

        with (
            patch("server.agency_reviewer._call_review_model", return_value=first_pass),
            patch(
                "server.agency_reviewer._call_dedup_model",
                return_value=finalist_pass,
            ) as finalist_call,
        ):
            reviewed = review_agency_assignments(
                results=results,
                preview_paths={item.photo_id: Path("unused.jpg") for item in results},
                checkpoint={},
                save_checkpoint=lambda state: None,
            )

        self.assertEqual(
            [item.agency_instruction for item in reviewed],
            [
                "agency:getty_editorial",
                "agency:getty_editorial",
                "agency:shutterstock_editorial",
            ],
        )
        finalist_call.assert_called_once()
        self.assertEqual(
            [item.photo_id for item in finalist_call.call_args.kwargs["window"]],
            ["photo-a", "photo-b", "photo-c"],
        )

    def test_completed_review_windows_are_reused(self) -> None:
        results = [
            result("photo-a", "agency:shutterstock_editorial"),
            result("photo-b", "agency:shutterstock_editorial"),
        ]
        decisions = [
            AgencyReviewDecision(
                "photo-a", "adobe_shutterstock", 82, 60, False, "routine"
            ),
            AgencyReviewDecision(
                "photo-b", "adobe_shutterstock", 84, 62, False, "routine"
            ),
        ]
        checkpoint = {
            "schema_version": AGENCY_REVIEW_SCHEMA_VERSION,
            "windows": {
                "window-0001": {
                    "photo_ids": ["photo-a", "photo-b"],
                    "decisions": [decision.to_dict() for decision in decisions],
                }
            },
        }

        with patch("server.agency_reviewer._call_review_model") as model_call:
            reviewed = review_agency_assignments(
                results=results,
                preview_paths={item.photo_id: Path("unused.jpg") for item in results},
                checkpoint=checkpoint,
                save_checkpoint=lambda state: None,
            )

        model_call.assert_not_called()
        self.assertTrue(
            all(
                item.agency_instruction == "agency:shutterstock_editorial"
                for item in reviewed
            )
        )

    def test_schema_five_checkpoint_reuses_scoring_but_invalidates_finalist(self) -> None:
        checkpoint = {
            "schema_version": 5,
            "windows": {"window-0001": {"photo_ids": ["photo-a"]}},
            "commercial_recovery_windows": {
                "commercial-window-0001": {"photo_ids": ["photo-b"]}
            },
            "global_finalist": {"photo_ids": ["photo-a", "photo-b"]},
            "final": {"photo-a": {}},
        }

        normalized = _normalize_checkpoint(checkpoint)

        self.assertEqual(normalized["schema_version"], AGENCY_REVIEW_SCHEMA_VERSION)
        self.assertEqual(normalized["windows"], checkpoint["windows"])
        self.assertEqual(
            normalized["commercial_recovery_windows"],
            checkpoint["commercial_recovery_windows"],
        )
        self.assertNotIn("global_finalist", normalized)


if __name__ == "__main__":
    unittest.main()
