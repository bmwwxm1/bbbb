"""Configuration — all settings in one place.

Loaded from environment variables / .env file via pydantic-settings.
Validated at startup — invalid config = fast fail, no silent corruption.
"""

from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings

NANOTON = 1_000_000_000


class Settings(BaseSettings):
    # Telegram
    telegram_bot_token: str
    admin_chat_id: int

    # MRKT
    mrkt_auth_token: str
    mrkt_api_base: str = "https://api.mrkt.xyz/api/v1"
    mrkt_origin: str = "https://mrkt.xyz"

    # Getgems
    getgems_api_key: str = ""
    getgems_auth_token: str = ""
    wallet_mnemonic: str = ""
    getgems_cdp_url: str = "http://localhost:29229"

    # Fragment
    fragment_api_key: str = ""
    fragment_cookies: str = ""

    # Portal Market
    portal_tma_init_data: str = ""
    portal_buy_fee_pct: float = 0.0
    portal_sell_fee_pct: float = 0.0
    portal_withdraw_fee_ton: float = 0.25

    # Telegram session (for gift transfers)
    telegram_session: str = ""
    telegram_api_id: int = 2040
    telegram_api_hash: str = "b18441a1ff607e10a989891a5462e627"
    gift_target_getgems: str = "gemsrelayer"
    gift_target_mrkt: str = "mrktbank"
    gift_target_fragment: str = ""
    gift_target_portal: str = "GiftsToPortals"

    # Database
    database_url: str

    # Redis (optional — falls back to PG locks if not set)
    redis_url: str = ""

    # Database pool
    db_pool_size: int = 5
    db_max_overflow: int = 10

    # Trading
    min_roi_percent: float = 10.0
    min_absolute_profit_ton: float = 0.5
    min_confidence: float = 0.3
    max_trade_amount_ton: float = 0  # 0 = no limit

    # Fees (percent)
    mrkt_buy_fee_pct: float = 0.0
    mrkt_sell_fee_pct: float = 5.0
    getgems_sell_fee_pct: float = 0.0

    # Withdrawal fees (TON)
    mrkt_withdraw_fee_ton: float = 0.2
    getgems_withdraw_fee_ton: float = 0.3
    fragment_withdraw_fee_ton: float = 0.0
    # portal_withdraw_fee_ton defined above with Portal settings

    # Risk
    max_total_exposure_ton: float = 600.0
    max_per_collection_ton: float = 600.0
    max_items_total: int = 50
    max_items_per_collection: int = 10
    daily_loss_limit_ton: float = 50.0
    max_holding_hours: int = 168

    # Scanner
    request_delay_seconds: float = 0.5
    collections_refresh_minutes: int = 5
    scan_interval_seconds: float = 30.0

    # Auto-seller
    autosell_check_interval: float = 120.0

    # Falling price listing (Getgems "Падающая цена")
    falling_price_enabled: bool = False
    falling_price_interval_ms: int = 3_600_000  # 1 hour
    falling_price_decrease_pct: float = 3.0  # decrease by 3% of start price each interval

    # Strategies
    cross_market_enabled: bool = True
    order_arb_enabled: bool = False
    deep_discount_enabled: bool = True
    price_drop_threshold: float = 0.40

    # Shadow mode (default ON — never start with real trading)
    shadow_mode: bool = True

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @field_validator("min_roi_percent")
    @classmethod
    def _roi_positive(cls, v: float) -> float:
        if v < 0:
            raise ValueError("min_roi_percent must be >= 0")
        return v

    @field_validator("mrkt_sell_fee_pct", "mrkt_buy_fee_pct", "getgems_sell_fee_pct")
    @classmethod
    def _fee_range(cls, v: float) -> float:
        if v < 0 or v > 100:
            raise ValueError("Fee percentage must be 0-100")
        return v

    @field_validator("max_total_exposure_ton", "max_per_collection_ton", "daily_loss_limit_ton")
    @classmethod
    def _exposure_positive(cls, v: float) -> float:
        if v < 0:
            raise ValueError("Exposure limits must be >= 0")
        return v

    @field_validator("db_pool_size")
    @classmethod
    def _pool_size(cls, v: int) -> int:
        if v < 1:
            raise ValueError("db_pool_size must be >= 1")
        return v

    @property
    def max_trade_amount_nanoton(self) -> int:
        if self.max_trade_amount_ton <= 0:
            return 0
        return int(self.max_trade_amount_ton * NANOTON)


settings = Settings()  # type: ignore[call-arg]
