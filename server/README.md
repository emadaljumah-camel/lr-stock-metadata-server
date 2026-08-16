# Lightroom Stock Metadata FastAPI Server

This server is an independent implementation of the Photo Metadata Tool hybrid metadata path. It does not import, run, or modify `D:\Projects\photo-metadata-tool`.

The Lightroom plugin sends only the exported preview image and original filename. GPS and Date Created are extracted from the preview JPEG metadata on the server.

## Install

```powershell
cd D:\Projects\lr-stock-metadata-server
python -m venv .venv
.\.venv\Scripts\activate
pip install -r server\requirements.txt
```

## Configure Models

Edit `server/config.yaml`:

```yaml
models:
  lm_studio_base_url: "http://localhost:1234/v1"
  lm_studio_api_key: "lm-studio"
  hybrid_local_model: "your-local-vision-model-id"

  allow_cloud_fallback: false
  fallback_provider: "gemini" # gemini or openai
  gemini_model: "gemini-3-flash-preview"
  openai_model: "gpt-5.4-mini"
  gemini_api_key: ""
  openai_api_key: ""
```

`hybrid_local_model` must match the model id/name exposed by LM Studio.

`allow_cloud_fallback` controls whether the server may call Gemini/OpenAI automatically when the local hybrid path fails. Keep it `false` to require local-only processing.

Environment variables still work as fallback if a YAML value is blank. To use a different config file, set `LR_METADATA_CONFIG` to its path before starting the server.

## Run

```powershell
.\run_server.bat
```

`run_server.bat` reads `server.host` and `server.port` from `server/config.yaml`.

Durable job storage and worker concurrency are configured in the same file:

```yaml
server:
  jobs_dir: "_jobs"
  max_job_workers: 1
```

Use one Uvicorn process with the filesystem job store. Increase
`max_job_workers` only when the model server can safely handle concurrent
generation requests.

Health check:

```powershell
curl http://127.0.0.1:8008/health
```

## Asynchronous Job API

The Lightroom client uses this sequence:

1. `POST /jobs` creates or reuses a job with the same client id and manifest.
2. `GET /jobs/{job_id}/uploads` discovers previews already acknowledged.
3. `POST /jobs/{job_id}/photos/{photo_id}/preview` uploads missing JPEGs.
4. `POST /jobs/{job_id}/run` queues or resumes background generation.
5. `GET /jobs/{job_id}/status` reports stage and photo progress.
6. `GET /jobs/{job_id}/result` returns the complete result after completion.

Each server job is stored under `_jobs/<job-id>/`:

```text
job.json
status.json
uploads.json
checkpoint.json
previews/
result.json
```

Writes use temporary files and atomic replacement. Every completed photo is
checkpointed before processing continues. Startup marks jobs left in `running`
as `interrupted`; calling the run endpoint again resumes from the checkpoint.
Job data is intentionally retained until manually removed.

## Test Client

```powershell
python server\test_client.py C:\path\photo-with-gps-exif.jpg
```

## Agency Instructions

The server writes one of these values for Lightroom Instructions:

- `agency:getty_editorial`
- `agency:getty_commercial`
- `agency:adobe_shutterstock`
- `agency:shutterstock_editorial`

For editorial images, the final description ends with:

```text
City, Country - Month Day, Year
```
