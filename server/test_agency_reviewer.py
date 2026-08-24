from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from .agency_reviewer import (
    AgencyDedupDecision,
    AgencyReviewDecision,
    _build_windows,
    review_agency_assignments,
)
from .job_models import JobPhotoResult


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
            result("photo-commercial", "agency:getty_commercial", editorial=False),
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
                "adobe_shutterstock",
                68,
                58,
                False,
                "repeatable scenic frame",
            ),
        ]

        with patch("server.agency_reviewer._call_review_model", return_value=decisions):
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
        self.assertEqual(reviewed[2].agency_instruction, "agency:adobe_shutterstock")
        self.assertIn("getty=91", reviewed[0].reason)
        self.assertTrue(saved)

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
            patch("server.agency_reviewer._call_dedup_model", return_value=finalist_pass),
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
            patch("server.agency_reviewer._call_dedup_model", return_value=finalist_pass),
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
            "schema_version": 2,
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


if __name__ == "__main__":
    unittest.main()
