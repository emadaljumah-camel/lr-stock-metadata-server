# Lightroom plug-in guide

This plug-in submits JPEG previews to LR Stock Metadata Server and applies the
completed title, caption, keywords, and Instructions to Lightroom's catalog.
It does not change Develop settings or upload stock-agency submissions.

Follow the [main installation guide](../../README.md#setup) first.

## Install and configure

1. Open **File > Plug-in Manager > Add** and select this complete folder.
2. Select **05 LR Stock Metadata**.
3. In **LR Stock Metadata Server**, set **Server URL** to match the backend.
   The committed server config uses `http://localhost:8003`; the plug-in's
   initial default is `http://localhost:8008`.
4. Set **Local job storage**, defaulting to Documents/LR Stock Metadata Jobs.

Settings are local Lightroom preferences. Saved values override
[PluginConfig.lua](PluginConfig.lua). The plug-in appends API paths itself.
Do not enter `/jobs` or `/health` in Server URL.

For a proxy base path, the proxy must strip that prefix before forwarding API
requests. For another computer, use its reachable private server address and
restricted firewall rules. See [server URLs](../../server/README.md#api-reference)
and [security limits](../../SECURITY.md).

## Generate Stock Metadata

Select test photos, then run
**Library > Plug-in Extras > Generate Stock Metadata**.

- Choose **Overwrite** to include photos even when metadata exists.
- Choose **Skip Existing** to exclude photos with any title, caption, or keyword.
  Existing Instructions alone do not trigger this skip.
- JPEG previews preserve metadata and use a 1200-by-1200 export bounding box.
- The current server path needs readable GPS in each preview, not merely
  coordinates in an API context object.
- Wait for a successful queued acknowledgment before disconnecting.
- Keep the backend and model running while Lightroom is disconnected.

Submission saves a local job and uploads binary previews. It does not change
catalog metadata. Only one active client job is tracked.

## Check Job Status / Apply Results

Run **Library > Plug-in Extras > Check Job Status / Apply Results**.

| State | Behavior |
|---|---|
| Submitting | Recover saved manifest and missing uploads |
| Running | Show progress; check again later |
| Failed/interrupted | Offer Resume Job or More Options for confirmed deletion |
| Completed | Download, validate, cache, and apply results |
| Downloaded after application failure | Retry from cached results |
| Applied | Report that the job has already been applied |

**Checking a completed job can immediately apply metadata.** There is no
separate per-photo approval screen. The catalog must match the submitting
catalog, and original photos must resolve. Result job/photo membership and
metadata types are checked before application.

## What changes in Lightroom

Title, caption, and Instructions are overwritten. Generated keywords are
added; existing keywords are not removed. The submission prompt mentions
overwriting keywords, but the actual application code adds them.

Agency routing is written as an `agency:...` Instructions tag. Location,
capture date, release fields, and editorial/commercial flags are not written
as separate dedicated Lightroom metadata fields.

Application uses write-access batches of **25 photos**. A later error can
leave earlier writes in place. A job-level applied flag is saved only after
all batches succeed; there is no per-photo applied checkpoint or pre-edit
metadata snapshot. A retry can reapply cached results and overwrite intervening
manual changes. Do not treat it as guaranteed exactly-once application.

## Recovery, stopping, and undo

Cancelling a client export/upload dialog does not cancel server work already
queued. There is no server running-job cancellation endpoint. Running jobs
cannot be deleted by the API. Failed/interrupted jobs offer explicit deletion,
which removes server artifacts and local previews; that is irreversible.

A failed application retains downloaded results for retry but does not
automatically restore old metadata. Deleting jobs does not undo catalog
changes. Use appropriate Lightroom Undo tools or a catalog backup and verify
metadata restoration; Develop History alone may not recover catalog fields.

Keep job folders until the job is resolved. Back them up before updates and
avoid mixing plug-in/server versions during active work. See
[update instructions](../../README.md#updating-an-installation) and
[architecture and safeguards](../../docs/ARCHITECTURE.md).

## Privacy

Local manifests can contain catalog paths, original filenames, IDs, and
capture/location context. Previews can include embedded GPS. The server can
contact location services even with cloud AI fallback disabled.

Do not attach catalogs, previews, raw job results, or unreviewed diagnostics
to public issues. See [SECURITY.md](../../SECURITY.md).
