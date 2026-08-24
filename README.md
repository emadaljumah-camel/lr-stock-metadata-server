# Lightroom Stock Metadata Server

This is a new, independent Lightroom Classic integration project. The existing Photo Metadata Tool at `D:\Projects\photo-metadata-tool` is treated only as a read-only reference for behavior.

## Structure

- `server/`: FastAPI app and self-contained hybrid metadata adapter.
- `lightroom-plugin/LRStockMetadata.lrplugin/`: Lightroom Classic plugin.
- `run_server.bat`: starts the FastAPI server using `server.host` and `server.port` from `server/config.yaml`.

GPS and Date Created are extracted by the server from the exported preview JPEG metadata. The Lightroom plugin sends only the preview image and original filename.

## Server Setup

```powershell
cd D:\Projects\lr-stock-metadata-server
python -m venv .venv
.\.venv\Scripts\activate
pip install -r server\requirements.txt
.\run_server.bat
```

Set your local LM Studio model in `server/config.yaml`:

```yaml
models:
  lm_studio_base_url: "http://localhost:1234/v1"
  hybrid_local_model: "your-local-vision-model-id"
  allow_cloud_fallback: false
```

The server exposes:

- `GET /health`
- `POST /generate-metadata`
- `POST /generate-metadata-json`
- `POST /jobs`
- `POST /jobs/{job_id}/photos/{photo_id}/preview`
- `GET /jobs/{job_id}/uploads`
- `POST /jobs/{job_id}/run`
- `GET /jobs/{job_id}/status`
- `GET /jobs/{job_id}/result`

## Durable Background Jobs

`Generate Stock Metadata` now exports the complete eligible selection into a
persistent local job folder, uploads the previews, queues the server job, and
returns. No Lightroom metadata is changed during submission. After the server
acknowledges the queued job, Lightroom and the submitting computer may be
closed or disconnected; the server computer and LM Studio must remain running.

Reconnect later and run `Library > Plug-in Extras > Check Job Status / Apply
Results`. Completed results are downloaded, validated, cached locally, and
then applied in one Lightroom catalog write. A failed catalog write can be
retried from the cached result without contacting the server.

The server checkpoints every completed photo. Following a server restart, a
previously running job becomes `interrupted` and can be resumed without
regenerating completed photos. Create, upload, and run requests are idempotent,
so interrupted submissions are also resumable.

After per-photo metadata generation, the server performs a comparative agency
review over overlapping groups of neighboring photos. It starts each routing
decision from scratch, so it can promote Adobe/Shutterstock selections to Getty
or demote Getty selections while preserving editorial/commercial usage. Review
progress is stored in `agency_review.json`; `result.json` is written only after
the comparative review succeeds.

Only one active Lightroom job is tracked at a time. Apply the active job before
submitting another. Job files are retained for recovery and are not deleted
automatically.

Agency routing values written to Lightroom Instructions:

- `agency:getty_editorial`
- `agency:getty_commercial`
- `agency:adobe_shutterstock`
- `agency:shutterstock_editorial`

## Lightroom Plugin Setup

In Lightroom Classic:

1. Open `File > Plug-in Manager`.
2. Click `Add`.
3. Select `D:\Projects\lr-stock-metadata-server\lightroom-plugin\LRStockMetadata.lrplugin`.
4. Use `Library > Plug-in Extras > Generate Stock Metadata`.

Set the plugin server URL in Lightroom Classic Plug-in Manager, or edit `lightroom-plugin\LRStockMetadata.lrplugin\PluginConfig.lua` for the default.

## Troubleshooting

- Server offline: start `run_server.bat` and verify the configured server URL plus `/health`.
- Local model unavailable: start LM Studio, load a vision model, and set `models.hybrid_local_model` in `server/config.yaml`.
- Cloud fallback unavailable: set `models.allow_cloud_fallback: true` and configure `models.gemini_api_key` or `models.openai_api_key` in `server/config.yaml`.
- Metadata not written: Lightroom may restrict some raw metadata fields; the plugin writes title, caption, keywords, and Instructions.
- HTTP failure: durable preview uploads use JSON with base64 image data; the saved local job can retry only the missing uploads.
- Interrupted upload: use `Check Job Status / Apply Results`; only missing previews are uploaded again.
- Interrupted server job: use the same command and choose `Resume Job`; completed photo checkpoints are preserved.
