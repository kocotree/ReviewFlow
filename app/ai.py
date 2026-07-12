"""AI 评分调用封装。

支持多个 AI Provider:
- openai: OpenAI GPT 系列
- claude: Anthropic Claude 系列
- doubao: 飞书豆包（通过 OpenAI 兼容接口）

内部处理 JSON 解析容错、重试等。
"""

import base64
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.config import Config

logger = logging.getLogger(__name__)

# 支持文件模态（PDF 直传）的豆包模型集合。
# 官方 file-输入白名单目前只列出 doubao-seed-2.0 系列（mini/lite/pro）；
# 2.1-pro 尚未被证实支持，故不默认加入 —— 若确认支持，在此追加型号前缀即可。
# 匹配采用「前缀」判断，以兼容带日期后缀的实际 endpoint（如
# doubao-seed-2-0-pro-260428）。
DOUBAO_FILE_CAPABLE_PREFIXES = (
    "doubao-seed-2-0-pro",
    "doubao-seed-2-0-lite",
    "doubao-seed-2-0-mini",
    "doubao-seed-2.0-pro",
    "doubao-seed-2.0-lite",
    "doubao-seed-2.0-mini",
)


def doubao_supports_file(model: str) -> bool:
    """判断给定豆包模型是否支持 PDF 文件模态直传。"""
    if not model:
        return False
    m = model.lower()
    return any(m.startswith(p) for p in DOUBAO_FILE_CAPABLE_PREFIXES)


@dataclass
class ScoringPayload:
    """评分素材包。

    把「三段纯文本」升级为「文本 + 一组待直传的文件/图片」，由 AIClient
    按当前 provider 能力决定走直传还是降级为纯文本。

    - text: 已拼好并截断的纯文本（文本字段 + 各类降级抽取文本），始终作为
      兜底，即使走文件直传也一并发送，确保非多模态 provider 有内容可评。
    - pdf_files: 待直传的 PDF，元素为 (bytes, 文件名)。
    - image_files: 待直传的图片，元素为 (bytes, mime_type)。
    """

    text: str = ""
    pdf_files: list[tuple[bytes, str]] = field(default_factory=list)
    image_files: list[tuple[bytes, str]] = field(default_factory=list)

    def has_direct_files(self) -> bool:
        """是否存在可直传的文件或图片。"""
        return bool(self.pdf_files or self.image_files)

    def is_empty(self) -> bool:
        """可用内容是否为空（无文本且无可直传文件）。"""
        return not self.text.strip() and not self.has_direct_files()

# 评分 Prompt 模板
SCORING_SYSTEM_PROMPT = """\
你是一位严格且专业的内容评审专家。你需要根据用户提交的内容进行综合评分。

评分维度（满分 100 分）：
1. 内容完整性（30 分）：信息是否完整、要素是否齐全
2. 逻辑清晰度（30 分）：表达是否清晰、逻辑是否连贯
3. 格式规范性（20 分）：格式是否符合规范、排版是否整洁
4. 深度与质量（20 分）：内容是否有深度、是否具备实用价值

请严格按照以下 JSON 格式输出，不要输出任何其他内容：
{
  "score": <整数，0-100>,
  "detail": "<详细评分说明和改进建议，不超过500字>",
  "dimensions": {
    "completeness": <0-30>,
    "logic": <0-30>,
    "format": <0-20>,
    "quality": <0-20>
  }
}"""

# 多模态评分 + 文档转写系统提示。
# 相比 SCORING_SYSTEM_PROMPT 多一个 content_text 字段：让模型对随附文件/图片
# 做一次忠实文字转写，把图片/图表/表格转成文字描述，用于回填「文档内容缓存」
# 字段（飞书 raw_content 接口只给图片占位符，拿不到图像内容）。
# content_text 放在 JSON 末尾：万一响应被 max_tokens 截断导致 JSON 非法，
# 前面的 score/detail 仍可被正则兜底救回。
MULTIMODAL_SYSTEM_PROMPT = """\
你是一位严格且专业的内容评审专家。你需要根据用户提交的内容进行综合评分，并对随附的文件/图片做一次忠实的文字转写。

评分维度（满分 100 分）：
1. 内容完整性（30 分）：信息是否完整、要素是否齐全
2. 逻辑清晰度（30 分）：表达是否清晰、逻辑是否连贯
3. 格式规范性（20 分）：格式是否符合规范、排版是否整洁
4. 深度与质量（20 分）：内容是否有深度、是否具备实用价值

关于 content_text（文档转写）：
- 忠实转写随附文件/图片中的文字，尽量保留原有结构；
- 对其中的图片、图表、表格，用简洁中文说明其内容与要点，禁止输出“[图片]”这类占位符；
- 只转写、不做评价；整体控制在 4000 字以内。

请严格按照以下 JSON 格式输出，不要输出任何其他内容：
{
  "score": <整数，0-100>,
  "detail": "<详细评分说明和改进建议，不超过500字>",
  "dimensions": {
    "completeness": <0-30>,
    "logic": <0-30>,
    "format": <0-20>,
    "quality": <0-20>
  },
  "content_text": "<随附文件/图片的忠实文字转写，含图片/图表的简要描述>"
}"""

SCORING_USER_PROMPT_TEMPLATE = """\
请对以下提交内容进行综合评分：

=== 文本内容 ===
{text_content}

=== 文档内容 ===
{doc_content}

=== 附件内容 ===
{attachment_content}

请按照 JSON 格式输出评分结果。"""


class AIScoringError(Exception):
    """AI 评分异常。"""
    pass


class AIClient:
    """AI 评分客户端。

    使用示例::

        client = AIClient(config)
        result = await client.score(
            text_content="这是文本内容",
            doc_content="这是文档内容",
            attachment_content="这是附件内容",
        )
        print(result["score"])  # 85
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._provider = config.ai_provider

    async def score(
        self,
        payload: ScoringPayload | None = None,
        *,
        text_content: str = "",
        doc_content: str = "",
        attachment_content: str = "",
        transcribe: bool = False,
    ) -> dict[str, Any]:
        """执行 AI 评分，返回包含 score/detail/dimensions 的 dict。

        推荐传入 ScoringPayload（支持文件/图片直传）；也兼容旧的三段
        text 关键字参数（纯文本评分）。

        transcribe=True 且走豆包多模态直传时，返回值额外含 content_text ——
        模型对随附文件/图片的忠实文字转写（图片转为文字描述），用于回填
        「文档内容缓存」字段。其余情况无此字段。

        Raises:
            AIScoringError: AI 调用失败或解析失败时抛出。
        """
        if payload is None:
            # 兼容旧调用：把三段文本拼成 payload 的纯文本
            payload = ScoringPayload(
                text=SCORING_USER_PROMPT_TEMPLATE.format(
                    text_content=text_content or "（无）",
                    doc_content=doc_content or "（无）",
                    attachment_content=attachment_content or "（无）",
                )
            )

        raw_response: str | None = None

        # 仅豆包 + file-capable 型号 + 确有可直传文件时，走多模态直传
        use_direct = (
            self._provider == "doubao"
            and payload.has_direct_files()
            and doubao_supports_file(self._config.ai_model)
        )

        if use_direct:
            raw_response = await self._call_doubao_multimodal(
                payload, transcribe=transcribe
            )
        elif self._provider == "openai":
            raw_response = await self._call_openai(payload.text)
        elif self._provider == "claude":
            raw_response = await self._call_claude(payload.text)
        elif self._provider == "doubao":
            raw_response = await self._call_doubao(payload.text)
        elif self._provider == "deepseek":
            raw_response = await self._call_deepseek(payload.text)
        else:
            raise AIScoringError(f"不支持的 AI Provider: {self._provider}")

        if not raw_response:
            raise AIScoringError("AI 返回空响应")

        return self._parse_response(raw_response)

    async def _call_openai(self, user_prompt: str) -> str | None:
        """调用 OpenAI GPT API。"""
        try:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(
                api_key=self._config.ai_api_key,
                base_url=self._config.ai_base_url or None,
            )
            resp = await client.chat.completions.create(
                model=self._config.ai_model,
                messages=[
                    {"role": "system", "content": SCORING_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                max_tokens=2000,
            )
            return resp.choices[0].message.content
        except Exception as e:
            logger.error("OpenAI 调用失败: %s", e)
            raise AIScoringError(f"OpenAI 调用失败: {e}") from e

    async def _call_claude(self, user_prompt: str) -> str | None:
        """调用 Anthropic Claude API。"""
        try:
            from anthropic import AsyncAnthropic

            client = AsyncAnthropic(api_key=self._config.ai_api_key)
            resp = await client.messages.create(
                model=self._config.ai_model,
                max_tokens=2000,
                system=SCORING_SYSTEM_PROMPT,
                messages=[
                    {"role": "user", "content": user_prompt},
                ],
            )
            # Claude 返回的是 ContentBlock 列表
            for block in resp.content:
                if block.type == "text":
                    return block.text
            return None
        except Exception as e:
            logger.error("Claude 调用失败: %s", e)
            raise AIScoringError(f"Claude 调用失败: {e}") from e

    async def _call_doubao(self, user_prompt: str) -> str | None:
        """调用飞书豆包 API（通过 OpenAI 兼容接口）。"""
        try:
            from openai import AsyncOpenAI

            base_url = self._config.ai_base_url or "https://ark.cn-beijing.volces.com/api/v3"
            client = AsyncOpenAI(
                api_key=self._config.ai_api_key,
                base_url=base_url,
            )
            resp = await client.chat.completions.create(
                model=self._config.ai_model or "doubao-pro-32k",
                messages=[
                    {"role": "system", "content": SCORING_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                max_tokens=2000,
            )
            return resp.choices[0].message.content
        except Exception as e:
            logger.error("豆包 调用失败: %s", e)
            raise AIScoringError(f"豆包调用失败: {e}") from e

    async def _call_doubao_multimodal(
        self, payload: ScoringPayload, *, transcribe: bool = False
    ) -> str | None:
        """调用豆包 API，直传 PDF（file 模态）与图片（image_url 模态）。

        content 为多部件数组：文本 + 若干 PDF file 部件 + 若干图片部件。
        PDF 用 base64 data-URI 内联（file_data），避免 Files API 两步上传。

        transcribe=True 时改用 MULTIMODAL_SYSTEM_PROMPT，让模型在评分之外
        额外产出 content_text 文档转写；并放宽 max_tokens 以容纳转写文本。
        """
        try:
            from openai import AsyncOpenAI

            base_url = self._config.ai_base_url or "https://ark.cn-beijing.volces.com/api/v3"
            client = AsyncOpenAI(
                api_key=self._config.ai_api_key,
                base_url=base_url,
            )

            content: list[dict[str, Any]] = [
                {"type": "text", "text": payload.text or "请对随附文件进行综合评分。"}
            ]

            for data, name in payload.pdf_files:
                b64 = base64.b64encode(data).decode("utf-8")
                content.append({
                    "type": "file",
                    "file": {
                        # 方舟要求 file_data 方式必须带 filename，否则 400
                        "filename": name or "attachment.pdf",
                        "file_data": f"data:application/pdf;base64,{b64}",
                    },
                })

            for data, mime in payload.image_files:
                b64 = base64.b64encode(data).decode("utf-8")
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                })

            resp = await client.chat.completions.create(
                model=self._config.ai_model,
                messages=[
                    {
                        "role": "system",
                        "content": MULTIMODAL_SYSTEM_PROMPT if transcribe
                        else SCORING_SYSTEM_PROMPT,
                    },
                    {"role": "user", "content": content},
                ],
                temperature=0.3,
                # 需转写时放宽上限，给 content_text 留空间；否则维持精简
                max_tokens=16000 if transcribe else 2000,
            )
            return resp.choices[0].message.content
        except Exception as e:
            logger.error("豆包多模态调用失败: %s", e)
            raise AIScoringError(f"豆包多模态调用失败: {e}") from e

    async def _call_deepseek(self, user_prompt: str) -> str | None:
        """调用 DeepSeek API（OpenAI 兼容接口）。"""
        try:
            from openai import AsyncOpenAI

            base_url = self._config.ai_base_url or "https://api.deepseek.com"
            client = AsyncOpenAI(
                api_key=self._config.ai_api_key,
                base_url=base_url,
            )
            resp = await client.chat.completions.create(
                model=self._config.ai_model or "deepseek-v4-flash",
                messages=[
                    {"role": "system", "content": SCORING_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                max_tokens=2000,
            )
            return resp.choices[0].message.content
        except Exception as e:
            logger.error("DeepSeek 调用失败: %s", e)
            raise AIScoringError(f"DeepSeek 调用失败: {e}") from e

    def _parse_response(self, text: str) -> dict[str, Any]:
        """容错式 JSON 解析，应对 AI 不严格返回 JSON 的情况。

        尝试顺序:
        1. 直接 json.loads
        2. 正则提取 JSON 块后 json.loads
        3. 正则提取 score 字段兜底
        """
        # 方式 1: 直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 方式 2: 提取最外层 JSON 对象
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass

        # 方式 3: 兜底 —— 正则提取 score 值
        logger.warning("JSON 解析失败，使用正则兜底提取。原始响应: %s", text[:500])

        score_match = re.search(r'"score"[\s:]*(\d+)', text)
        score = int(score_match.group(1)) if score_match else 0
        score = max(0, min(100, score))  # 限制在 0-100

        detail_match = re.search(r'"detail"[\s:]*"([^"]*)"', text)
        detail = detail_match.group(1) if detail_match else text[:500]

        return {
            "score": score,
            "detail": detail,
            "dimensions": {
                "completeness": 0,
                "logic": 0,
                "format": 0,
                "quality": 0,
            },
            "_parse_fallback": True,
        }
