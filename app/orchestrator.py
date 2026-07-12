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
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from app.ai import AIClient, AIScoringError, ScoringPayload, doubao_supports_file
from app.config import Config, get_config
from app.parser import (
    classify_attachment,
    extract_document_id,
    extract_text_from_attachment,
    normalize_image_mime,
    truncate_content,
)
from app.docx_convert import docx_to_pdf
from app.feishu import FeishuClient
from app.notification import NotificationManager

logger = logging.getLogger(__name__)


def _extract_open_id(submitter: Any) -> str:
    """从飞书人员字段中解析 open_id。

    人员字段返回格式可能是 [{"id": "ou_xxx", "name": "张三"}]、
    {"id": "ou_xxx"} 或裸字符串。
    """
    if isinstance(submitter, list) and submitter:
        first = submitter[0]
        return first.get("id", "") if isinstance(first, dict) else ""
    if isinstance(submitter, dict):
        return submitter.get("id", "")
    return str(submitter) if submitter else ""


from app.field_mapping import (
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
STATUS_ERROR = "评分异常"   # AI 系统性失败的终止态，不可再触发

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

        # 已评分内容指纹缓存：record_id → 上次处理时的内容 hash。
        # 用于识别「评分写回」触发的记录变更事件（内容未变）并跳过，根除自触发
        # 死循环。内存态，重启后清空（重启后每条记录至多多评一次）。
        self._scored_sig: dict[str, str] = {}

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

    def _content_signature(self, fields: dict[str, Any]) -> str:
        """计算记录「被评分内容」的指纹（原始描述 + 需求文档 + 需求附件）。

        只取真正参与评分的内容字段，且附件用稳定标识（file_token，退回文件名+
        大小），避开 tmp_url 等每次拉取都变的易变值——确保同一份内容多次读取指纹
        一致。用于识别评分写回回声（内容未变）并跳过。
        """
        text = str(fields.get(FIELD_TEXT_CONTENT, "") or "")

        doc = fields.get(FIELD_DOC_LINK, "")
        if isinstance(doc, dict):
            doc = doc.get("link", "") or doc.get("text", "")
        doc = str(doc or "")

        atts = fields.get(FIELD_ATTACHMENT, []) or []
        att_parts: list[str] = []
        if isinstance(atts, list):
            for a in atts:
                if isinstance(a, dict):
                    att_parts.append(
                        str(a.get("file_token")
                            or f"{a.get('name', '')}:{a.get('size', '')}")
                    )

        raw = "".join([text, doc, *att_parts])
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

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

        # 2.5 写回回声防护：被评分内容未变化，且不是用户显式改回「待评分」重触发，
        # 判定为评分写回自身触发的事件，跳过，避免「未通过写回 → 事件 → 再评分」死循环。
        # （事件 payload 携带全部字段且人员字段序列化不稳定，无法靠字段级 diff 可靠
        # 识别，故改用记录实际内容指纹判断。）
        content_sig = self._content_signature(fields)
        if status != STATUS_PENDING and self._scored_sig.get(record_id) == content_sig:
            logger.info(
                "跳过写回回声（评分内容未变化）: record=%s status=%s",
                record_id, status,
            )
            return
        self._scored_sig[record_id] = content_sig

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

        submitter = fields.get(FIELD_SUBMITTER, "")
        open_id = _extract_open_id(submitter)

        # 4. 附件格式白名单校验（Point 5）：任一附件格式不符 → 整条拒审
        attachments = fields.get(FIELD_ATTACHMENT, []) or []
        attachments = attachments if isinstance(attachments, list) else []
        unsupported_files: list[str] = []
        for att in attachments:
            if isinstance(att, dict):
                name = att.get("name", "") or "（未命名文件）"
                kind = classify_attachment(att.get("mime_type", ""), name)
                if kind == "unsupported":
                    unsupported_files.append(name)
        if unsupported_files:
            logger.info(
                "记录含不支持格式附件，跳过评审: record=%s files=%s",
                record_id, unsupported_files,
            )
            if open_id:
                await self._notifier.notify_format_unsupported(
                    open_id=open_id,
                    record_id=record_id,
                    unsupported_files=unsupported_files,
                )
            # 不改变原状态（保持 待评分/未通过），等待用户修正后重新触发
            return

        # 5. 设置状态为"评分中"
        await self._feishu.update_record(
            record_id,
            {FIELD_SCORE_STATUS: STATUS_SCORING},
            app_token=app_token,
            table_id=table_id,
        )

        # 6. 收集内容，按当前 provider 能力决定直传文件还是降级抽文本
        # 仅豆包 + file-capable 型号 走文件/图片直传，其余 provider 降级为文本
        direct_mode = (
            self._config.ai_provider == "doubao"
            and doubao_supports_file(self._config.ai_model)
        )

        text_segments: list[str] = []
        pdf_files: list[tuple[bytes, str]] = []
        image_files: list[tuple[bytes, str]] = []
        images_skipped = False  # 非直传模式下有图片被跳过

        # 6.1 文本字段
        text_content = fields.get(FIELD_TEXT_CONTENT, "") or ""
        if text_content.strip():
            text_segments.append(
                f"=== 文本内容 ===\n{truncate_content(text_content)}"
            )

        # 6.2 在线文档
        doc_cache_text = ""  # 用于回写文档内容缓存字段（raw_content 兜底）
        doc_exported_pdf = False  # 文档已导出 PDF 直传 → 可让模型转写含图片内容
        doc_link = fields.get(FIELD_DOC_LINK, "") or ""
        if isinstance(doc_link, dict):
            doc_link = doc_link.get("link", "") or doc_link.get("text", "")
        if doc_link:
            doc_id = extract_document_id(doc_link)
            doc_text, doc_exported_pdf = await self._collect_online_doc(
                doc_id, fields, direct_mode, pdf_files
            )
            if doc_text:
                doc_cache_text = doc_text
                text_segments.append(f"=== 文档内容 ===\n{truncate_content(doc_text)}")

        # 6.3 附件（已通过格式校验）
        attachment_texts: list[str] = []
        for att in attachments:
            if not isinstance(att, dict):
                continue
            url = att.get("url") or att.get("tmp_url", "")
            name = att.get("name", "") or ""
            mime = att.get("mime_type", "")
            if not url:
                continue
            data = await self._feishu.download_attachment(url)
            if not data:
                continue
            kind = classify_attachment(mime, name)
            text = await self._collect_attachment(
                kind, data, mime, name, direct_mode, pdf_files, image_files
            )
            if text:
                attachment_texts.append(text)
            elif kind == "image" and not direct_mode:
                images_skipped = True
        if attachment_texts:
            text_segments.append(
                "=== 附件内容 ===\n" + "\n\n---\n\n".join(attachment_texts)
            )

        payload = ScoringPayload(
            text="\n\n".join(text_segments),
            pdf_files=pdf_files,
            image_files=image_files,
        )

        # 7. 空内容保护：无任何可评审内容时通知用户，不调用模型
        if payload.is_empty():
            logger.info("无可评审内容，跳过评分: record=%s", record_id)
            reason = (
                "附件为图片，但当前模型不支持图片审核，且无其他文本或文档内容"
                if images_skipped
                else "未提供任何可评审的文本、文档或附件内容"
            )
            # 恢复状态，等待用户补充
            await self._feishu.update_record(
                record_id,
                {FIELD_SCORE_STATUS: status},
                app_token=app_token,
                table_id=table_id,
            )
            if open_id:
                await self._notifier.notify_unprocessable(
                    open_id=open_id, record_id=record_id, reason=reason
                )
            return

        # 8. 调用 AI 评分
        # 文档已 PDF 直传且缓存为空时，让模型顺带转写文档（含图片描述）回填缓存；
        # 缓存已填则不再转写，省去修改重评轮次的额外 token。
        need_transcribe = doc_exported_pdf and not fields.get(FIELD_DOC_CACHE)
        try:
            result = await self._ai.score(payload, transcribe=need_transcribe)
        except AIScoringError as e:
            # 置为「评分异常」终止态（不在 TRIGGERABLE_STATUSES 中），避免恢复
            # 成可触发状态后被写回事件反复重新触发，形成死循环。
            logger.error(
                "AI 评分失败，置为评分异常终止态: record=%s error=%s", record_id, e
            )
            await self._feishu.update_record(
                record_id,
                {FIELD_SCORE_STATUS: STATUS_ERROR},
                app_token=app_token,
                table_id=table_id,
            )
            # 向告警群推送异常卡片（未配置群则内部跳过）
            await self._notifier.notify_error_to_group(
                record_id=record_id,
                error=str(e),
                record_url=self._build_record_url(app_token, table_id, record_id),
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
        # 将文档内容缓存到辅助字段（供飞书 AI 字段使用）。
        # 文档走了 PDF 直传时优先用模型转写 content_text（图片已转成文字描述，
        # 不再是占位符）；否则回退 raw_content 纯文本（视觉不可用时的兜底）。
        if not fields.get(FIELD_DOC_CACHE):
            transcribed = result.get("content_text") if doc_exported_pdf else None
            cache_text = (
                transcribed
                if isinstance(transcribed, str) and transcribed.strip()
                else doc_cache_text
            )
            if cache_text:
                update_fields[FIELD_DOC_CACHE] = truncate_content(
                    cache_text, max_chars=5000
                )

        success = await self._feishu.update_record(
            record_id,
            update_fields,
            app_token=app_token,
            table_id=table_id,
        )
        if not success:
            logger.error("更新评分字段失败: record=%s", record_id)
            return

        # 9. 根据结果通知（open_id 已在收集阶段解析）
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
                    base_url=self._config.feishu_base_url,
                    app_token=app_token,
                    table_id=table_id,
                )

        logger.info(
            "评分完成: record=%s score=%d status=%s rounds=%d",
            record_id, score, new_status, new_rounds,
        )

    async def _collect_online_doc(
        self,
        doc_id: str | None,
        fields: dict[str, Any],
        direct_mode: bool,
        pdf_files: list[tuple[bytes, str]],
    ) -> tuple[str, bool]:
        """收集在线文档内容。

        direct_mode（豆包直传）下优先导出为 PDF 直传（内嵌图可被模型看到）；
        导出失败或非直传模式回退到 raw_content 纯文本，再回退到缓存字段。

        返回 (兜底纯文本, 是否已导出 PDF 并直传)。第二个值为 True 时文档已作为
        PDF 进入 payload，可让视觉模型转写（含图片描述）回填「文档内容缓存」；
        为 False 时只能用 raw_content 纯文本，其中的图片是占位符。
        """
        if not doc_id:
            # 无法解析 doc_id 时，尝试缓存字段
            return fields.get(FIELD_DOC_CACHE, "") or "", False

        if direct_mode:
            pdf = await self._feishu.export_doc_to_pdf(doc_id)
            if pdf:
                pdf_files.append((pdf, f"{doc_id}.pdf"))
                # 直传成功仍抓一次纯文本作兜底（供 payload.text），失败不阻塞
                raw = await self._feishu.get_doc_raw_content(doc_id)
                return raw or "", True
            logger.info("在线文档导出 PDF 失败，回退纯文本: doc_id=%s", doc_id)

        raw = await self._feishu.get_doc_raw_content(doc_id)
        if raw:
            return raw, False
        return fields.get(FIELD_DOC_CACHE, "") or "", False

    async def _collect_attachment(
        self,
        kind: str,
        data: bytes,
        mime: str,
        name: str,
        direct_mode: bool,
        pdf_files: list[tuple[bytes, str]],
        image_files: list[tuple[bytes, str]],
    ) -> str | None:
        """收集单个附件，按 kind 与 provider 能力分流。

        返回可加入纯文本兜底的抽取文本；走直传的 PDF/图片返回 None（已直接
        追加到 pdf_files/image_files）。

        - pdf: 直传模式入 pdf_files；否则抽文本。
        - docx: 直传模式先转 PDF 入 pdf_files（失败回退抽文本）；否则抽文本。
        - text: 始终抽文本。
        - image: 直传模式入 image_files；否则跳过（返回 None）。
        """
        if kind == "text":
            return extract_text_from_attachment(data, mime, name)

        if kind == "image":
            if direct_mode:
                img_mime = normalize_image_mime(mime, name) or "image/png"
                image_files.append((data, img_mime))
            return None

        if kind == "pdf":
            if direct_mode:
                pdf_files.append((data, name or "attachment.pdf"))
                return None
            return extract_text_from_attachment(data, mime, name)

        if kind == "docx":
            if direct_mode:
                pdf = await docx_to_pdf(data, name)
                if pdf:
                    pdf_files.append((pdf, (name or "attachment") + ".pdf"))
                    return None
                logger.info("docx 转 PDF 失败，回退抽文本: file=%s", name)
            return extract_text_from_attachment(data, mime, name)

        return None

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

        open_id = _extract_open_id(fields.get(FIELD_SUBMITTER, ""))
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

    def _build_record_url(
        self, app_token: str, table_id: str, record_id: str
    ) -> str:
        """拼接多维表格记录跳转链接；未配置域名则返回空串。"""
        base_url = self._config.feishu_base_url
        if not base_url:
            return ""
        return (
            f"{base_url}/base/{app_token}"
            f"?table={table_id}&record={record_id}"
        )

    async def close(self) -> None:
        """释放资源。"""
        await self._feishu.close()
