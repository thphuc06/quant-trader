"""Configuration: environment settings, application YAML and the risk policy.

Defaults are safe: PAPER mode, live disabled, no credentials.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TradingMode(StrEnum):
    PAPER = "PAPER"
    BINANCE_DEMO = "BINANCE_DEMO"
    LIVE = "LIVE"


@dataclass(frozen=True)
class BinanceCredentials:
    """One environment's API key plus exactly one signing method.

    ``hmac_secret``: Binance "system generated" key. ``private_key_file``: a self-generated Ed25519 key in PEM
    (only its public half is registered with Binance; the private half never leaves this machine).
    """

    api_key: SecretStr
    hmac_secret: SecretStr | None = None
    private_key_file: Path | None = None

    @property
    def method(self) -> str:
        return "ed25519" if self.private_key_file is not None else "hmac"


def project_root() -> Path:
    """Repository root: $TRADER_HOME if set, else the directory holding ``configs/``."""
    override = os.environ.get("TRADER_HOME")
    if override:
        return Path(override).resolve()
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "configs" / "risk_policy.yaml").exists():
            return parent
    return here.parents[2]


class Settings(BaseSettings):
    """Process settings from environment variables and an optional ``.env`` file."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    trading_mode: TradingMode = TradingMode.PAPER
    live_trading_enabled: bool = False
    trader_data_dir: Path | None = None

    binance_demo_api_key: SecretStr | None = None
    binance_demo_api_secret: SecretStr | None = None
    binance_live_api_key: SecretStr | None = None
    binance_live_api_secret: SecretStr | None = None
    # Self-generated Ed25519 keys: path to the PEM private key (relative paths are resolved from the repo root).
    binance_demo_private_key_file: Path | None = None
    binance_live_private_key_file: Path | None = None

    tavily_api_key: SecretStr | None = None
    brave_api_key: SecretStr | None = None

    @field_validator(
        "binance_demo_api_key",
        "binance_demo_api_secret",
        "binance_live_api_key",
        "binance_live_api_secret",
        "binance_demo_private_key_file",
        "binance_live_private_key_file",
        "tavily_api_key",
        "brave_api_key",
        mode="before",
    )
    @classmethod
    def _empty_is_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    def data_dir(self, root: Path | None = None) -> Path:
        base = self.trader_data_dir or ((root or project_root()) / "data")
        return Path(base)

    def _raw_credentials(self, mode: TradingMode) -> tuple[SecretStr | None, SecretStr | None, Path | None]:
        if mode is TradingMode.BINANCE_DEMO:
            return self.binance_demo_api_key, self.binance_demo_api_secret, self.binance_demo_private_key_file
        if mode is TradingMode.LIVE:
            return self.binance_live_api_key, self.binance_live_api_secret, self.binance_live_private_key_file
        return None, None, None

    def credentials_problem(self, mode: TradingMode) -> str | None:
        """Why this mode has no usable credentials (None when they are complete). Never reveals values."""
        if mode is TradingMode.PAPER:
            return "PAPER mode uses no exchange credentials"
        key, secret, key_file = self._raw_credentials(mode)
        prefix = f"BINANCE_{'DEMO' if mode is TradingMode.BINANCE_DEMO else 'LIVE'}"
        if key is None:
            return f"{prefix}_API_KEY is not set"
        if secret is not None and key_file is not None:
            return f"both an HMAC secret and {prefix}_PRIVATE_KEY_FILE are set; keep exactly one"
        if secret is None and key_file is None:
            return f"set either the HMAC secret or {prefix}_PRIVATE_KEY_FILE (Ed25519)"
        return None

    def credentials_for(self, mode: TradingMode) -> BinanceCredentials | None:
        """Credentials for exactly this mode. Never falls back to another environment; ambiguity is an error."""
        if self.credentials_problem(mode) is not None:
            return None
        key, secret, key_file = self._raw_credentials(mode)
        assert key is not None
        return BinanceCredentials(api_key=key, hmac_secret=secret, private_key_file=key_file)


def load_settings(root: Path | None = None) -> Settings:
    env_file = (root or project_root()) / ".env"
    return Settings(_env_file=env_file if env_file.exists() else None)  # type: ignore[call-arg]


class Endpoints(BaseModel):
    market_data_rest: str
    market_data_ws: str
    futures_public_rest: str
    binance_demo_rest: str
    live_rest: str

    def trading_rest(self, mode: TradingMode) -> str:
        if mode is TradingMode.BINANCE_DEMO:
            return self.binance_demo_rest
        if mode is TradingMode.LIVE:
            return self.live_rest
        raise ValueError("PAPER mode has no trading endpoint")


class HttpConfig(BaseModel):
    timeout_seconds: float = 10
    max_retries: int = 3
    recv_window_ms: int = 5000
    weight_soft_limit_1m: int = 3000


class CostConfig(BaseModel):
    fee_bps: float = 10
    half_spread_bps: float = 1
    slippage_bps: float = 2

    def per_side_fraction(self, multiplier: float = 1.0) -> float:
        return (self.fee_bps + self.half_spread_bps + self.slippage_bps) * multiplier / 10_000


class PaperConfig(BaseModel):
    starting_balance_usdt: Decimal = Decimal("10000")
    fee_bps: Decimal = Decimal("10")
    slippage_bps: Decimal = Decimal("2")


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    version: str
    universe: dict[str, Any]
    timeframes: dict[str, Any]
    endpoints: Endpoints
    http: HttpConfig = HttpConfig()
    costs: CostConfig = CostConfig()
    paper: PaperConfig = PaperConfig()
    features: dict[str, int] = Field(default_factory=dict)
    features_by_timeframe: dict[str, dict[str, int]] = Field(default_factory=dict)
    min_bars: dict[str, int] = Field(default_factory=dict)
    strategies: dict[str, dict[str, float]] = Field(default_factory=dict)
    backtest: dict[str, Any] = Field(default_factory=dict)
    validation: dict[str, float] = Field(default_factory=dict)
    ml: dict[str, Any] = Field(default_factory=dict)
    research_budget: dict[str, int] = Field(default_factory=dict)
    news: dict[str, int] = Field(default_factory=dict)

    @property
    def symbols(self) -> list[str]:
        return list(self.universe["symbols"])

    @property
    def decision_timeframe(self) -> str:
        return str(self.timeframes["decision"])

    @property
    def context_timeframes(self) -> list[str]:
        return [str(x) for x in self.timeframes.get("context", [])]

    def feature_params(self, interval: str) -> dict[str, int]:
        return {**self.features, **self.features_by_timeframe.get(interval, {})}

    def bars_needed(self, interval: str) -> int:
        return int(self.min_bars.get(interval, 500))


def load_app_config(root: Path | None = None) -> AppConfig:
    path = (root or project_root()) / "configs" / "app.yaml"
    with path.open("r", encoding="utf-8") as handle:
        return AppConfig.model_validate(yaml.safe_load(handle))


class RiskPolicy(BaseModel):
    """Risk limits. Parsed strictly: unknown keys are an error so typos cannot disable a limit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    status: str
    allowed_instruments: list[str]
    allowed_symbols: list[str]
    allowed_order_types: list[str]
    allowed_open_sides: list[str]
    max_leverage: Decimal

    max_risk_per_trade_pct: Decimal
    max_position_notional_pct: Decimal
    max_total_exposure_pct: Decimal
    max_open_positions: int
    daily_loss_limit_pct: Decimal
    weekly_loss_limit_pct: Decimal
    max_drawdown_kill_pct: Decimal

    require_stop_loss: bool
    min_stop_distance_pct: Decimal
    max_stop_distance_pct: Decimal
    min_reward_risk: Decimal | None = None

    max_spread_bps: Decimal
    max_expected_slippage_bps: Decimal
    min_24h_quote_volume: Decimal
    max_quote_age_seconds: int
    max_candle_staleness_bars: int

    cooldown_after_loss_minutes: int
    intent_ttl_minutes: int
    approval_ttl_minutes: int
    live_unlock_ttl_minutes: int
    cli_approval_required_modes: list[str]

    @field_validator("status")
    @classmethod
    def _status(cls, value: str) -> str:
        if value not in {"provisional", "approved"}:
            raise ValueError("status must be 'provisional' or 'approved'")
        return value

    @field_validator(
        "max_risk_per_trade_pct",
        "max_position_notional_pct",
        "max_total_exposure_pct",
        "daily_loss_limit_pct",
        "weekly_loss_limit_pct",
        "max_drawdown_kill_pct",
        "min_stop_distance_pct",
        "max_stop_distance_pct",
        "max_spread_bps",
        "max_expected_slippage_bps",
        "min_24h_quote_volume",
    )
    @classmethod
    def _positive(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("limit must be positive")
        return value

    @field_validator("max_leverage")
    @classmethod
    def _no_leverage(cls, value: Decimal) -> Decimal:
        if value != Decimal("1"):
            raise ValueError("this system supports max_leverage = 1 only")
        return value

    def cli_approval_required(self, mode: TradingMode) -> bool:
        return mode.value in self.cli_approval_required_modes


class LoadedPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    policy: RiskPolicy
    sha256: str
    path: str


def load_risk_policy(root: Path | None = None) -> LoadedPolicy:
    """Load and hash the policy file. Any error propagates: callers must fail closed."""
    path = (root or project_root()) / "configs" / "risk_policy.yaml"
    raw = path.read_bytes()
    policy = RiskPolicy.model_validate(yaml.safe_load(raw.decode("utf-8")))
    return LoadedPolicy(policy=policy, sha256=hashlib.sha256(raw).hexdigest(), path=str(path))
