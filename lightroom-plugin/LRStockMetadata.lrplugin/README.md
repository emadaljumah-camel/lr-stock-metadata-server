# LR Stock Metadata Lightroom Plugin

Lightroom Classic plugin for sending photo previews to LR Stock Metadata Server and applying completed stock metadata to the catalog.

## Architecture

Photos remain in Lightroom. The plugin exports JPEG previews, submits them to the FastAPI server as a durable job, and later applies the completed metadata result in one catalog write. The server stores recovery artifacts locally and uses a local vision model by default; cloud fallback is optional.

See the repository's [architecture and data-flow overview](../../README.md#architecture-and-data-flow) for the complete flow between Lightroom, the plugin, the server, model provider, and local job storage.

## Install

1. Open Lightroom Classic.
2. Go to **File > Plug-in Manager**.
3. Select **Add**.
4. Select this `LRStockMetadata.lrplugin` folder.
5. Select **LR Stock Metadata** in the Plug-in Manager list.

## Configure the Server URL

In Lightroom Classic, open:

```text
File > Plug-in Manager > LR Stock Metadata > LR Stock Metadata Server
```

Enter the URL of your metadata server. Do not add a trailing slash.

| Deployment | Example URL |
| --- | --- |
| Direct local FastAPI server | `http://localhost:8003` |
| Direct server on your network | `http://192.168.1.50:8003` |
| Local reverse proxy | `http://localhost:8080/metadata` |

The server must receive paths appended by the plugin, including `/jobs`, `/health`, and `/generate-metadata`. A reverse proxy using a base path such as `/metadata` must strip that prefix before forwarding requests to the FastAPI service.

The Plug-in Manager value is stored in Lightroom's local plugin preferences and overrides the default in [`PluginConfig.lua`](PluginConfig.lua). Changing the committed default does not replace an existing saved preference.

`PluginConfig.lua` provides the public safe default:

```lua
return {
    serverUrl = "http://localhost:8008",
}
```

For a direct connection, update the Plug-in Manager URL to match the host and port configured in `server/config.yaml`. The committed server configuration uses `http://localhost:8003`.

## Submit Metadata Generation

1. Select eligible photos in Lightroom's Library module.
2. Choose **Library > Plug-in Extras > Generate Stock Metadata**.
3. Wait for Lightroom to confirm that the job has been queued.

The plugin exports persistent JPEG previews, uploads them to the server, and creates a durable job. It does not modify catalog metadata during submission.

After the server acknowledges the job, Lightroom and the submitting computer can be closed or disconnected. The metadata server and its configured model service must remain available while processing continues.

## Apply Results or Resume Work

Reconnect to the server and choose:

```text
Library > Plug-in Extras > Check Job Status / Apply Results
```

This command can:

- Resume an interrupted preview upload.
- Resume an interrupted server job.
- Download and validate completed metadata.
- Apply title, caption, keywords, and agency Instructions in one catalog write.

The plugin caches the full downloaded result before modifying the catalog. If a catalog write fails, retry applying the cached result without rerunning metadata generation. No partial server result is applied.

## Local Job Storage

The Plug-in Manager also configures local durable job storage. By default, it is the `LR Stock Metadata Jobs` folder in your Documents directory.

Local previews, manifests, active-job state, and downloaded results are retained for recovery. Only one active Lightroom job is tracked at a time; apply or resolve it before submitting another one.

## Troubleshooting

| Problem | Suggested fix |
| --- | --- |
| Cannot connect to the server | Confirm the URL in Plug-in Manager, start the server, and open its `/health` endpoint in a browser. |
| Connection works locally but not from another computer | Use the server computer's LAN address and check its host binding and firewall. |
| Job is interrupted | Run **Check Job Status / Apply Results** and choose to resume when prompted. |
| Metadata does not appear | Lightroom may restrict source metadata fields. The plugin writes title, caption, keywords, and Instructions. |
| Changed the default URL but Lightroom still uses an old one | Edit the Plug-in Manager value; it overrides `PluginConfig.lua`. |
