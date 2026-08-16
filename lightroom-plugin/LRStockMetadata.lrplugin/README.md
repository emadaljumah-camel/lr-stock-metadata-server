# LR Stock Metadata Lightroom Plugin

Install this folder through Lightroom Classic `File > Plug-in Manager > Add`.

Run it from:

`Library > Plug-in Extras > Generate Stock Metadata`

The command exports persistent JPEG previews, submits a durable background job,
and returns after the server acknowledges the queue. It does not write metadata
during submission. Once queued, the Lightroom computer can disconnect while
the server and LM Studio continue processing.

Reconnect and run:

`Library > Plug-in Extras > Check Job Status / Apply Results`

This command can resume an interrupted submission or server job. Once the job
completes, it downloads and validates the entire result, caches it locally, and
applies title, caption, keywords, and agency Instructions in one catalog write.
No partial server result is applied.

Configure the server URL in `PluginConfig.lua`:

```lua
return {
    serverUrl = "http://100.108.139.62:8008",
}
```

You can also edit the same URL in Lightroom Classic:

`File > Plug-in Manager > LR Stock Metadata > LR Stock Metadata Server`

The same panel configures local durable job storage. The default is `LR Stock
Metadata Jobs` in the user's Documents folder. Only one active job is tracked;
apply it before submitting another. Local previews, manifests, and downloaded
results are retained for recovery.
