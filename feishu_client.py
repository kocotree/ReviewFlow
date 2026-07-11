"""飞书 Open API 封装 —— 统一管理所有飞书 API 调用。

提供以下能力：
- 租户访问令牌自动获取与刷新
- 多维表格记录读写
- 飞书文档纯文本内容获取
- 机器人消息发送
"""

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
from lark_oapi.api.im.v1 import CreateMessageRequest
from lark_oapi.api.im.v1.model.create_message_request_body import (
    CreateMessageRequestBody,
)
from lark_oapi.core.token import TokenManager

from config import Config

logger = logging.getLogger(__name__)


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

    # ---- 消息通知 ----

    async def send_text_message(
        self, open_id: str, text: str
    ) -> bool:
        """发送文本消息给指定用户。"""
        content = json.dumps({"text": text})
        body = CreateMessageRequestBody.builder() \
            .receive_id(open_id) \
            .msg_type("text") \
            .content(content) \
            .build()
        req = CreateMessageRequest.builder() \
            .receive_id_type("open_id") \
            .request_body(body) \
            .build()
        resp = self._client.im.v1.message.create(req)

        if not resp.success():
            logger.error("发送消息失败: code=%s msg=%s", resp.code, resp.msg)
            return False
        return True

    async def send_card_message(
        self, open_id: str, card_json: dict[str, Any]
    ) -> bool:
        """发送卡片消息给指定用户。card_json 为飞书消息卡片 JSON。"""
        content = json.dumps(card_json, ensure_ascii=False)
        body = CreateMessageRequestBody.builder() \
            .receive_id(open_id) \
            .msg_type("interactive") \
            .content(content) \
            .build()
        req = CreateMessageRequest.builder() \
            .receive_id_type("open_id") \
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
