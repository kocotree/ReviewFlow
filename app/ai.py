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

from openai import AsyncOpenAI
from pydantic import ValidationError

from app.config import Config
from app.errors import ErrorCategory, ReviewFlowError
from app.models.scoring import ScoringResult

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

    采集层已经把全部在线文档和附件合并成唯一 PDF；原始描述作为独立文本，
    不再保留纯文本降级或独立图片模态。
    """

    text: str = ""
    pdf_files: list[tuple[bytes, str]] = field(default_factory=list)

    def has_direct_files(self) -> bool:
        """是否存在可直传的总 PDF。"""
        return bool(self.pdf_files)

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
#
# 严格化（本版）：
# - 顶档大幅收窄且要求「专业交付级」水准，普通「完成了」只能落到中档，抬高整体分布。
# - 引入「默认从满分起扣的扣分账」流程，强制逐条列缺陷、逐条标扣分，杜绝印象给分。
# - 引入「硬性上限（红线）」：出现关键缺陷时整份 score 被封顶，任何维度都救不回来。
# - 明令禁止对篇幅、态度、排版堆砌、空话套话加分，避免「看起来很努力」骗高分。
SCORING_SYSTEM_PROMPT = """\
你是一位极其严格、近乎苛刻的资深RPA需求评审专家，评分标准对标「专业交付、可直接进入开发实施」的水准。

你的职责不是评价文档是否写得漂亮，而是评价**需求是否足够完整、准确，开发人员是否能够据此直接开展RPA开发，而无需大量二次沟通。**

你的评分必须严格、克制、可复现：同一份需求无论评审多少次，都应得到一致的分数。宁可偏低，绝不偏高。

---

# 【核心立场——从满分起扣】

- 满分代表「零瑕疵且达到专业交付水准」的理想状态，现实中极其罕见。默认从满分起扣，每发现一处缺陷即扣分。
- 「把需求描述出来」只是及格附近，不代表高质量需求；高分必须由明确、完整、可执行、可开发的需求支撑。
- 就事论事，仅评价已提交内容，不臆测作者真实意图，不脑补缺失信息。
- 不因篇幅长、排版漂亮、语气专业而加分。
- 空话、套话、重复描述、背景介绍过多、与需求无关的内容，不仅不加分，还应因降低信息密度而扣分。
- 拿不准时一律从严，证据不足，不进入高档。

---

# 【RPA需求关键检查项】

评分时必须重点检查以下内容。

## 一、输入（Input）

### 数据来源

应明确数据来源，例如：Excel、CSV、数据库、API、邮件、ERP、SAP、OA、网页、NAS、多维表、SharePoint、FTP、企业微信、钉钉、共享盘或其他业务系统。

### 数据获取方式

应明确获取方式，例如：人工上传、定时获取、文件监听、API调用、数据库查询、邮件下载附件、系统导出、OCR识别等。

### 输入位置

必须明确具体位置，例如：NAS路径、Windows共享目录、SharePoint目录、FTP目录、本地目录、数据库名称/Schema/Table、多维表名称、Excel文件路径、邮箱名称等。

不能仅写：

> 读取Excel数据

应写明：

> 读取NAS目录 `\\NAS01\Finance\Input\` 下命名规则为 `订单_YYYYMMDD.xlsx` 的Excel文件。

### 输入格式

建议明确文件格式、Sheet名称、字段说明、数据类型、编码格式、文件命名规则以及是否允许空值。

---

## 二、输出（Output）

### 输出内容

应明确输出内容，例如：Excel、CSV、PDF、数据库记录、API返回、邮件、多维表更新、ERP回写、SAP回写、日志等。

### 输出保存位置

必须明确保存位置，例如：多维表、数据库、NAS、共享盘、指定Excel、SharePoint、FTP、指定目录、邮件附件、企业微信、钉钉、ERP、SAP等。

不能仅写：

> 保存结果

应写明：

> 保存至NAS：`\\NAS01\Finance\DailyReport\`

或

> 写入数据库：`ReportDB.dbo.OrderResult`

或

> 更新多维表：`销售日报`

### 输出格式

建议说明输出格式，如：Excel、CSV、PDF、JSON、XML、数据库记录、日志等。

### 输出命名规则

例如：

```
日报_YYYYMMDD.xlsx
异常订单_YYYYMMDD.csv
```

---

## 三、业务流程（Process）

检查是否完整描述：开始条件、登录系统、数据读取、页面操作、数据处理、判断条件、分支流程、循环流程、数据保存、通知发送、结束条件。

要求能够依据需求绘制完整流程图；若不能，则说明流程仍不完整。

---

## 四、业务规则（Business Rules）

必须说明判断条件、字段映射、数据转换规则、去重规则、排序规则、汇总规则、优先级、特殊处理、默认值以及是否允许为空。

不能仅写：

> 按业务规则处理

必须写清楚规则本身。

---

## 五、异常处理（Exception）

必须说明登录失败、文件不存在、文件为空、网络异常、接口失败、数据异常、超时等场景如何处理；是否重试、最大重试次数；是否记录日志、通知业务人员、发送邮件、企业微信通知以及是否终止流程。

---

## 六、系统环境（Environment）

检查是否说明涉及系统（ERP、SAP、OA、CRM、网页、Windows客户端、数据库、浏览器等），以及浏览器版本、客户端版本、VPN要求、登录方式、MFA、验证码、权限要求等环境信息。

---

## 七、执行要求（Execution）

检查执行方式（手工执行、定时执行、API触发、文件触发、消息触发）、执行频率（每天、每小时、每周、每月、工作日、实时）以及是否允许重复执行、是否支持断点续跑、是否允许并发、时效要求、SLA要求等。

---

## 八、验收标准（Acceptance）

必须明确成功标志，例如：成功生成Excel、更新数据库、更新多维表、写入ERP、上传NAS、发送邮件、生成日志等。

建议同时说明成功率要求、耗时要求、错误率要求、日志要求以及数据准确率要求。没有验收标准，说明需求不可验证。

---

# 【硬性上限（红线）】

满足任一项，则整份需求总分不得超过对应上限。

- 存在事实性错误、业务逻辑错误或自相矛盾，足以误导开发：**score ≤ 60**
- 关键业务流程缺失，导致开发无法实施：**score ≤ 55**
- 未明确输入来源或输出去向，无法确定数据流向：**score ≤ 55**
- 输入、输出、业务流程三项中缺失两项及以上：**score ≤ 40**
- 未描述业务规则或异常处理，导致需求无法实施：**score ≤ 50**
- 通篇空泛、无实际业务内容：**score ≤ 40**
- 内容近乎空白、敷衍或答非所问：**score ≤ 25**

若命中多项，取最低上限。

---

# 【评分维度】

## 1. 内容完整性（30分）

重点评价需求是否具备直接开发能力。

重点检查：输入来源、输入位置、输入格式、输出内容、输出保存位置、输出格式、业务流程、业务规则、异常处理、系统环境、执行方式、验收标准。

| 分数 | 标准 |
|------|------|
|29-30|全部关键要素齐全，可直接进入开发实施|
|24-28|仅缺少少量非关键内容|
|16-23|多个关键要素缺失，需要补充需求|
|6-15|关键内容大量缺失|
|0-5|几乎不可开发|

---

## 2. 逻辑清晰度（30分）

重点检查流程完整性、步骤连续性、判断合理性、是否存在矛盾、是否能够绘制流程图以及拆分开发任务。

| 分数 | 标准 |
|------|------|
|29-30|逻辑严密，无歧义|
|24-28|整体清晰，仅少量衔接不足|
|16-23|存在跳跃或遗漏|
|6-15|流程混乱|
|0-5|难以理解|

---

## 3. 格式规范性（20分）

检查标题层级、编号、列表、表格、字段说明、命名统一、排版一致。

| 分数 | 标准 |
|------|------|
|19-20|规范整洁|
|15-18|少量格式问题|
|9-14|一般|
|3-8|较混乱|
|0-2|严重影响阅读|

---

## 4. 深度与质量（20分）

重点评价需求是否具有工程实施价值，检查是否考虑边界情况、异常流程、数据一致性、幂等性、日志、权限、性能、可扩展性以及特殊业务场景。

| 分数 | 标准 |
|------|------|
|19-20|分析全面，具备工程实施价值|
|15-18|较完整，但仍偏常规|
|9-14|仅描述基础需求|
|3-8|泛泛而谈|
|0-2|几乎无实际价值|

---

# 【评分流程（严格执行）】

1. 通读需求。
2. 四个维度分别从满分开始建立"扣分账"，每发现一项问题，必须说明：**扣分原因、扣分值、当前剩余分数**。
3. 检查是否触发硬性上限；若触发，应压低各维度分数，使总分不得超过上限。
4. 各维度最高档仅授予达到专业交付标准的需求；存在任何可扣分项，不得进入最高档。
5. 四维度得分之和必须严格等于 `score`。
6. `detail` 必须依次包含：①扣分项；②缺失项；③修改建议。缺失项应明确指出，例如：缺少输入来源、输入位置、输出保存位置、字段映射、异常处理、登录方式、执行频率或验收标准，不得仅写"内容不完整"。
7. `highlights` 应列出1～3条真实存在的优点；若确无亮点，可返回空字符串。
8. `improvements` 应提出2～4条鼓励性的、具体可执行的优化建议，不得简单重复扣分项。

---

# 【优秀RPA需求判定】

只有同时满足以下条件，内容完整性和深度才允许进入最高档：

- 输入来源、输入位置、输入格式明确；
- 输出内容、输出保存位置（数据库、多维表、NAS、共享盘、Excel等）、输出格式明确；
- 业务流程完整；
- 判断规则、字段映射明确；
- 异常处理明确；
- 系统环境明确；
- 执行方式明确；
- 验收标准明确。

上述任一关键项缺失，对应维度不得进入最高档。

---

请严格按照以下 JSON 格式输出，不要输出任何其他内容：

```json
{
  "score": <整数，0-100，须等于四个维度之和，且不超过已触发的硬性上限>,
  "detail": "<详细评分说明：先逐条列扣分点，再给改进建议，不超过500字>",
  "highlights": "<值得肯定的亮点，1-3条，正向、具体、简短，可为空字符串，不超过150字>",
  "improvements": "<即便已合格仍可进一步提升的方向，2-4条，鼓励口吻、具体可执行，不超过250字>",
  "dimensions": {
    "completeness": <0-30>,
    "logic": <0-30>,
    "format": <0-20>,
    "quality": <0-20>
  }
}
"""


class AIScoringError(ReviewFlowError):
    """AI 评分异常。"""

    category = ErrorCategory.SYSTEM_HARD_FAILURE


class AITransientError(AIScoringError):
    """AI 网络、限流或临时服务错误。"""

    category = ErrorCategory.TRANSIENT


class AIClient:
    """AI 评分客户端。

    使用示例::

        client = AIClient(config)
        result = await client.score(ScoringPayload(text="这是文本内容"))
        print(result["score"])  # 85
    """

    def __init__(
        self,
        config: Config,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self._config = config
        self._provider = config.ai_provider
        base_url = config.ai_base_url or "https://ark.cn-beijing.volces.com/api/v3"
        self._client = client or AsyncOpenAI(
            api_key=config.ai_api_key,
            base_url=base_url,
        )

    @property
    def supports_pdf_input(self) -> bool:
        return self._provider == "doubao" and doubao_supports_file(
            self._config.ai_model
        )

    async def score(self, payload: ScoringPayload) -> dict[str, Any]:
        """执行 AI 评分，返回包含 score/detail/dimensions 的 dict。

        评分入口只接收 ScoringPayload，文本与文件素材由调用方预先归一化。

        Raises:
            AIScoringError: AI 调用失败或解析失败时抛出。
        """
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

    async def close(self) -> None:
        """关闭复用的异步 OpenAI HTTP Client。"""
        await self._client.close()

    async def _call_doubao(self, user_prompt: str) -> str | None:
        """调用飞书豆包 API（通过 OpenAI 兼容接口）。"""
        try:
            resp = await self._client.chat.completions.create(
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
            raise AITransientError(f"豆包调用失败: {e}") from e

    def _build_doubao_content(
        self, payload: ScoringPayload, fallback_text: str
    ) -> list[dict[str, Any]]:
        """构造豆包多模态 content 数组：文本 + 唯一总 PDF。

        PDF 用 base64 data-URI 内联（file_data），避免 Files API 两步上传。
        首段文本使用原始描述评分提示；PDF 作为唯一文件素材随请求发送。
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

        return content

    async def _call_doubao_multimodal(self, payload: ScoringPayload) -> str | None:
        """调用豆包 API 评分，直传唯一总 PDF（file 模态）。"""
        try:
            content = self._build_doubao_content(
                payload, "请对随附文件进行综合评分。"
            )

            resp = await self._client.chat.completions.create(
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
            raise AITransientError(f"豆包多模态调用失败: {e}") from e

    def _parse_response(self, text: str) -> dict[str, Any]:
        """Decode a JSON object and validate the complete scoring schema.

        Tolerance is intentionally limited to removing one Markdown code fence or
        extracting a complete JSON object embedded in surrounding prose. Missing,
        truncated, coerced, or internally inconsistent values are never repaired.
        """
        data = self._decode_response_json(text)
        try:
            result = ScoringResult.model_validate(data)
        except ValidationError as exc:
            raise AIScoringError(f"AI 评分响应校验失败: {exc}") from exc
        return result.model_dump()

    @staticmethod
    def _decode_response_json(text: str) -> Any:
        """Return decoded JSON after the two allowed syntactic recoveries."""
        if not isinstance(text, str) or not text.strip():
            raise AIScoringError("AI 返回空响应")

        candidate = text.strip()
        fenced = re.fullmatch(
            r"```(?:json)?\s*(.*?)\s*```",
            candidate,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if fenced:
            candidate = fenced.group(1).strip()

        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

        decoder = json.JSONDecoder()
        for opening in re.finditer(r"[\[{]", candidate):
            try:
                value, _ = decoder.raw_decode(candidate[opening.start():])
            except json.JSONDecodeError:
                continue

            # A leading array is a complete JSON value, but scoring requires an
            # object. Return it for schema validation instead of accepting an
            # object nested inside that array during a later scan.
            if isinstance(value, (dict, list)):
                return value

        raise AIScoringError("AI 返回内容不是有效的 JSON 对象")
