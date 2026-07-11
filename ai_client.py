"""AI 评分调用封装。

支持多个 AI Provider:
- openai: OpenAI GPT 系列
- claude: Anthropic Claude 系列
- doubao: 飞书豆包（通过 OpenAI 兼容接口）

内部处理 JSON 解析容错、重试等。
"""

import json
import logging
import re
from typing import Any

from config import Config

logger = logging.getLogger(__name__)

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
        text_content: str = "",
        doc_content: str = "",
        attachment_content: str = "",
    ) -> dict[str, Any]:
        """执行 AI 评分，返回包含 score/detail/dimensions 的 dict。

        Raises:
            AIScoringError: AI 调用失败或解析失败时抛出。
        """
        user_prompt = SCORING_USER_PROMPT_TEMPLATE.format(
            text_content=text_content or "（无）",
            doc_content=doc_content or "（无）",
            attachment_content=attachment_content or "（无）",
        )

        raw_response: str | None = None

        if self._provider == "openai":
            raw_response = await self._call_openai(user_prompt)
        elif self._provider == "claude":
            raw_response = await self._call_claude(user_prompt)
        elif self._provider == "doubao":
            raw_response = await self._call_doubao(user_prompt)
        elif self._provider == "deepseek":
            raw_response = await self._call_deepseek(user_prompt)
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
