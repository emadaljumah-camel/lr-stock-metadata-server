# Server configuration and API

Start with the [root setup guide](../README.md#setup). This document explains
the implementation-facing configuration and endpoints.

## Configuration

[config.py](config.py) reads `LR_METADATA_CONFIG` or the default
[config.yaml](config.yaml) at import/startup. Use a machine-private file outside
the repository for deployment values. There is no automatic `.env` loader here;
set environment variables in the launching process.

**Precedence: nonempty YAML value, then supported environment variable, then
built-in default.** Boolean false and numeric values in YAML are real settings,
not invitations to fall back to the environment. Restart after edits.

| Setting | Committed configuration / behavior |
|---|---|
| `server.host` / `server.port` | localhost / 8003; built-in fallback without config is 127.0.0.1 / 8008 |
| `server.jobs_dir` | `_jobs`; relative storage paths resolve from process working directory |
| `server.upload_dir` | `_uploads`; used by synchronous upload endpoints |
| `server.max_job_workers` | 1; use one Uvicorn process with this filesystem store |
| `models.lm_studio_base_url` | `http://localhost:1234/v1` |
| `models.hybrid_local_model` | Exact loaded image-capable model ID; committed value is an example, not a compatibility guarantee |
| `models.allow_cloud_fallback` | false |
| `models.fallback_provider` | gemini or openai |
| Provider API keys | Keep YAML cloud keys blank and use `GEMINI_API_KEY` or `OPENAI_API_KEY` |
| `metadata.agency_review` | Comparative sequence-review settings |
| `geocoding` | Nominatim reverse lookup and Wikipedia nearby lookup |

The launcher reads selected host/port and starts `server.main:app` from the
repository root. It expects the repository `.venv`. For a different server
command, pass the desired bind settings explicitly and preserve the selected
configuration environment.

## Processing and routing

1. Read the preview and extract GPS/capture date from embedded metadata.
   Missing readable GPS fails the current hybrid path before inference.
2. Request metadata through the configured local model. Configured cloud
   fallback is attempted when the local path fails or is unavailable.
3. Validate/normalize generated metadata and determine editorial/commercial
   classification and preliminary destination.
4. Persist each completed photo result in a job checkpoint.
5. If enabled, review related photos comparatively for technical merit,
   Getty suitability, and duplicate/sequence diversity.
6. Save final ordered results after required review finishes.

The review preserves the initial editorial/commercial classification while
adjusting agency destination. Thresholds, overlapping review windows, retries,
and commercial recovery are in [agency_reviewer.py](agency_reviewer.py).
Getty ratio targets do not guarantee a fixed count or agency acceptance.

Nominatim and Wikipedia lookups can transmit GPS independently of cloud AI
fallback. The pipeline is not offline merely because fallback is disabled.

## API reference

Declared route paths in [main.py](main.py):

| Method | Route | Purpose |
|---|---|---|
| GET | `/health` | `{"status":"ok"}` process availability |
| POST | `/generate-metadata` | Synchronous multipart image + original_filename + optional context fields |
| POST | `/generate-metadata-json` | Synchronous Base64 image, original_filename, and context object |
| POST | `/jobs` | Create/reuse durable manifest with unique photo IDs |
| GET | `/jobs/{job_id}/uploads` | List acknowledged photo previews |
| POST | `/jobs/{job_id}/photos/{photo_id}/preview` | JSON `image_base64` preview |
| POST | `/jobs/{job_id}/photos/{photo_id}/preview/binary` | JPEG bytes with `Content-Type: image/jpeg` |
| POST | `/jobs/{job_id}/run` | Queue/resume complete uploads |
| GET | `/jobs/{job_id}/status` | Progress and state |
| GET | `/jobs/{job_id}/result` | Completed result, or current status if incomplete |
| DELETE | `/jobs/{job_id}` | Delete stored job artifacts; running jobs are refused |

There is no running-job cancellation route. Deletion is not rollback.
Request/response structures are in [models.py](models.py) and
[job_models.py](job_models.py); do not invent extra required fields from prose.

The app declares `root_path="/metadata"` for proxy deployment metadata. A direct
local client uses `http://localhost:8003` and the routes above. A proxy base URL
such as `http://localhost:8080/metadata` must strip its prefix when forwarding
requests to this service. Do not add the prefix to direct localhost calls.

## Jobs, persistence, and failure handling

[JobStore](job_store.py) persists manifests, uploaded JPEGs, upload hashes,
photo checkpoints, review checkpoints, model-failure diagnostics, status, and
final results. Files use temporary writes and atomic replacement individually.
[JobRunner](job_runner.py) tracks active execution in process memory and resumes
from persisted checkpoints after explicit requeueing. Startup marks previously
running jobs interrupted.

A reused client ID with identical inputs reuses the job; changed input
conflicts. Upload hashes detect conflicting bytes. Completed jobs return their
existing completion state instead of regenerating on another run request.

Use one Uvicorn process; this is not a distributed queue. A filesystem job ID
is not an authentication secret. Failed operations can leave retained inputs
and diagnostic data. Synchronous endpoint uploads are also retained rather
than automatically unlinked after the response.

## Tests and manual request

From the repository root:

```powershell
.\.venv\Scripts\python.exe -m unittest server.test_async_jobs server.test_agency_reviewer -q
```

A separate manual live-server check:

```powershell
.\.venv\Scripts\python.exe server\test_client.py C:\path\photo-with-gps-exif.jpg --url http://localhost:8003/generate-metadata
```

The utility's default port is 8008, so pass the URL explicitly. It sends the
image to a real server and can invoke real providers and location lookups.

See [architecture](../docs/ARCHITECTURE.md) and [security](../SECURITY.md).
