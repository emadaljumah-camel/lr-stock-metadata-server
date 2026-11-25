# Security and privacy

## Deployment boundary

The FastAPI service has no client authentication, per-user authorization, or
built-in TLS. Anyone able to reach it can invoke generation and job operations,
including deletion of eligible non-running jobs. Job IDs are not credentials.
Provider API keys authenticate outbound model calls, not incoming clients.

Keep localhost bindings for single-computer use. On a trusted private network,
limit firewall access to the Lightroom computer. A private LAN is not itself
authentication or encryption. Do not expose FastAPI or LM Studio directly to
the public internet. Broader deployments require separately configured access
control, authentication, and encrypted transport.

## Data handling

- Previews preserve embedded metadata by design, including GPS required by the
  current generation path. They can also include capture dates and other EXIF.
- Original filenames, catalog paths, photo identifiers, camera/location context,
  generated metadata, and model diagnostics can be persisted in jobs or local
  recovery files. Review them before sharing.
- Local inference is local only when the configured endpoint is local. Remote
  endpoints receive image/context input. Enabled cloud fallback can send
  previews and associated processing context to Gemini or OpenAI and incur
  charges; provider policies apply.
- Nominatim reverse lookup and Wikipedia geosearch can receive GPS coordinates
  even when cloud AI fallback is disabled. That flag is not an offline-mode
  switch. Empty configuration values can fall through to built-in service URLs;
  do not assume blanking a URL disables network access.
- Durable previews, results, review checkpoints, raw model-failure diagnostics,
  synchronous uploads, and resized inference images can remain on disk. No
  automatic retention period is implemented. Cancelling client progress does
  not erase them or stop an already-queued server job.
- There is no application-level storage encryption. Protect catalogs, job data,
  private configuration, and backups using OS permissions and encryption.

The server defaults to `_jobs/` and `_uploads/` under the working directory.
The plug-in defaults to Documents/LR Stock Metadata Jobs. Configured locations
can differ. Retain recovery files until work is resolved; deletion is not a
catalog rollback. Stop the server before manual maintenance or backups of job
folders to avoid copying inconsistent in-progress state.

## Credentials and repository hygiene

Keep cloud API-key fields blank in tracked YAML. Use environment variables, or
a private YAML file outside the checkout selected with `LR_METADATA_CONFIG`.
There is no automatic `.env` loader in this project. Nonempty YAML takes
precedence over supported environment variables, including keys.

The tracked config contains an `lm-studio` local-server key placeholder; it is
not protection for this API. Never replace it with a real secret in committed
configuration. Ignore rules cover common runtime folders, but do not erase old
commits or protect arbitrary private files placed elsewhere in the repository.

Do not attach photos, catalogs, GPS-bearing previews, unredacted errors, or raw
job directories to public issues. Rotate exposed credentials; removing them
only from the current file does not remove Git history.

## Known verification limits

Passing tests and a secret scan do not establish security clearance. Dependency
requirements were intentionally left unchanged during documentation preparation.
No comprehensive vulnerability assessment or dependency upgrade was performed
for this repository as part of that work. Inspect the installed dependency set
before processing untrusted images or deploying more broadly.

## Reporting a vulnerability

Use GitHub's private **Report a vulnerability** option if enabled. If it is not
available, open an issue requesting a private contact channel without exploit
details, credentials, GPS, or personal data. Share a sanitized reproduction,
affected commit/version, and impact over the agreed private channel.

There is no guaranteed response time. Fixes target the current main branch;
older snapshots do not have a separate maintenance commitment.
