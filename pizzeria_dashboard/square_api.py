from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


DEFAULT_SQUARE_API_VERSION = "2026-07-15"


class SquareError(RuntimeError):
    """Base exception for Square integration failures."""


class SquareConfigurationError(SquareError):
    """Raised when required Square settings are missing or invalid."""


class SquareAPIError(SquareError):
    """Raised when Square returns an error response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        errors: Sequence[Mapping[str, object]] = (),
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.errors = tuple(errors)


Requester = Callable[
    [str, str, Mapping[str, str], Mapping[str, object] | None, float],
    Mapping[str, object],
]


@dataclass(frozen=True, slots=True)
class SquareSettings:
    access_token: str
    location_id: str | None
    environment: str = "production"
    api_version: str = DEFAULT_SQUARE_API_VERSION
    timeout_seconds: float = 20.0
    order_lookback_days: int = 60

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> "SquareSettings":
        environment = str(values.get("SQUARE_ENVIRONMENT", "production")).lower()
        if environment not in {"production", "sandbox"}:
            raise SquareConfigurationError(
                "SQUARE_ENVIRONMENT must be 'production' or 'sandbox'."
            )

        try:
            timeout_seconds = float(values.get("SQUARE_TIMEOUT_SECONDS", 20))
            lookback_days = int(values.get("SQUARE_ORDER_LOOKBACK_DAYS", 60))
        except (TypeError, ValueError) as exc:
            raise SquareConfigurationError(
                "Square timeout and lookback settings must be numeric."
            ) from exc

        return cls(
            access_token=str(values.get("SQUARE_ACCESS_TOKEN", "")).strip(),
            location_id=(
                str(values.get("SQUARE_LOCATION_ID", "")).strip() or None
            ),
            environment=environment,
            api_version=str(
                values.get("SQUARE_API_VERSION", DEFAULT_SQUARE_API_VERSION)
            ).strip()
            or DEFAULT_SQUARE_API_VERSION,
            timeout_seconds=max(timeout_seconds, 1.0),
            order_lookback_days=max(lookback_days, 1),
        )

    @property
    def base_url(self) -> str:
        if self.environment == "sandbox":
            return "https://connect.squareupsandbox.com"
        return "https://connect.squareup.com"

    def require_access_token(self) -> None:
        if not self.access_token:
            raise SquareConfigurationError(
                "SQUARE_ACCESS_TOKEN is not configured. Copy .env.example to .env "
                "and add a Square personal access token."
            )


class SquareClient:
    def __init__(
        self,
        settings: SquareSettings,
        *,
        requester: Requester | None = None,
    ) -> None:
        self.settings = settings
        self.settings.require_access_token()
        self._requester = requester or self._stdlib_request

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Square-Version": self.settings.api_version,
            "User-Agent": "pizzeria-mari-dashboard/0.3.12",
        }

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        return self._requester(
            method,
            f"{self.settings.base_url}{path}",
            self._headers(),
            payload,
            self.settings.timeout_seconds,
        )

    @staticmethod
    def _stdlib_request(
        method: str,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object] | None,
        timeout: float,
    ) -> Mapping[str, object]:
        body = None
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = Request(url, data=body, headers=dict(headers), method=method)

        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            raw = exc.read()
            decoded = _decode_json(raw)
            errors = _extract_errors(decoded)
            message = _format_square_errors(errors) or f"Square returned HTTP {exc.code}."
            raise SquareAPIError(
                message,
                status_code=exc.code,
                errors=errors,
            ) from exc
        except (URLError, TimeoutError, socket.timeout) as exc:
            reason = getattr(exc, "reason", exc)
            raise SquareAPIError(f"Unable to reach Square: {reason}") from exc

        decoded = _decode_json(raw)
        errors = _extract_errors(decoded)
        if errors:
            raise SquareAPIError(_format_square_errors(errors), errors=errors)
        return decoded

    def list_locations(self) -> tuple[Mapping[str, object], ...]:
        response = self._request("GET", "/v2/locations")
        raw_locations = response.get("locations", [])
        if not isinstance(raw_locations, list):
            return ()
        return tuple(
            location for location in raw_locations if isinstance(location, Mapping)
        )

    def resolve_location(self) -> Mapping[str, object]:
        locations = self.list_locations()
        if self.settings.location_id:
            for location in locations:
                if str(location.get("id", "")) == self.settings.location_id:
                    return location
            raise SquareConfigurationError(
                "SQUARE_LOCATION_ID does not match a location returned by Square. "
                "Run 'uv run pizzeria-square-check' to list valid IDs."
            )

        active_locations = tuple(
            location
            for location in locations
            if str(location.get("status", "ACTIVE")) == "ACTIVE"
        )
        if len(active_locations) == 1:
            return active_locations[0]
        if not active_locations:
            raise SquareConfigurationError("Square returned no active locations.")

        options = ", ".join(
            f"{location.get('name', 'Unnamed')} ({location.get('id', 'unknown')})"
            for location in active_locations
        )
        raise SquareConfigurationError(
            "More than one active Square location is available. Set "
            f"SQUARE_LOCATION_ID in .env. Locations: {options}"
        )

    def retrieve_order(self, order_id: str) -> Mapping[str, object]:
        """Retrieve the complete current Square order document."""
        response = self._request("GET", f"/v2/orders/{quote(order_id, safe='')}")
        order = response.get("order")
        if not isinstance(order, Mapping):
            raise SquareAPIError("Square did not return an order document.")
        return order

    def get_payment(self, payment_id: str) -> Mapping[str, object]:
        """Retrieve one payment associated with an order tender."""
        response = self._request(
            "GET", f"/v2/payments/{quote(payment_id, safe='')}"
        )
        payment = response.get("payment")
        if not isinstance(payment, Mapping):
            raise SquareAPIError("Square did not return a payment document.")
        return payment

    def search_orders_for_service_date(
        self,
        *,
        location_id: str,
        created_start_at: str,
        created_end_at: str,
    ) -> tuple[Mapping[str, object], ...]:
        query: dict[str, object] = {
            "filter": {
                "state_filter": {"states": ["OPEN", "COMPLETED"]},
                "date_time_filter": {
                    "created_at": {
                        "start_at": created_start_at,
                        "end_at": created_end_at,
                    }
                },
            },
            "sort": {"sort_field": "CREATED_AT", "sort_order": "ASC"},
        }
        base_payload: dict[str, object] = {
            "location_ids": [location_id],
            "query": query,
            "limit": 1000,
            "return_entries": False,
        }

        orders: list[Mapping[str, object]] = []
        cursor: str | None = None
        while True:
            payload = dict(base_payload)
            if cursor:
                payload["cursor"] = cursor
            response = self._request("POST", "/v2/orders/search", payload)
            raw_orders = response.get("orders", [])
            if isinstance(raw_orders, list):
                orders.extend(
                    order for order in raw_orders if isinstance(order, Mapping)
                )
            next_cursor = response.get("cursor")
            cursor = str(next_cursor) if next_cursor else None
            if not cursor:
                break
        return tuple(orders)

    def search_pickup_orders(
        self,
        *,
        location_id: str,
        created_start_at: str,
        created_end_at: str,
    ) -> tuple[Mapping[str, object], ...]:
        """Backward-compatible alias for the broader service-date search."""
        return self.search_orders_for_service_date(
            location_id=location_id,
            created_start_at=created_start_at,
            created_end_at=created_end_at,
        )


    def list_payments(
        self,
        *,
        location_id: str,
        begin_time: str,
        end_time: str,
    ) -> tuple[Mapping[str, object], ...]:
        """List payments in the order lookback window for receipt-number lookup."""
        payments: list[Mapping[str, object]] = []
        cursor: str | None = None
        while True:
            query = {
                "location_id": location_id,
                "begin_time": begin_time,
                "end_time": end_time,
                "sort_order": "ASC",
                "limit": 100,
            }
            if cursor:
                query["cursor"] = cursor
            response = self._request(
                "GET",
                f"/v2/payments?{urlencode(query)}",
            )
            raw_payments = response.get("payments", [])
            if isinstance(raw_payments, list):
                payments.extend(
                    payment for payment in raw_payments if isinstance(payment, Mapping)
                )
            next_cursor = response.get("cursor")
            cursor = str(next_cursor) if next_cursor else None
            if not cursor:
                break
        return tuple(payments)

    def batch_retrieve_catalog_objects(
        self,
        object_ids: Sequence[str],
        *,
        include_related_objects: bool = False,
    ) -> tuple[Mapping[str, object], ...]:
        unique_ids = tuple(dict.fromkeys(value for value in object_ids if value))
        if not unique_ids:
            return ()

        objects: list[Mapping[str, object]] = []
        for offset in range(0, len(unique_ids), 1000):
            chunk = unique_ids[offset : offset + 1000]
            response = self._request(
                "POST",
                "/v2/catalog/batch-retrieve",
                {
                    "object_ids": list(chunk),
                    "include_related_objects": include_related_objects,
                },
            )
            for key in ("objects", "related_objects"):
                raw_objects = response.get(key, [])
                if isinstance(raw_objects, list):
                    objects.extend(
                        value for value in raw_objects if isinstance(value, Mapping)
                    )
        return tuple(objects)


def _decode_json(raw: bytes) -> Mapping[str, object]:
    if not raw:
        return {}
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SquareAPIError("Square returned a response that was not valid JSON.") from exc
    if not isinstance(decoded, Mapping):
        raise SquareAPIError("Square returned an unexpected JSON response.")
    return decoded


def _extract_errors(payload: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    raw_errors = payload.get("errors", [])
    if not isinstance(raw_errors, list):
        return ()
    return tuple(error for error in raw_errors if isinstance(error, Mapping))


def _format_square_errors(errors: Sequence[Mapping[str, object]]) -> str:
    messages: list[str] = []
    for error in errors:
        detail = str(error.get("detail", "")).strip()
        code = str(error.get("code", "")).strip()
        category = str(error.get("category", "")).strip()
        if detail:
            messages.append(detail)
        elif code:
            messages.append(code)
        elif category:
            messages.append(category)
    return "; ".join(messages)
