from __future__ import annotations

import argparse
import json
from pathlib import Path

import requests


def main() -> None:
    parser = argparse.ArgumentParser(description="Send one JPEG to the Lightroom stock metadata server.")
    parser.add_argument("image", help="Path to a JPEG image")
    parser.add_argument("--url", default="http://127.0.0.1:8008/generate-metadata")
    parser.add_argument("--capture-time", default="")
    args = parser.parse_args()

    image_path = Path(args.image)
    with image_path.open("rb") as handle:
        response = requests.post(
            args.url,
            files={"image": (image_path.name, handle, "image/jpeg")},
            data={
                "original_filename": image_path.name,
                "capture_time": args.capture_time,
            },
            timeout=180,
        )
    response.raise_for_status()
    print(json.dumps(response.json(), indent=2))


if __name__ == "__main__":
    main()
