"""配置管理 —— 所有环境变量集中定义和校验。"""

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# 加载项目根目录的 .env 文件
load_dotenv(Path(__file__).parent.parent / ".env")


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
    # 飞书租户域名（用于拼接记录跳转链接），形如 https://xxx.feishu.cn
    feishu_base_url: str = field(
        default_factory=lambda: os.getenv("FEISHU_BASE_URL", "")
    )

    # ---- Webhook 安全 ----
    webhook_verification_token: str = field(
        default_factory=lambda: os.getenv("WEBHOOK_VERIFICATION_TOKEN", "")
    )
    webhook_encrypt_key: str = field(
        default_factory=lambda: os.getenv("WEBHOOK_ENCRYPT_KEY", "")
    )

    # ---- AI 配置 ----
    # 当前仅支持 doubao（其余 provider 已下线）
    ai_provider: str = field(
        default_factory=lambda: os.getenv("AI_PROVIDER", "doubao")
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
    # 评分采样温度：默认 0，走确定性采样以最大化多次调用间的评分一致性。
    # 值越低越稳定；如需模型稍有发挥空间可上调（一般不建议超过 0.3）。
    ai_temperature: float = field(
        default_factory=lambda: float(os.getenv("AI_TEMPERATURE", "0"))
    )
    # 评分调用的 max_tokens：评分响应仅含短 JSON（detail≤500字 + 维度分），
    # 默认 4000 留足余量，避免 detail 偏长时被截断触发正则兜底。
    ai_score_max_tokens: int = field(
        default_factory=lambda: int(os.getenv("AI_SCORE_MAX_TOKENS", "4000"))
    )
    # 文档转写调用的 max_tokens：转写正文可能较长（限 4000 字内），默认 16000。
    # 转写与评分已解耦，此处截断只影响缓存回填、不影响评分。
    ai_transcribe_max_tokens: int = field(
        default_factory=lambda: int(os.getenv("AI_TRANSCRIBE_MAX_TOKENS", "16000"))
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
    # 异常/告警通知群的 chat_id（形如 oc_xxx）；为空则不向群推送。
    notification_group_chat_id: str = field(
        default_factory=lambda: os.getenv("NOTIFICATION_GROUP_CHAT_ID", "")
    )
    # 发送侧熔断：同一记录在滑动窗口内最多允许发送的卡片数。
    send_circuit_breaker_window_minutes: int = field(
        default_factory=lambda: int(
            os.getenv("SEND_CIRCUIT_BREAKER_WINDOW_MINUTES", "5")
        )
    )
    send_circuit_breaker_max_messages: int = field(
        default_factory=lambda: int(
            os.getenv("SEND_CIRCUIT_BREAKER_MAX_MESSAGES", "20")
        )
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
