"""飞书 Open API 封装 —— 统一管理所有飞书 API 调用。

提供以下能力：
- 租户访问令牌自动获取与刷新
- 多维表格记录读写
- 飞书文档纯文本内容获取
- 机器人消息发送
"""

import asyncio
import json
import logging
from typing import Any

import httpx
import lark_oapi as lark
from lark_oapi.api.bitable.v1 import (
    AppTableRecord,
    GetAppTableRecordRequest,
    UpdateAppTableRecordRequest,
)
from lark_oapi.api.docx.v1 import RawContentDocumentRequest
from lark_oapi.api.drive.v1 import (
    CreateExportTaskRequest,
    DownloadExportTaskRequest,
    ExportTask,
    GetExportTaskRequest,
)
from lark_oapi.api.im.v1 import CreateMessageRequest
from lark_oapi.api.im.v1.model.create_message_request_body import (
    CreateMessageRequestBody,
)
from lark_oapi.api.wiki.v2 import GetNodeSpaceRequest
from lark_oapi.core.token import TokenManager

from app.config import Config

logger = logging.getLogger(__name__)

# 导出任务轮询参数
_EXPORT_POLL_INTERVAL = 1.5   # 每次轮询间隔（秒）
_EXPORT_POLL_MAX_TRIES = 20   # 最多轮询次数（~30s 封顶）
_EXPORT_JOB_SUCCESS = 0       # job_status 为 0 表示导出成功


class FeishuClient:
    """飞书 API 客户端。

    使用示例::

        client = FeishuClient(config)
        record = await client.get_record("rec_xxx")
        await client.update_record("rec_xxx", {"AI评分": 85})
        doc_content = await client.get_doc_raw_content("doc_xxx")
        await client.send_text_message("ou_xxx", "您的提交已通过评审")
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._client = lark.Client.builder() \
            .app_id(config.feishu_app_id) \
            .app_secret(config.feishu_app_secret) \
            .log_level(lark.LogLevel.DEBUG if config.log_level == "DEBUG" else lark.LogLevel.INFO) \
            .build()
        self._http = httpx.AsyncClient(timeout=30.0)

    # ---- 多维表格 ----

    async def get_record(
        self, record_id: str, *, app_token: str = "", table_id: str = ""
    ) -> dict[str, Any] | None:
        """获取单条记录，返回字段 dict 或 None。"""
        app_token = app_token or self._config.bitable_app_token
        table_id = table_id or self._config.bitable_table_id

        req = GetAppTableRecordRequest.builder() \
            .app_token(app_token) \
            .table_id(table_id) \
            .record_id(record_id) \
            .build()
        resp = self._client.bitable.v1.app_table_record.get(req)

        if not resp.success():
            logger.error("获取记录失败: code=%s msg=%s", resp.code, resp.msg)
            return None

        record: AppTableRecord | None = resp.data.record
        if record is None or record.fields is None:
            return None
        return record.fields

    async def update_record(
        self,
        record_id: str,
        fields: dict[str, Any],
        *,
        app_token: str = "",
        table_id: str = "",
    ) -> bool:
        """更新记录字段，返回是否成功。"""
        app_token = app_token or self._config.bitable_app_token
        table_id = table_id or self._config.bitable_table_id

        body = AppTableRecord.builder().fields(fields).build()
        req = UpdateAppTableRecordRequest.builder() \
            .app_token(app_token) \
            .table_id(table_id) \
            .record_id(record_id) \
            .request_body(body) \
            .build()
        resp = self._client.bitable.v1.app_table_record.update(req)

        if not resp.success():
            logger.error("更新记录失败: code=%s msg=%s", resp.code, resp.msg)
            return False
        return True

    # ---- 飞书文档 ----

    async def get_doc_raw_content(self, document_id: str) -> str | None:
        """获取飞书文档的纯文本内容。"""
        req = RawContentDocumentRequest.builder() \
            .document_id(document_id).build()
        resp = self._client.docx.v1.document.raw_content(req)

        if not resp.success():
            logger.error(
                "获取文档内容失败: doc_id=%s code=%s msg=%s",
                document_id, resp.code, resp.msg,
            )
            return None

        return resp.data.content if resp.data else None

    async def get_wiki_node(self, node_token: str) -> tuple[str, str] | None:
        """解析知识库（wiki）节点，返回挂载文档的 (obj_token, obj_type)。

        wiki 链接里的 token 是知识库节点 token，并非真实文档 token；直接拿它去
        导出 PDF 会报 1069914 file token invalid。需先经 Wiki API 解析出实际
        挂载的文档 obj_token 与 obj_type（docx/doc/sheet/...），再用于导出/读取。

        Args:
            node_token: wiki 节点 token（来自 /wiki/<token> 链接）。

        Returns:
            (obj_token, obj_type)，解析失败返回 None（由调用方回退到原 token）。
        """
        req = GetNodeSpaceRequest.builder().token(node_token).build()
        resp = self._client.wiki.v2.space.get_node(req)
        if not resp.success() or resp.data is None or resp.data.node is None:
            logger.error(
                "解析 wiki 节点失败: token=%s code=%s msg=%s",
                node_token, resp.code, resp.msg,
            )
            return None
        node = resp.data.node
        if not node.obj_token or not node.obj_type:
            logger.error("wiki 节点缺少 obj_token/obj_type: token=%s", node_token)
            return None
        return node.obj_token, node.obj_type

    async def export_doc_to_pdf(
        self, doc_token: str, doc_type: str = "docx"
    ) -> bytes | None:
        """将飞书云文档导出为 PDF，返回 PDF 字节内容。

        异步三步流程：创建导出任务 → 轮询任务状态 → 下载产物。
        导出产物在任务完成 ~10 分钟后被删除，故完成后立即下载。
        任一步失败（超时/权限/文档类型不支持）返回 None，由调用方回退
        到 get_doc_raw_content 纯文本。

        Args:
            doc_token: 云文档 token（docx 文档即 document_id）。
            doc_type: 文档类型，普通文档为 "docx"。

        Returns:
            PDF 字节内容，失败返回 None。
        """
        try:
            # 1. 创建导出任务
            create_req = CreateExportTaskRequest.builder() \
                .request_body(
                    ExportTask.builder()
                    .file_extension("pdf")
                    .token(doc_token)
                    .type(doc_type)
                    .build()
                ) \
                .build()
            create_resp = self._client.drive.v1.export_task.create(create_req)
            if not create_resp.success() or create_resp.data is None:
                logger.error(
                    "创建导出任务失败: token=%s code=%s msg=%s",
                    doc_token, create_resp.code, create_resp.msg,
                )
                return None
            ticket = create_resp.data.ticket
            if not ticket:
                logger.error("导出任务未返回 ticket: token=%s", doc_token)
                return None

            # 2. 轮询任务状态直到成功
            file_token: str | None = None
            for _ in range(_EXPORT_POLL_MAX_TRIES):
                await asyncio.sleep(_EXPORT_POLL_INTERVAL)
                get_req = GetExportTaskRequest.builder() \
                    .ticket(ticket) \
                    .token(doc_token) \
                    .build()
                get_resp = self._client.drive.v1.export_task.get(get_req)
                if not get_resp.success() or get_resp.data is None:
                    logger.error(
                        "查询导出任务失败: token=%s code=%s msg=%s",
                        doc_token, get_resp.code, get_resp.msg,
                    )
                    return None
                result = get_resp.data.result
                if result is None:
                    continue
                if result.job_status == _EXPORT_JOB_SUCCESS:
                    file_token = result.file_token
                    break
                # 非 0 且非「进行中」的状态视为失败
                if result.job_status not in (None, _EXPORT_JOB_SUCCESS):
                    # 进行中的状态码文档未固定，这里仅在有明确错误信息时判失败
                    if result.job_error_msg:
                        logger.error(
                            "导出任务失败: token=%s status=%s msg=%s",
                            doc_token, result.job_status, result.job_error_msg,
                        )
                        return None
            if not file_token:
                logger.warning("导出任务超时未完成: token=%s", doc_token)
                return None

            # 3. 下载导出产物
            dl_req = DownloadExportTaskRequest.builder() \
                .file_token(file_token) \
                .build()
            dl_resp = self._client.drive.v1.export_task.download(dl_req)
            if not dl_resp.success() or dl_resp.file is None:
                logger.error(
                    "下载导出文件失败: token=%s code=%s msg=%s",
                    doc_token, dl_resp.code, dl_resp.msg,
                )
                return None
            return dl_resp.file.read()
        except Exception as e:
            logger.error("文档导出 PDF 异常: token=%s error=%s", doc_token, e)
            return None

    # ---- 消息通知 ----

    async def send_text_message(
        self, receive_id: str, text: str, *, receive_id_type: str = "open_id"
    ) -> bool:
        """发送文本消息。

        receive_id_type 默认 open_id（发给个人）；传 chat_id 可发到群。
        """
        content = json.dumps({"text": text})
        body = CreateMessageRequestBody.builder() \
            .receive_id(receive_id) \
            .msg_type("text") \
            .content(content) \
            .build()
        req = CreateMessageRequest.builder() \
            .receive_id_type(receive_id_type) \
            .request_body(body) \
            .build()
        resp = self._client.im.v1.message.create(req)

        if not resp.success():
            logger.error("发送消息失败: code=%s msg=%s", resp.code, resp.msg)
            return False
        return True

    async def send_card_message(
        self, receive_id: str, card_json: dict[str, Any], *, receive_id_type: str = "open_id"
    ) -> bool:
        """发送卡片消息。card_json 为飞书消息卡片 JSON。

        receive_id_type 默认 open_id（发给个人）；传 chat_id 可发到群。
        """
        content = json.dumps(card_json, ensure_ascii=False)
        body = CreateMessageRequestBody.builder() \
            .receive_id(receive_id) \
            .msg_type("interactive") \
            .content(content) \
            .build()
        req = CreateMessageRequest.builder() \
            .receive_id_type(receive_id_type) \
            .request_body(body) \
            .build()
        resp = self._client.im.v1.message.create(req)

        if not resp.success():
            logger.error("发送卡片消息失败: code=%s msg=%s", resp.code, resp.msg)
            return False
        return True

    # ---- 附件下载 ----

    async def download_attachment(self, url: str) -> bytes | None:
        """下载附件文件，返回字节内容。"""
        try:
            # 飞书附件下载需要带 tenant_access_token
            token = TokenManager.get_self_tenant_token(self._client._config)
            headers = {"Authorization": f"Bearer {token}"}
            resp = await self._http.get(url, headers=headers)
            resp.raise_for_status()
            return resp.content
        except Exception as e:
            logger.error("附件下载失败: url=%s error=%s", url, e)
            return None

    async def close(self) -> None:
        """释放 HTTP 资源。"""
        await self._http.aclose()
