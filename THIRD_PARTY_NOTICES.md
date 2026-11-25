# Third-party requirements and notices

The [MIT license](LICENSE) applies to original project code and documentation.
It does not relicense external libraries, model weights, services, photographs,
Adobe software, or external location/reference data.

## Python dependencies

[server/requirements.txt](server/requirements.txt) lists FastAPI, Uvicorn,
python-multipart, Pillow, OpenAI's Python client, google-genai, Pydantic,
Requests, and PyYAML. Their transitive dependencies retain their own licenses
and notices too. Requirements are version ranges, not a complete locked
environment. Review license metadata and bundled notices for the actual
versions you install or redistribute.

## Lightroom and models

Adobe Lightroom Classic is proprietary software and must be obtained and
licensed separately. Adobe and Lightroom are Adobe trademarks. This project is
independent and is not endorsed by Adobe or by the named stock agencies.

LM Studio and local model weights have separate licenses and hardware/runtime
requirements. Example model IDs are configuration examples, not a grant to
redistribute weights or a guarantee of image capability. Gemini/OpenAI usage is
subject to provider terms and can incur charges. No model weights or Lightroom
SDK binaries are bundled.

## Location and reference services

The default configuration points to Nominatim/OpenStreetMap and Wikipedia.
These services and underlying datasets have their own usage, attribution, and
redistribution terms. Review those terms for your deployment and any output you
redistribute; the project's MIT license does not grant rights to their data.
The configured User-Agent is not evidence of permission for unlimited service
use. GPS can be transmitted even with cloud AI fallback disabled.

## Generated content and provenance

Generated metadata and routing do not establish copyright ownership, release
clearance, factual accuracy, or stock-agency acceptance. The maintainer remains
responsible for rights to contributed/reused code and assets. This notice does
not certify third-party provenance or override an existing third-party license.
