# LR Stock Metadata Server API

FastAPI service for generating stock-photo metadata from JPEG previews submitted by the Lightroom Classic plugin.

The server extracts available GPS coordinates and capture dates from preview JPEG metadata. It processes images through a local LM Studio vision model by default, with optional Gemini or OpenAI fallback.

For the end-to-end flow between Lightroom, the plugin, this service, model providers, and local job storage, see the repository's [architecture and data-flow overview](../README.md#architecture-and-data-flow).

## Requirements

- Python 3.10 or newer
- An LM Studio instance with a compatible vision model loaded
- Optional: Gemini or OpenAI credentials if cloud fallback is enabled

## Install

From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r server\requirements.txt
```

## Configure

Edit [`config.yaml`](config.yaml) to match your environment:

```yaml
server:
  host: "localhost"
  port: 8003
  jobs_dir: "_jobs"
  max_job_workers: 1

models:
  lm_studio_base_url: "http://localhost:1234/v1"
  hybrid_local_model: "your-local-vision-model-id"
  allow_cloud_fallback: false
```

`hybrid_local_model` must exactly match the model ID exposed by LM Studio.

Keep `allow_cloud_fallback: false` for local-only processing. To enable fallback, set it to `true`, choose `gemini` or `openai` as `fallback_provider`, and provide the appropriate API key. Leave the YAML key blank and use `GEMINI_API_KEY` or `OPENAI_API_KEY` environment variables to avoid storing credentials in the repository.

Configuration values can be overridden with environment variables. Set `LR_METADATA_CONFIG` to use a different configuration file.

## Run

Start LM Studio and load the configured model, then run this from the repository root:

```powershell
.\run_server.bat
```

`run_server.bat` reads the host and port from `server/config.yaml`. With the committed configuration, verify the service at:

```powershell
curl http://localhost:8003/health
```

For a server on another computer, configure `server.host` to a reachable interface, allow the port through the firewall, and use that computer's LAN address in Lightroom.

## Lightroom Connection URLs

The plugin appends API paths such as `/jobs` and `/health` to its configured server URL.

| Deployment | Example plugin URL | Notes |
| --- | --- | --- |
| Direct local server | `http://localhost:8003` | Matches the committed `config.yaml`. |
| Direct remote server | `http://192.168.1.50:8003` | Requires network and firewall access. |
| Reverse proxy | `http://localhost:8080/metadata` | The proxy must strip `/metadata` and forward the remaining API path to this service. |

Use the URL that matches your own server or proxy configuration. The plugin's committed default is only a starting value; Lightroom Plug-in Manager settings take precedence.

## Processing Pipeline

```text
Create job
  -> validate manifest and create job folder
  -> accept and deduplicate preview uploads
  -> queue background worker
  -> generate metadata for each photo
  -> checkpoint each completed photo
  -> run comparative agency review across related photos
  -> write result.json
  -> Lightroom downloads and applies the result
```

For each photo, the server reads the uploaded preview and available embedded metadata, sends the image to the configured vision model, and produces a title, caption, keywords, editorial/commercial classification, and preliminary agency routing. Each completed photo is checkpointed before the next one is processed.

After the per-photo pass, the server can perform comparative agency review across neighboring photos. This pass evaluates candidate quality and sequence diversity, while preserving the editorial/commercial classification from the initial pass. The final job result is written only after the required processing and review complete.

Uploads are idempotent, job files use atomic replacement, interrupted jobs are recoverable after restart, and malformed agency-review model responses receive bounded retries. These safeguards allow a job to resume without regenerating already completed photos.

## Durable Jobs

The Lightroom plugin uses the durable-job API rather than changing catalog metadata during submission:

1. `POST /jobs` creates or reuses a job.
2. `GET /jobs/{job_id}/uploads` identifies previews already stored.
3. `POST /jobs/{job_id}/photos/{photo_id}/preview` uploads a JSON/base64 preview, or the `/binary` variant uploads JPEG bytes directly.
4. `POST /jobs/{job_id}/run` starts or resumes metadata generation.
5. `GET /jobs/{job_id}/status` reports progress.
6. `GET /jobs/{job_id}/result` returns the completed result.

Jobs are written to the configured `jobs_dir` (default: `_jobs/`). Each completed photo is checkpointed. If the server restarts, running jobs become `interrupted` and can be resumed without regenerating completed work. Job artifacts are retained until manually removed.

Use one Uvicorn process with the filesystem job store. Increase `max_job_workers` only when the model server can handle concurrent generation requests.

## API Reference

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Service health status |
| `POST` | `/generate-metadata` | Generate metadata from a multipart image upload |
| `POST` | `/generate-metadata-json` | Generate metadata from a JSON request |
| `POST` | `/jobs` | Create or reuse a durable job |
| `GET` | `/jobs/{job_id}/uploads` | List stored previews |
| `POST` | `/jobs/{job_id}/photos/{photo_id}/preview` | Upload a base64-encoded preview |
| `POST` | `/jobs/{job_id}/photos/{photo_id}/preview/binary` | Upload a JPEG preview as binary data |
| `POST` | `/jobs/{job_id}/run` | Queue or resume a job |
| `GET` | `/jobs/{job_id}/status` | Retrieve job state and progress |
| `GET` | `/jobs/{job_id}/result` | Retrieve a completed job result |
| `DELETE` | `/jobs/{job_id}` | Delete a job and its stored artifacts |

## Test a Single Image

The test client submits a JPEG to the multipart endpoint. Pass the server URL explicitly so it matches your configuration:

```powershell
python server\test_client.py C:\path\to\photo-with-gps-exif.jpg --url http://localhost:8003/generate-metadata
```

## Output Routing

The server writes one of these values to Lightroom's Instructions field:

```text
agency:getty_editorial
agency:getty_commercial
agency:adobe_shutterstock
agency:shutterstock_editorial
```

The initial metadata pass decides whether an image is editorial or commercial. Agency review can change the destination agency, but never that classification. For editorial images, the final description ends with `City, Country - Month Day, Year`.

## Privacy

Preview JPEGs and job artifacts remain on the server filesystem. With cloud fallback disabled, image analysis stays on the local model server. Enabling fallback may transmit previews and processing input to the selected provider.
