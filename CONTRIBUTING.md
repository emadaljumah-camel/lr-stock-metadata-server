# Contributing

Read the [setup guide](README.md#setup) and
[architecture](docs/ARCHITECTURE.md) before changing behavior. Discuss substantial
changes in an issue first. Keep patches focused and preserve unrelated changes.

## Local verification

Use the repository virtual environment and run from its root:

```powershell
.\.venv\Scripts\python.exe -m unittest server.test_async_jobs server.test_agency_reviewer -q
.\.venv\Scripts\python.exe -m pip check
git diff --check
```

The automated suites use mocked providers and synthetic fixtures. They cover
job storage/API/runner recovery and agency review. The separate
[test_client.py](server/test_client.py) is a manual live-server utility, not an
offline unit suite. Use its explicit URL option to avoid the 8008/8003 default
port mismatch. Live requests may send GPS to external services and invoke
configured model providers.

For plug-in changes, use a backed-up disposable catalog and a GPS-enabled test
photo. Verify submission acknowledgment, result recovery, actual metadata
changes, keyword addition, and failure/partial-write behavior. Record Lightroom,
Python, and model versions when reporting manual results. Unit tests do not
prove Lightroom application behavior or metadata accuracy.

## Pull requests

- Describe the concrete problem, resulting behavior, and verification.
- Add focused tests for behavioral changes; identify untested manual paths.
- Preserve client IDs, upload hashes, recovery files, and result validation.
- Update setup/API/privacy documentation when those contracts change.
- Keep dependency updates separate from unrelated changes.
- Use synthetic data; do not commit keys, local configurations, catalogs,
  generated previews/results, GPS, private paths, or model weights.
- Check both current files and history for secrets before sharing a branch.

Original contributions are submitted under the [MIT license](LICENSE). Include
appropriate attribution and compatible terms for any third-party material.
Report vulnerabilities using [SECURITY.md](SECURITY.md), not a public exploit
report.
