from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .square_api import SquareClient, SquareError, SquareSettings


def main() -> None:
    load_dotenv(Path.cwd() / ".env")
    config: dict[str, object] = dict(os.environ)
    try:
        settings = SquareSettings.from_mapping(config)
        client = SquareClient(settings)
        locations = client.list_locations()
    except SquareError as exc:
        print(f"Square check failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    print(f"Environment: {settings.environment}")
    print(f"API version: {settings.api_version}")
    print(f"Access token: configured ({len(settings.access_token)} characters)")
    print("Locations:")
    for location in locations:
        location_id = str(location.get("id", "unknown"))
        selected = " [selected]" if location_id == settings.location_id else ""
        print(
            "  - "
            f"{location.get('name', 'Unnamed')} | {location_id} | "
            f"{location.get('status', 'unknown')} | "
            f"{location.get('timezone', 'timezone unknown')}"
            f"{selected}"
        )

    if not settings.location_id and len(
        [location for location in locations if location.get("status", "ACTIVE") == "ACTIVE"]
    ) > 1:
        print("\nSet SQUARE_LOCATION_ID in .env to one of the IDs above.")
