"""配置管理 —— 所有环境变量集中定义和校验。"""

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# 加载项目根目录的 .env 文件
load_dotenv(Path(__file__).parent / ".env")


@dataclass
class Config:
    """应用配置。所有字段从环境变量读取，有明确默认值。"""

    # ---- 飞书应用 ----
    feishu_app_id: str = field(
        default_factory=lambda: os.getenv("FEISHU_APP_ID", "")
    )
    feishu_app_secret: str = field(
        default_factory=lambda: os.getenv("FEISHU_APP_SECRET", "")
    )

    # ---- 多维表格 ----
    bitable_app_token: str = field(
        default_factory=lambda: os.getenv("BITABLE_APP_TOKEN", "")
    )
    bitable_table_id: str = field(
        default_factory=lambda: os.getenv("BITABLE_TABLE_ID", "")
    )

    # ---- Webhook 安全 ----
    webhook_verification_token: str = field(
        default_factory=lambda: os.getenv("WEBHOOK_VERIFICATION_TOKEN", "")
    )
    webhook_encrypt_key: str = field(
        default_factory=lambda: os.getenv("WEBHOOK_ENCRYPT_KEY", "")
    )

    # ---- AI 配置 ----
    ai_provider: str = field(
        default_factory=lambda: os.getenv("AI_PROVIDER", "openai")
    )
    ai_api_key: str = field(
        default_factory=lambda: os.getenv("AI_API_KEY", "")
    )
    ai_model: str = field(
        default_factory=lambda: os.getenv("AI_MODEL", "gpt-4o")
    )
    ai_base_url: str = field(
        default_factory=lambda: os.getenv("AI_BASE_URL", "")
    )

    # ---- 评分配置 ----
    score_threshold: int = field(
        default_factory=lambda: int(os.getenv("SCORE_THRESHOLD", "60"))
    )
    max_revision_rounds: int = field(
        default_factory=lambda: int(os.getenv("MAX_REVISION_ROUNDS", "5"))
    )

    # ---- 通知配置 ----
    notification_cooldown_minutes: int = field(
        default_factory=lambda: int(os.getenv("NOTIFICATION_COOLDOWN_MINUTES", "60"))
    )
    max_daily_notifications_per_user: int = field(
        default_factory=lambda: int(os.getenv("MAX_DAILY_NOTIFICATIONS_PER_USER", "3"))
    )

    # ---- 服务配置 ----
    host: str = field(
        default_factory=lambda: os.getenv("HOST", "0.0.0.0")
    )
    port: int = field(
        default_factory=lambda: int(os.getenv("PORT", "8000"))
    )
    log_level: str = field(
        default_factory=lambda: os.getenv("LOG_LEVEL", "INFO")
    )

    def validate(self) -> list[str]:
        """校验必填配置项，返回缺失项列表。"""
        missing: list[str] = []
        if not self.feishu_app_id:
            missing.append("FEISHU_APP_ID")
        if not self.feishu_app_secret:
            missing.append("FEISHU_APP_SECRET")
        if not self.bitable_app_token:
            missing.append("BITABLE_APP_TOKEN")
        if not self.bitable_table_id:
            missing.append("BITABLE_TABLE_ID")
        if not self.ai_api_key and self.ai_provider != "doubao":
            missing.append("AI_API_KEY")
        return missing


@lru_cache(maxsize=1)
def get_config() -> Config:
    """获取全局配置单例。"""
    cfg = Config()
    missing = cfg.validate()
    if missing:
        raise ValueError(
            f"缺少必要的环境变量: {', '.join(missing)}。"
            f"请参考 .env.example 配置 .env 文件。"
        )
    return cfg
