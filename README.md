# LR Stock Metadata Server

Generate stock-ready photo metadata in Lightroom Classic using a local vision model, with optional cloud fallback.

The project includes:

- A FastAPI server that analyzes exported JPEG previews.
- A Lightroom Classic plugin for submitting photos and applying results.
- Durable background jobs that can resume after Lightroom, the client computer, or the server restarts.
- Optional comparative agency routing for Getty, Adobe Stock, and Shutterstock.

## How It Works

1. In Lightroom Classic, select photos and run **Generate Stock Metadata**.
2. The plugin exports JPEG previews and submits them to the server.
3. The server generates metadata and performs optional agency review.
4. Later, run **Check Job Status / Apply Results** in Lightroom to download and apply completed metadata.

The server extracts GPS coordinates and capture date from preview JPEG metadata. The plugin sends the preview image and original filename.

## Features

- Title, caption, keyword, and Lightroom Instructions generation.
- Editorial or commercial classification.
- Agency routing:
  - Getty Editorial
  - Getty Commercial
  - Adobe Stock / Shutterstock
  - Shutterstock Editorial
- Persistent job checkpoints and retry-safe uploads.
- Resume interrupted uploads and metadata generation.
- Local LM Studio vision-model support.
- Optional Gemini or OpenAI fallback.
- Comparative review to reduce duplicate Getty selections within a sequence.

## Requirements

- Windows with Lightroom Classic.
- Python 3.10 or newer.
- [LM Studio](https://lmstudio.ai/) running a vision-capable model compatible with its OpenAI-style local API.
- A Lightroom Classic catalog containing the photos to process.

Cloud fallback is optional. If enabled, you also need an API key for Gemini or OpenAI.

## Repository Layout

```text
server/                              FastAPI server and metadata pipeline
lightroom-plugin/
  LRStockMetadata.lrplugin/          Lightroom Classic plugin
run_server.bat                       Windows server launcher
```

## Server Setup

Clone the repository and create a virtual environment:

```powershell
git clone https://github.com/YOUR-USERNAME/lr-stock-metadata-server.git
cd lr-stock-metadata-server

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r server\requirements.txt
```

Configure the model server in [`server/config.yaml`](server/config.yaml):

```yaml
server:
  host: "localhost"
  port: 8003

models:
  lm_studio_base_url: "http://localhost:1234/v1"
  hybrid_local_model: "your-local-vision-model-id"
  allow_cloud_fallback: false
```

Set `hybrid_local_model` to the exact model ID displayed by LM Studio.

Start LM Studio, load the selected vision model, then start the API server:

```powershell
.\run_server.bat
```

Confirm it is available:

```powershell
curl http://localhost:8003/health
```

> The server URL configured in Lightroom must use the same host and port as `server.host` and `server.port` in `server/config.yaml`.

## Optional Cloud Fallback

Cloud fallback is disabled by default.

To enable it, update `server/config.yaml`:

```yaml
models:
  allow_cloud_fallback: true
  fallback_provider: "gemini" # or "openai"
  gemini_api_key: ""
  openai_api_key: ""
```

For safer credential management, leave API-key fields blank and provide keys through environment variables instead:

```powershell
$env:GEMINI_API_KEY = "your-key"
# or
$env:OPENAI_API_KEY = "your-key"
```

Never commit API keys to GitHub.

## Install the Lightroom Plugin

1. Open Lightroom Classic.
2. Go to **File > Plug-in Manager**.
3. Select **Add**.
4. Choose the `lightroom-plugin/LRStockMetadata.lrplugin` folder from this repository.
5. Select the installed plugin and set its server URL, for example:

   ```text
   http://localhost:8003
   ```

6. Open **Library > Plug-in Extras > Generate Stock Metadata**.

If Lightroom and the server run on different computers, use the server computer's reachable LAN IP address instead of `localhost`.

## Using the Plugin

### Submit a Job

1. Select eligible photos in Lightroom's Library module.
2. Choose **Library > Plug-in Extras > Generate Stock Metadata**.
3. Wait for the job to be acknowledged.

The plugin exports and uploads previews, then returns without modifying catalog metadata.

### Apply Completed Results

Choose:

```text
Library > Plug-in Extras > Check Job Status / Apply Results
```

When complete, the plugin downloads, validates, caches, and applies the result in one Lightroom catalog write.

Metadata written to Lightroom includes:

- Title
- Caption
- Keywords
- Instructions, including the agency destination

## Durable Jobs and Recovery

Jobs are designed to survive interruptions.

- Preview uploads are idempotent; only missing previews are uploaded again.
- Each completed photo is checkpointed on the server.
- A server restart marks active work as interrupted; rerunning the job resumes from its checkpoint.
- Lightroom can be closed after a job is successfully queued.
- Downloaded results are cached locally before catalog metadata is changed.
- If applying metadata to the catalog fails, retry it without regenerating server results.

The server stores job data in the configured `jobs_dir` (default: `_jobs/`). These files are retained for recovery and are not automatically deleted.

Only one active Lightroom job is tracked at a time. Apply or resolve the active job before submitting another one.

## Agency Routing

The server assigns one of these values to Lightroom's Instructions field:

```text
agency:getty_editorial
agency:getty_commercial
agency:adobe_shutterstock
agency:shutterstock_editorial
```

The initial metadata pass determines whether an image is editorial or commercial. The agency review may change the destination agency, but does not change that classification.

Getty selection uses configured quality and merit thresholds, sequence-level duplicate review, and a preference for visually differentiated images. The configured Getty ratio is a planning target, not a quota.

## API Overview

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Confirm server availability |
| `POST` | `/generate-metadata` | Generate metadata for a single request |
| `POST` | `/generate-metadata-json` | Generate metadata as JSON |
| `POST` | `/jobs` | Create or reuse a durable job |
| `GET` | `/jobs/{job_id}/uploads` | List acknowledged previews |
| `POST` | `/jobs/{job_id}/photos/{photo_id}/preview` | Upload a preview |
| `POST` | `/jobs/{job_id}/run` | Start or resume processing |
| `GET` | `/jobs/{job_id}/status` | Retrieve progress and job state |
| `GET` | `/jobs/{job_id}/result` | Retrieve completed results |

For the upload and job-request formats, refer to the Lightroom plugin implementation and server models.

## Troubleshooting

| Problem | Suggested fix |
| --- | --- |
| Server is offline | Start `run_server.bat` and confirm the plugin URL matches `server/config.yaml`. |
| Local model is unavailable | Start LM Studio, load a compatible vision model, and verify its model ID in the configuration. |
| Plugin cannot connect from another computer | Use the server's LAN IP address, verify firewall rules, and ensure the server binds to a reachable host. |
| Cloud fallback fails | Confirm it is enabled, the selected provider is correct, and its API key is available. |
| Upload was interrupted | Run **Check Job Status / Apply Results**; only missing previews should upload again. |
| Server job was interrupted | Use **Check Job Status / Apply Results** and choose to resume when prompted. |
| Metadata was not written | Lightroom may restrict some source metadata fields. This plugin writes title, caption, keywords, and Instructions. |

## Privacy and Data Handling

Preview JPEGs and job artifacts are stored locally in the configured server job directory. When cloud fallback is disabled, metadata generation remains on your local model server.

Enabling cloud fallback may send preview images and associated processing input to the selected cloud provider. Review that provider's policies before enabling it.

## License

Add a license file before publishing the repository, then link it here.

## Contributing

Issues and pull requests are welcome. Please avoid including API keys, personal IP addresses, Lightroom catalogs, original photos, or generated job files in contributions.
