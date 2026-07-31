from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Mapping

from .database import SyncInfo, replace_orders_for_date
from .sample_data import build_sample_orders
from .square_api import SquareClient, SquareConfigurationError, SquareSettings
from .square_orders import (
    ClassificationRules,
    SquareOrderPull,
    pull_square_orders_for_date,
)


@dataclass(frozen=True, slots=True)
class SyncResult:
    info: SyncInfo
    warnings: tuple[str, ...] = ()
    candidates_scanned: int | None = None
    location_name: str | None = None


def configured_order_source(config: Mapping[str, object]) -> str:
    requested = str(config.get("ORDER_SOURCE", "auto")).strip().lower()
    if requested not in {"auto", "sample", "square"}:
        raise SquareConfigurationError(
            "ORDER_SOURCE must be 'auto', 'sample', or 'square'."
        )
    if requested == "auto":
        return "square" if str(config.get("SQUARE_ACCESS_TOKEN", "")).strip() else "sample"
    return requested


def sync_orders_for_date(
    path: Path,
    service_date: date,
    config: Mapping[str, object],
    *,
    square_client: SquareClient | None = None,
) -> SyncResult:
    source = configured_order_source(config)
    if source == "sample":
        return SyncResult(
            info=replace_orders_for_date(
                path,
                service_date,
                build_sample_orders(service_date),
                source="sample",
            )
        )

    settings = SquareSettings.from_mapping(config)
    client = square_client or SquareClient(settings)
    location = client.resolve_location()
    location_id = str(location.get("id", "")).strip()
    if not location_id:
        raise SquareConfigurationError("The selected Square location has no ID.")

    pull: SquareOrderPull = pull_square_orders_for_date(
        client,
        service_date=service_date,
        timezone_name=str(config.get("SERVICE_TIMEZONE", "America/New_York")),
        location_id=location_id,
        lookback_days=settings.order_lookback_days,
        rules=ClassificationRules.from_mapping(config),
    )
    info = replace_orders_for_date(
        path,
        service_date,
        pull.orders,
        source="square",
    )
    return SyncResult(
        info=info,
        warnings=pull.warnings,
        candidates_scanned=pull.candidates_scanned,
        location_name=(str(location.get("name")) if location.get("name") else None),
    )


def sync_sample_orders(path: Path, service_date: date) -> SyncInfo:
    """Compatibility helper retained for tests and sample mode."""
    return replace_orders_for_date(
        path,
        service_date,
        build_sample_orders(service_date),
        source="sample",
    )
