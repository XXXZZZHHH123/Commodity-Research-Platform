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

    # ---- LLM 接入层（SPEC §4.1 / F2）----
    # 换 provider 的唯一开关。anthropic（现阶段）/ openai_compatible（内网 30B）/ fake（测试）。
    # 业务代码不读这一项，只调 tin.llm.build_provider()。
    llm_provider: str = "anthropic"
    llm_model: str = "claude-opus-5"
    # OpenAI 兼容端点地址，本地 vLLM 形如 http://10.0.0.5:8000/v1；Anthropic 这条路不用
    llm_base_url: str | None = None
    # 本地端点通常不校验密钥。Anthropic 的凭据由 SDK 自行解析（环境变量或 ant auth login
    # 存的 OAuth profile），不从这里读，也不要往这里填。
    llm_api_key: str | None = None
    llm_timeout: float = 120.0
    llm_max_retries: int = 2
    llm_max_tokens: int = 16000
    # 安全分类器误伤时由服务端换模型续上，而不是把失败甩给研究员。
    # 切到内网自托管端点后这个开关没有意义，置 false。
    llm_fallback: bool = True


settings = Settings()
