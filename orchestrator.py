"""评分编排核心 —— 完整流程编排器。

负责协调：
1. 获取记录 → 2. 收集内容（文本/文档/附件）→ 3. AI 评分
→ 4. 写回评分 → 5. 判断通知/放行

状态机：
    待评分 → 评分中 → 已通过（结束）
                       → 未通过（等待修改）
    未通过 → 评分中 → ...（循环）
    已驳回（修改轮次超限，需管理员介入）
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from ai_client import AIClient, AIScoringError
from config import Config, get_config
from document_parser import (
    extract_document_id,
    extract_text_from_attachment,
    truncate_content,
)
from feishu_client import FeishuClient
from notification import NotificationManager

logger = logging.getLogger(__name__)


from field_mapping import (
    FIELD_AI_SCORE,
    FIELD_AI_SCORE_DETAIL,
    FIELD_AI_SCORE_TIME,
    FIELD_ATTACHMENT,
    FIELD_DOC_CACHE,
    FIELD_DOC_LINK,
    FIELD_REVISION_ROUNDS,
    FIELD_SCORE_STATUS,
    FIELD_SUBMITTER,
    FIELD_TEXT_CONTENT,
)

# 评分状态常量
STATUS_PENDING = "待评分"
STATUS_SCORING = "评分中"
STATUS_PASSED = "已通过"
STATUS_FAILED = "未通过"
STATUS_REJECTED = "已驳回"

# 可触发的状态（只有这些状态才执行评分）
TRIGGERABLE_STATUSES = {STATUS_PENDING, STATUS_FAILED}


class Orchestrator:
    """评分编排器。

    使用示例::

        orch = Orchestrator()
        await orch.process_record("app_xxx", "tbl_xxx", "rec_xxx")
    """

    def __init__(
        self,
        config: Config | None = None,
        feishu: FeishuClient | None = None,
        ai: AIClient | None = None,
    ) -> None:
        cfg = config or get_config()
        self._config = cfg
        self._feishu = feishu or FeishuClient(cfg)
        self._ai = ai or AIClient(cfg)
        self._notifier = NotificationManager(cfg, self._feishu)

        # 简单的并发锁：同一记录不并发处理
        self._processing: set[str] = set()

    async def process_record(
        self,
        app_token: str,
        table_id: str,
        record_id: str,
    ) -> None:
        """处理单条记录的评分流程（入口方法）。

        Args:
            app_token: 多维表格 app token。
            table_id: 数据表 ID。
            record_id: 记录 ID。
        """
        # 防并发：同一记录正在处理中则跳过
        lock_key = f"{app_token}:{table_id}:{record_id}"
        if lock_key in self._processing:
            logger.info("记录正在处理中，跳过: %s", lock_key)
            return

        self._processing.add(lock_key)
        try:
            await self._do_process(app_token, table_id, record_id)
        except Exception as e:
            logger.exception("处理记录异常: %s error=%s", lock_key, e)
        finally:
            self._processing.discard(lock_key)

    async def _do_process(
        self,
        app_token: str,
        table_id: str,
        record_id: str,
    ) -> None:
        """实际处理流程。"""
        # 1. 获取当前记录
        fields = await self._feishu.get_record(
            record_id, app_token=app_token, table_id=table_id
        )
        if fields is None:
            logger.error("获取记录失败，跳过: %s", record_id)
            return

        # 2. 检查评分状态
        status = fields.get(FIELD_SCORE_STATUS, STATUS_PENDING)
        if status not in TRIGGERABLE_STATUSES:
            logger.info(
                "记录状态不需评分: record=%s status=%s", record_id, status
            )
            return

        # 3. 检查修改轮次
        rounds = int(fields.get(FIELD_REVISION_ROUNDS, 0))
        if rounds >= self._config.max_revision_rounds:
            logger.info(
                "记录已达到最大修改轮次，驳回: record=%s rounds=%d",
                record_id, rounds,
            )
            await self._handle_rejected(
                app_token, table_id, record_id, fields
            )
            return

        # 4. 设置状态为"评分中"
        await self._feishu.update_record(
            record_id,
            {FIELD_SCORE_STATUS: STATUS_SCORING},
            app_token=app_token,
            table_id=table_id,
        )

        # 5. 收集内容
        text_content = fields.get(FIELD_TEXT_CONTENT, "") or ""

        # 从文档链接获取内容
        doc_link = fields.get(FIELD_DOC_LINK, "") or ""
        if isinstance(doc_link, dict):
            doc_link = doc_link.get("link", "") or doc_link.get("text", "")
        doc_content = ""
        if doc_link:
            doc_id = extract_document_id(doc_link)
            if doc_id:
                raw = await self._feishu.get_doc_raw_content(doc_id)
                doc_content = raw or ""
            if not doc_content:
                # 回退到缓存字段
                doc_content = fields.get(FIELD_DOC_CACHE, "") or ""

        # 从附件获取内容
        attachment_content = ""
        attachments = fields.get(FIELD_ATTACHMENT, []) or []
        if isinstance(attachments, list) and attachments:
            attachment_texts: list[str] = []
            for att in attachments:
                if isinstance(att, dict):
                    url = att.get("url") or att.get("tmp_url", "")
                    mime = att.get("mime_type", "")
                    name = att.get("name", "")
                    if url:
                        data = await self._feishu.download_attachment(url)
                        if data:
                            text = extract_text_from_attachment(data, mime, name)
                            if text:
                                attachment_texts.append(text)
            if attachment_texts:
                attachment_content = "\n\n---\n\n".join(attachment_texts)

        # 6. 截断过长内容
        text_content_truncated = truncate_content(text_content)
        doc_content_truncated = truncate_content(doc_content)
        attachment_content_truncated = truncate_content(attachment_content)

        # 7. 调用 AI 评分
        try:
            result = await self._ai.score(
                text_content=text_content_truncated,
                doc_content=doc_content_truncated,
                attachment_content=attachment_content_truncated,
            )
        except AIScoringError as e:
            logger.error("AI 评分失败: %s", e)
            # 恢复状态为之前的状态
            await self._feishu.update_record(
                record_id,
                {FIELD_SCORE_STATUS: status},
                app_token=app_token,
                table_id=table_id,
            )
            return

        score = result.get("score", 0)
        score = max(0, min(100, int(score)))
        detail = result.get("detail", "") or ""

        # 8. 更新记录字段
        new_rounds = rounds + 1
        passed = score >= self._config.score_threshold
        new_status = STATUS_PASSED if passed else STATUS_FAILED
        now = int(datetime.now(timezone.utc).timestamp() * 1000)

        update_fields: dict[str, Any] = {
            FIELD_AI_SCORE: score,
            FIELD_AI_SCORE_DETAIL: detail,
            FIELD_AI_SCORE_TIME: now,
            FIELD_SCORE_STATUS: new_status,
            FIELD_REVISION_ROUNDS: new_rounds,
        }
        # 将获取到的文档内容缓存到辅助字段（供飞书 AI 字段使用）
        if doc_content and not fields.get(FIELD_DOC_CACHE):
            update_fields[FIELD_DOC_CACHE] = truncate_content(doc_content, max_chars=5000)

        success = await self._feishu.update_record(
            record_id,
            update_fields,
            app_token=app_token,
            table_id=table_id,
        )
        if not success:
            logger.error("更新评分字段失败: record=%s", record_id)
            return

        # 9. 根据结果通知
        submitter = fields.get(FIELD_SUBMITTER, "")
        if isinstance(submitter, list) and submitter:
            # 飞书人员字段返回格式: [{"id": "ou_xxx", "name": "张三"}]
            open_id = submitter[0].get("id", "") if isinstance(submitter[0], dict) else ""
        elif isinstance(submitter, dict):
            open_id = submitter.get("id", "")
        else:
            open_id = str(submitter) if submitter else ""

        if open_id:
            if passed:
                await self._notifier.notify_score_passed(
                    open_id=open_id,
                    record_id=record_id,
                    score=score,
                    threshold=self._config.score_threshold,
                )
            else:
                await self._notifier.notify_score_failed(
                    open_id=open_id,
                    record_id=record_id,
                    score=score,
                    detail=detail,
                    threshold=self._config.score_threshold,
                    base_url="https://xxx.feishu.cn",
                    app_token=app_token,
                    table_id=table_id,
                )

        logger.info(
            "评分完成: record=%s score=%d status=%s rounds=%d",
            record_id, score, new_status, new_rounds,
        )

    async def _handle_rejected(
        self,
        app_token: str,
        table_id: str,
        record_id: str,
        fields: dict[str, Any],
    ) -> None:
        """处理驳回情况。"""
        await self._feishu.update_record(
            record_id,
            {FIELD_SCORE_STATUS: STATUS_REJECTED},
            app_token=app_token,
            table_id=table_id,
        )

        submitter = fields.get(FIELD_SUBMITTER, "")
        if isinstance(submitter, list) and submitter:
            open_id = submitter[0].get("id", "") if isinstance(submitter[0], dict) else ""
        elif isinstance(submitter, dict):
            open_id = submitter.get("id", "")
        else:
            open_id = ""

        if open_id:
            score = fields.get(FIELD_AI_SCORE, 0) or 0
            detail = fields.get(FIELD_AI_SCORE_DETAIL, "") or ""
            rounds = int(fields.get(FIELD_REVISION_ROUNDS, 0))
            await self._notifier.notify_rejected(
                open_id=open_id,
                record_id=record_id,
                score=int(score),
                detail=str(detail),
                rounds=rounds,
            )

    async def close(self) -> None:
        """释放资源。"""
        await self._feishu.close()
