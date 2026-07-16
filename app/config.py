"""配置管理 —— 所有环境变量集中定义和校验。"""

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# 加载项目根目录的 .env 文件
load_dotenv(Path(__file__).parent.parent / ".env")


def _csv_env(name: str, default: str) -> tuple[str, ...]:
    return tuple(
        item.strip().lower()
        for item in os.getenv(name, default).split(",")
        if item.strip()
    )


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
        default_factory=lambda: os.getenv(
            "AI_MODEL", "doubao-seed-2-0-pro-260215"
        )
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

    # ---- 后台任务 ----
    shutdown_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("SHUTDOWN_TIMEOUT_SECONDS", "30"))
    )
    scavenger_interval_seconds: float = field(
        default_factory=lambda: float(os.getenv("SCAVENGER_INTERVAL_SECONDS", "60"))
    )
    scoring_orphan_timeout_seconds: float = field(
        default_factory=lambda: float(
            os.getenv("SCORING_ORPHAN_TIMEOUT_SECONDS", "900")
        )
    )

    # ---- 内容资源与下载安全 ----
    max_attachment_count: int = field(
        default_factory=lambda: int(os.getenv("MAX_ATTACHMENT_COUNT", "20"))
    )
    max_single_attachment_mb: int = field(
        default_factory=lambda: int(os.getenv("MAX_SINGLE_ATTACHMENT_MB", "20"))
    )
    max_total_attachment_mb: int = field(
        default_factory=lambda: int(os.getenv("MAX_TOTAL_ATTACHMENT_MB", "100"))
    )
    max_pdf_pages: int = field(
        default_factory=lambda: int(os.getenv("MAX_PDF_PAGES", "300"))
    )
    max_image_count: int = field(
        default_factory=lambda: int(os.getenv("MAX_IMAGE_COUNT", "20"))
    )
    doc_cache_max_chars: int = field(
        default_factory=lambda: int(os.getenv("DOC_CACHE_MAX_CHARS", "5000"))
    )
    attachment_allowed_hosts: tuple[str, ...] = field(
        default_factory=lambda: _csv_env(
            "ATTACHMENT_ALLOWED_HOSTS",
            ".feishu.cn,.larksuite.com,.larkoffice.com,open.feishu.cn",
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
        """返回全部启动配置错误，便于一次修完而非逐项试错。"""
        errors: list[str] = []
        if not self.feishu_app_id:
            errors.append("FEISHU_APP_ID 不能为空")
        if not self.feishu_app_secret:
            errors.append("FEISHU_APP_SECRET 不能为空")
        if not self.bitable_app_token:
            errors.append("BITABLE_APP_TOKEN 不能为空")
        if not self.bitable_table_id:
            errors.append("BITABLE_TABLE_ID 不能为空")
        if not self.webhook_verification_token:
            errors.append("WEBHOOK_VERIFICATION_TOKEN 不能为空")
        if not self.webhook_encrypt_key:
            errors.append("WEBHOOK_ENCRYPT_KEY 不能为空")

        if self.ai_provider not in {"doubao"}:
            errors.append(f"AI_PROVIDER 不受支持: {self.ai_provider}")
        if not self.ai_api_key:
            errors.append("AI_API_KEY 不能为空")
        if not self.ai_model:
            errors.append("AI_MODEL 不能为空")
        if not 0 <= self.ai_temperature <= 2:
            errors.append("AI_TEMPERATURE 必须在 0 到 2 之间")
        if not 1 <= self.ai_score_max_tokens <= 128_000:
            errors.append("AI_SCORE_MAX_TOKENS 必须在 1 到 128000 之间")
        if not 1 <= self.ai_transcribe_max_tokens <= 128_000:
            errors.append("AI_TRANSCRIBE_MAX_TOKENS 必须在 1 到 128000 之间")

        if not 0 <= self.score_threshold <= 100:
            errors.append("SCORE_THRESHOLD 必须在 0 到 100 之间")
        if self.max_revision_rounds < 1:
            errors.append("MAX_REVISION_ROUNDS 必须大于 0")
        if self.send_circuit_breaker_window_minutes < 1:
            errors.append("SEND_CIRCUIT_BREAKER_WINDOW_MINUTES 必须大于 0")
        if self.send_circuit_breaker_max_messages < 1:
            errors.append("SEND_CIRCUIT_BREAKER_MAX_MESSAGES 必须大于 0")
        if self.shutdown_timeout_seconds <= 0:
            errors.append("SHUTDOWN_TIMEOUT_SECONDS 必须大于 0")
        if self.scavenger_interval_seconds <= 0:
            errors.append("SCAVENGER_INTERVAL_SECONDS 必须大于 0")
        if self.scoring_orphan_timeout_seconds <= 0:
            errors.append("SCORING_ORPHAN_TIMEOUT_SECONDS 必须大于 0")
        if self.max_attachment_count < 1:
            errors.append("MAX_ATTACHMENT_COUNT 必须大于 0")
        if self.max_single_attachment_mb < 1:
            errors.append("MAX_SINGLE_ATTACHMENT_MB 必须大于 0")
        if self.max_total_attachment_mb < self.max_single_attachment_mb:
            errors.append("MAX_TOTAL_ATTACHMENT_MB 不得小于单附件上限")
        if self.max_pdf_pages < 1:
            errors.append("MAX_PDF_PAGES 必须大于 0")
        if self.max_image_count < 1:
            errors.append("MAX_IMAGE_COUNT 必须大于 0")
        if self.doc_cache_max_chars < 1:
            errors.append("DOC_CACHE_MAX_CHARS 必须大于 0")
        if not self.attachment_allowed_hosts:
            errors.append("ATTACHMENT_ALLOWED_HOSTS 至少配置一个允许域名")
        if not 1 <= self.port <= 65_535:
            errors.append("PORT 必须在 1 到 65535 之间")
        return errors


@lru_cache(maxsize=1)
def get_config() -> Config:
    """获取全局配置单例。"""
    cfg = Config()
    errors = cfg.validate()
    if errors:
        raise ValueError(
            "配置校验失败：" + "；".join(errors) + "。"
            "请参考 .env.example 修正环境变量。"
        )
    return cfg
