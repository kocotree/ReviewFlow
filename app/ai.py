"""AI 评分调用封装。

当前仅支持豆包 provider（doubao，通过 OpenAI 兼容接口，支持 PDF/图片文件模态直传）。
其余 provider（openai/claude/deepseek）已下线，后续如需可按 score() 内的分派补回。

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
#
# 评分一致性设计：
# - 采用「先扣分、后给分」的严格立场：满分是「零瑕疵」的假设，任何缺陷都要扣分。
# - 每个维度给出明确的分档锚点（评分标尺），压缩模型的主观发挥空间，
#   使同一份内容在多次调用间得到稳定一致的分数。
# - 要求模型在打分前逐维度对照标尺，避免凭整体印象拍脑袋给分。
# - 配合 config.ai_temperature（默认 0）的确定性采样，进一步提高可复现性。
SCORING_SYSTEM_PROMPT = """\
你是一位极其严格、专业且客观的内容评审专家。你的评分必须严格、可复现：
同一份内容无论评审多少次，都应得到一致的分数。

【核心原则】
- 严格扣分制：以满分为「零瑕疵」的理想状态，发现任何缺陷都必须扣分，绝不放水。
- 就事论事：只依据提交内容本身评价，不臆测未提供的信息，不因内容篇幅长而加分。
- 对照标尺：逐维度对照下方评分标尺客观定档，禁止凭整体印象随意给分。
- 拿不准时从严：证据不足以支撑高档时，一律归入较低档。

【评分维度与分档标尺（满分 100 分）】

1. 内容完整性（30 分）：信息是否完整、要素是否齐全
   - 27-30：要素齐全、信息充分，无明显缺失
   - 21-26：要素基本齐全，个别信息缺失或略显单薄
   - 12-20：存在明显缺失，关键要素不全
   - 0-11：信息严重不足、要素大面积缺失

2. 逻辑清晰度（30 分）：表达是否清晰、逻辑是否连贯
   - 27-30：结构清晰、逻辑严密、论证连贯
   - 21-26：逻辑基本清晰，个别环节衔接不畅
   - 12-20：逻辑混乱或跳跃，存在明显断层
   - 0-11：思路不清、前后矛盾、难以理解

3. 格式规范性（20 分）：格式是否符合规范、排版是否整洁
   - 18-20：格式规范、排版整洁、层次分明
   - 14-17：格式基本规范，存在少量瑕疵
   - 8-13：格式较随意、排版混乱
   - 0-7：几乎无格式可言、严重影响阅读

4. 深度与质量（20 分）：内容是否有深度、是否具备实用价值
   - 18-20：有独到见解、分析深入、实用价值高
   - 14-17：有一定深度，但停留在常规层面
   - 8-13：内容浅显、多为泛泛而谈
   - 0-7：空洞无物、无实际价值

【评分流程】
1. 逐一对照上述标尺，为每个维度客观定档并给出该维度得分。
2. 四个维度得分之和即为 score，必须与 dimensions 完全一致。
3. 在 detail 中先指出主要扣分点，再给出具体、可执行的改进建议。

请严格按照以下 JSON 格式输出，不要输出任何其他内容：
{
  "score": <整数，0-100，须等于四个维度之和>,
  "detail": "<详细评分说明：先列扣分点，再给改进建议，不超过500字>",
  "dimensions": {
    "completeness": <0-30>,
    "logic": <0-30>,
    "format": <0-20>,
    "quality": <0-20>
  }
}"""

# 纯文档转写系统提示（不评分）。
# 评分与转写已拆分为两次独立调用：评分走 SCORING_SYSTEM_PROMPT（响应短、不会被
# 转写文本撑长而截断）；转写单独用本提示，把随附文件/图片忠实转成文字，用于回填
# 「文档内容缓存」字段（飞书 raw_content 接口只给图片占位符，拿不到图像内容）。
# 转写直接输出纯文本，不再包裹 JSON，避免截断导致解析失败。
TRANSCRIBE_SYSTEM_PROMPT = """\
你是一位专业的文档转写助手。请对用户随附的文件/图片做一次忠实的文字转写。

要求：
- 忠实转写文件/图片中的文字，尽量保留原有结构与层次；
- 对其中的图片、图表、表格，用简洁中文说明其内容与要点，禁止输出“[图片]”这类占位符；
- 只转写、不做任何评价或打分；
- 直接输出转写正文纯文本，不要输出 JSON、不要加多余前后缀，整体控制在 4000 字以内。"""

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
    ) -> dict[str, Any]:
        """执行 AI 评分，返回包含 score/detail/dimensions 的 dict。

        推荐传入 ScoringPayload（支持文件/图片直传）；也兼容旧的三段
        text 关键字参数（纯文本评分）。

        评分只做评分，不再顺带转写文档——文档转写已拆分为独立的
        transcribe() 调用，避免转写文本把评分响应撑长而被 max_tokens 截断。

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

        # 当前仅支持豆包 provider（其余 provider 已下线，后续如需再按此分派补回）。
        if self._provider != "doubao":
            raise AIScoringError(f"不支持的 AI Provider: {self._provider}")

        # file-capable 型号 + 确有可直传文件时走多模态直传，否则走纯文本
        if payload.has_direct_files() and doubao_supports_file(self._config.ai_model):
            raw_response = await self._call_doubao_multimodal(payload)
        else:
            raw_response = await self._call_doubao(payload.text)

        if not raw_response:
            raise AIScoringError("AI 返回空响应")

        return self._parse_response(raw_response)

    async def transcribe(self, payload: ScoringPayload) -> str | None:
        """将随附 PDF/图片转写为纯文本，用于回填「文档内容缓存」字段。

        仅在豆包 + file-capable 型号 + 确有可直传文件时走视觉转写；其余情况
        返回 None，由调用方回退到纯文本兜底（在线文档 raw_content + 附件抽取文本）。
        与评分完全独立：转写失败或截断都不影响评分结果。
        """
        use_direct = (
            self._provider == "doubao"
            and payload.has_direct_files()
            and doubao_supports_file(self._config.ai_model)
        )
        if not use_direct:
            return None
        return await self._call_doubao_transcribe(payload)

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
                temperature=self._config.ai_temperature,
                max_tokens=self._config.ai_score_max_tokens,
            )
            return resp.choices[0].message.content
        except Exception as e:
            logger.error("豆包 调用失败: %s", e)
            raise AIScoringError(f"豆包调用失败: {e}") from e

    def _build_doubao_content(
        self, payload: ScoringPayload, fallback_text: str
    ) -> list[dict[str, Any]]:
        """构造豆包多模态 content 数组：文本 + 若干 PDF file 部件 + 若干图片部件。

        PDF 用 base64 data-URI 内联（file_data），避免 Files API 两步上传。
        评分与转写共用此逻辑，仅首段文本提示词不同。
        """
        content: list[dict[str, Any]] = [
            {"type": "text", "text": payload.text or fallback_text}
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

        return content

    async def _call_doubao_multimodal(self, payload: ScoringPayload) -> str | None:
        """调用豆包 API 评分，直传 PDF（file 模态）与图片（image_url 模态）。

        仅评分、不转写——文档转写已拆分为独立的 _call_doubao_transcribe，
        故此处响应短、max_tokens 精简，不会被转写文本撑长而截断。
        """
        try:
            from openai import AsyncOpenAI

            base_url = self._config.ai_base_url or "https://ark.cn-beijing.volces.com/api/v3"
            client = AsyncOpenAI(
                api_key=self._config.ai_api_key,
                base_url=base_url,
            )

            content = self._build_doubao_content(
                payload, "请对随附文件进行综合评分。"
            )

            resp = await client.chat.completions.create(
                model=self._config.ai_model,
                messages=[
                    {"role": "system", "content": SCORING_SYSTEM_PROMPT},
                    {"role": "user", "content": content},
                ],
                temperature=self._config.ai_temperature,
                max_tokens=self._config.ai_score_max_tokens,
            )
            return resp.choices[0].message.content
        except Exception as e:
            logger.error("豆包多模态调用失败: %s", e)
            raise AIScoringError(f"豆包多模态调用失败: {e}") from e

    async def _call_doubao_transcribe(self, payload: ScoringPayload) -> str | None:
        """调用豆包 API 转写直传 PDF/图片，返回纯文本转写正文（不评分）。

        独立于评分调用：用 TRANSCRIBE_SYSTEM_PROMPT，直接输出纯文本、放宽
        max_tokens；即便被截断也只影响缓存回填，不会污染评分。
        """
        try:
            from openai import AsyncOpenAI

            base_url = self._config.ai_base_url or "https://ark.cn-beijing.volces.com/api/v3"
            client = AsyncOpenAI(
                api_key=self._config.ai_api_key,
                base_url=base_url,
            )

            content = self._build_doubao_content(
                payload, "请对随附文件/图片进行忠实文字转写。"
            )

            resp = await client.chat.completions.create(
                model=self._config.ai_model,
                messages=[
                    {"role": "system", "content": TRANSCRIBE_SYSTEM_PROMPT},
                    {"role": "user", "content": content},
                ],
                temperature=self._config.ai_temperature,
                max_tokens=self._config.ai_transcribe_max_tokens,
            )
            return resp.choices[0].message.content
        except Exception as e:
            logger.error("豆包转写调用失败: %s", e)
            raise AIScoringError(f"豆包转写调用失败: {e}") from e

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

        # 方式 3: 兜底 —— 正则逐字段提取。
        # 常见触发场景：content_text（文档转写）位于 JSON 末尾，响应被 max_tokens
        # 截断导致整体 JSON 非法，但前半段的 score / detail / dimensions 通常完整可读，
        # 故此处逐字段正则捞回，避免维度分被硬编码填 0、造成 score 与 dimensions 不一致。
        logger.warning("JSON 解析失败，使用正则兜底提取。原始响应: %s", text[:500])

        score_match = re.search(r'"score"[\s:]*(\d+)', text)
        score = int(score_match.group(1)) if score_match else 0
        score = max(0, min(100, score))  # 限制在 0-100

        detail_match = re.search(r'"detail"[\s:]*"([^"]*)"', text)
        detail = detail_match.group(1) if detail_match else text[:500]

        def _grab_dim(name: str) -> int:
            m = re.search(rf'"{name}"[\s:]*(\d+)', text)
            return int(m.group(1)) if m else 0

        return {
            "score": score,
            "detail": detail,
            "dimensions": {
                "completeness": _grab_dim("completeness"),
                "logic": _grab_dim("logic"),
                "format": _grab_dim("format"),
                "quality": _grab_dim("quality"),
            },
            "_parse_fallback": True,
        }
