from __future__ import annotations

import base64
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from . import main
from .job_models import CreateJobRequest, JobPhoto, JobProgress
from .job_runner import JobRunner
from .job_store import JobStore, PreviewConflictError


JPEG = b"\xff\xd8\xfftest-preview"
METADATA = {
    "title": "Test title",
    "description": "Test description",
    "keywords": ["test", "stock"],
    "agency_instruction": "agency:adobe_shutterstock",
    "editorial": False,
    "commercial": True,
    "reason": "test",
}


def job_request(job_id: str = "client-job-1") -> CreateJobRequest:
    return CreateJobRequest(
        client_job_id=job_id,
        catalog_path="catalog.lrcat",
        photos=[JobPhoto(photo_id="photo-1", original_filename="one.jpg")],
    )


class JobStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = JobStore(Path(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_create_and_upload_are_idempotent(self) -> None:
        job_id, reused = self.store.create_job(job_request())
        repeated_id, repeated = self.store.create_job(job_request())
        encoded = base64.b64encode(JPEG).decode("ascii")
        digest, upload_reused = self.store.save_preview(job_id, "photo-1", encoded)
        repeated_digest, repeated_upload = self.store.save_preview(
            job_id, "photo-1", encoded
        )

        self.assertFalse(reused)
        self.assertTrue(repeated)
        self.assertEqual(repeated_id, job_id)
        self.assertFalse(upload_reused)
        self.assertTrue(repeated_upload)
        self.assertEqual(repeated_digest, digest)

    def test_changed_preview_is_rejected(self) -> None:
        job_id, _ = self.store.create_job(job_request())
        self.store.save_preview(
            job_id, "photo-1", base64.b64encode(JPEG).decode("ascii")
        )
        with self.assertRaises(PreviewConflictError):
            self.store.save_preview(
                job_id,
                "photo-1",
                base64.b64encode(JPEG + b"changed").decode("ascii"),
            )

    def test_running_job_is_marked_interrupted_after_restart(self) -> None:
        job_id, _ = self.store.create_job(job_request())
        self.store.set_status(
            job_id, "running", JobProgress(stage="generating", total_photos=1)
        )

        self.assertEqual(self.store.mark_running_jobs_interrupted(), [job_id])
        status = self.store.get_status(job_id)
        self.assertEqual(status["status"], "interrupted")
        self.assertTrue(status["resumable"])


class JobRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = JobStore(Path(self.temporary.name))
        self.runner = JobRunner(self.store, 1, logging.getLogger("test-job-runner"))
        self.job_id, _ = self.store.create_job(job_request())
        self.store.save_preview(
            self.job_id, "photo-1", base64.b64encode(JPEG).decode("ascii")
        )

    def tearDown(self) -> None:
        self.runner.executor.shutdown(wait=True)
        self.temporary.cleanup()

    def test_process_checkpoints_and_completes_job(self) -> None:
        with patch("server.job_runner.generate_metadata_for_image", return_value=METADATA):
            self.runner.process(self.job_id)

        result = self.store.load_result(self.job_id)
        self.assertEqual(result.results[0].photo_id, "photo-1")
        self.assertEqual(result.results[0].title, "Test title")
        self.assertEqual(self.store.get_status(self.job_id)["status"], "completed")


class JobEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.previous_store = main.JOB_STORE
        self.previous_runner = main.JOB_RUNNER
        main.JOB_STORE = JobStore(Path(self.temporary.name))
        main.JOB_RUNNER = Mock()

    def tearDown(self) -> None:
        main.JOB_STORE = self.previous_store
        main.JOB_RUNNER = self.previous_runner
        self.temporary.cleanup()

    def test_create_upload_and_queue_contract(self) -> None:
        main.JOB_RUNNER.queue.return_value = {
            "job_id": "endpoint-job",
            "status": "running",
        }
        with TestClient(main.app) as client:
            created = client.post(
                "/jobs", json=job_request("endpoint-job").model_dump(mode="json")
            )
            uploaded = client.post(
                "/jobs/endpoint-job/photos/photo-1/preview",
                json={"image_base64": base64.b64encode(JPEG).decode("ascii")},
            )
            queued = client.post("/jobs/endpoint-job/run")
            status = client.get("/jobs/endpoint-job/status")

        self.assertEqual(created.status_code, 200)
        self.assertEqual(uploaded.status_code, 200)
        self.assertEqual(queued.status_code, 200)
        self.assertEqual(status.json()["status"], "created")
        main.JOB_RUNNER.queue.assert_called_once_with("endpoint-job")

    def test_unknown_job_returns_not_found(self) -> None:
        with TestClient(main.app) as client:
            response = client.get("/jobs/not-present/status")
        self.assertEqual(response.status_code, 404)


class LightroomAsyncContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        plugin = Path(__file__).resolve().parent.parent / "lightroom-plugin" / "LRStockMetadata.lrplugin"
        cls.submit_source = (plugin / "GenerateStockMetadata.lua").read_text(encoding="utf-8")
        cls.check_source = (plugin / "CheckJobStatus.lua").read_text(encoding="utf-8")
        cls.client_source = (plugin / "JobClient.lua").read_text(encoding="utf-8")
        cls.info_source = (plugin / "Info.lua").read_text(encoding="utf-8")

    def test_submit_returns_after_queue_acknowledgement(self) -> None:
        self.assertIn("JobClient.submit", self.submit_source)
        self.assertNotIn("/generate-metadata-json", self.submit_source)
        self.assertNotIn("setRawMetadata", self.submit_source)

    def test_filename_fallback_and_saved_manifest_repair_are_present(self) -> None:
        self.assertIn("local function photoFilename", self.submit_source)
        self.assertIn('getFormattedMeta(photo, "fileName")', self.submit_source)
        self.assertIn("LrPathUtils.leafName(originalPath)", self.submit_source)
        self.assertIn("repairManifestFilenames", self.check_source)
        self.assertIn("serverPhoto.original_filename = filename", self.check_source)

    def test_status_command_supports_resume_and_cached_results(self) -> None:
        self.assertIn('active.status == "submitting"', self.check_source)
        self.assertIn("JobClient.run", self.check_source)
        self.assertIn('active.status == "downloaded"', self.check_source)
        self.assertIn("completed_result.json", self.check_source)

    def test_status_menu_and_timeout_contract_are_present(self) -> None:
        self.assertIn("Check Job Status / Apply Results", self.info_source)
        self.assertIn('"POST",\n        settings.timeoutSeconds', self.client_source)


if __name__ == "__main__":
    unittest.main()
