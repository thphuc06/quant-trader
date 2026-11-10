"""Time helpers. Everything inside the system is timezone-aware UTC."""

from __future__ import annotations

from datetime import UTC, datetime

INTERVAL_MS: dict[str, int] = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        raise ValueError("naive datetime is not allowed; use UTC-aware datetimes")
    return int(dt.timestamp() * 1000)


def from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise ValueError("naive datetime is not allowed; use UTC-aware datetimes")
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"timestamp without timezone: {value!r}")
    return dt.astimezone(UTC)


def interval_ms(interval: str) -> int:
    try:
        return INTERVAL_MS[interval]
    except KeyError as exc:
        raise ValueError(f"unsupported interval {interval!r}; supported: {sorted(INTERVAL_MS)}") from exc


def bars_per_year(interval: str) -> float:
    return 365.0 * 86_400_000 / interval_ms(interval)
