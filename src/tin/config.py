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
    # 大文件导入的暂存区。放 data/ 下，部署时它是软链到持久卷的，重启不丢。
    staging_dir: Path = ROOT / "data" / "import_staging"
    variety: str = "SN"
    liquidity_min_volume: int = 500
    http_timeout: float = 40.0


settings = Settings()
