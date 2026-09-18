from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]
SHANGHAI = ZoneInfo("Asia/Shanghai")
NEW_YORK = ZoneInfo("America/New_York")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TIN_", env_file=ROOT / ".env", extra="ignore")

    database_url: str = f"sqlite:///{ROOT / 'data' / 'tin.db'}"
    exports_dir: Path = ROOT / "exports"
    variety: str = "SN"
    liquidity_min_volume: int = 500
    http_timeout: float = 40.0


settings = Settings()
