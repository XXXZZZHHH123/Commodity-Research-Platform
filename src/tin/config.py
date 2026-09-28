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

    # 启动时自动把库升到最新。默认开：部署脚本和容器启动都没有迁移步骤，
    # 不自动跑就意味着每次带迁移的发布都得有人手动上服务器执行——而漏跑的代价
    # 是整站 503。迁移前会自动备份（见 tin.schema_check.auto_upgrade）。
    # 需要人工把关的环境（比如多实例共享一个库）置 false，退回 503 提示页。
    auto_migrate: bool = True
    # 自动迁移前的备份保留份数。SQLite 就是复制一个文件，很便宜。
    migrate_backups: int = 5

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
    # OpenAI 兼容端点的结构化输出模式，各家支持度不一致：
    #   json_schema —— 服务端按 schema 强约束（vLLM、以及部分百炼模型支持）
    #   json_object —— 只保证是合法 JSON，形状靠 prompt 说明 + 闸门兜底
    #   none        —— 端点完全不支持，纯靠 prompt 要求输出 JSON
    # 阿里云百炼：json_schema 仅限少数千问型号，通用型号要用 json_object。
    llm_json_mode: str = "json_schema"
    # 安全分类器误伤时由服务端换模型续上，而不是把失败甩给研究员。
    # 切到内网自托管端点后这个开关没有意义，置 false。
    llm_fallback: bool = True


settings = Settings()
