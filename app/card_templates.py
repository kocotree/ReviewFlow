"""纯卡片模板与统一评分动作构建。"""

from __future__ import annotations

from typing import Any

from app.card_action import ACTION_RESCORE, build_score_action_value


def record_url(base_url: str, app_token: str, table_id: str, record_id: str) -> str:
    if not base_url or not app_token or not table_id:
        return ""
    return f"{base_url}/base/{app_token}?table={table_id}&record={record_id}"


def rescore_value(
    app_token: str,
    table_id: str,
    record_id: str,
) -> dict[str, str] | None:
    if not app_token or not table_id or not record_id:
        return None
    return build_score_action_value(
        action=ACTION_RESCORE,
        app_token=app_token,
        table_id=table_id,
        record_id=record_id,
    )


def build_failed_card(
    *,
    score: int,
    detail: str,
    threshold: int,
    record_url: str,
    action_value: dict[str, str] | None,
) -> dict[str, Any]:
    elements: list[dict[str, Any]] = [
        markdown(
            "您的提交 **未通过** AI 自动评审。请完成全部修改后，"
            "点击下方按钮显式重新评分。"
        ),
        {"tag": "hr"},
        markdown(
            f"**当前评分**: {score} 分（通过线: {threshold} 分）\n"
            f"**改进建议**:\n{detail}"
        ),
    ]
    action = action_element(record_url=record_url, action_value=action_value)
    if action:
        elements.append(action)
    return card("⚠️ AI 评审未通过", "red", elements)


def build_passed_card(
    score: int,
    threshold: int,
    highlights: str,
    improvements: str,
) -> dict[str, Any]:
    elements = [
        markdown(
            f"您的提交已通过 AI 自动评审。\n**评分**: {score} 分（通过线: {threshold} 分）"
        )
    ]
    if highlights.strip():
        elements.append(markdown(f"**✨ 做得好的地方**:\n{highlights.strip()}"))
    if improvements.strip():
        elements.append(markdown(f"**🚀 还可以更好**:\n{improvements.strip()}"))
    return card("✅ AI 评审已通过", "green", elements)


def build_rejected_card(score: int, detail: str, rounds: int) -> dict[str, Any]:
    return card(
        "🚫 已达到修改上限",
        "red",
        [
            markdown(
                f"本次为第 **{rounds}** 次修改，已达到上限并立即驳回。\n\n"
                f"**最终评分**: {score} 分\n**评审意见**: {detail}\n\n"
                "请联系开发者或管理员获取帮助。"
            )
        ],
    )


def build_material_error_card(
    *,
    kind: str,
    reason: str,
    problems: list[tuple[str, str]],
    record_url: str,
    action_value: dict[str, str] | None,
) -> dict[str, Any]:
    titles = {
        "no_file": "⚠️ 请上传需求文档",
        "unsupported": "⚠️ 附件格式不支持",
        "damaged": "⚠️ 材料损坏或无法解析",
        "limit": "⚠️ 材料超过资源限制",
    }
    descriptions = {
        "no_file": "当前只有原始描述，尚无可评分的在线需求文档或附件。",
        "unsupported": "请删除或替换全部不支持的文件后再重新评分。",
        "damaged": "请替换损坏、加密或无法解析的材料后再重新评分。",
        "limit": "请减少材料数量、大小或页数后再重新评分。",
    }
    lines = [f"- **{name}**：{problem_reason}" for name, problem_reason in problems]
    content = descriptions.get(kind, reason)
    if lines:
        content += "\n\n**问题材料**:\n" + "\n".join(lines)
    elif reason:
        content += f"\n\n**原因**: {reason}"
    elements = [markdown(content)]
    action = action_element(record_url=record_url, action_value=action_value)
    if action:
        elements.append(action)
    return card(titles.get(kind, "⚠️ 材料暂不可评审"), "red", elements)


def build_error_card(
    record_id: str,
    error: str,
    record_url: str,
    action_value: dict[str, str] | None,
) -> dict[str, Any]:
    elements = [
        markdown(
            "一条记录评分失败并进入 **评分异常**，仅管理员可重试。\n\n"
            f"**记录 ID**: {record_id}\n**错误信息**: {error}"
        )
    ]
    action = action_element(
        record_url=record_url,
        action_value=action_value,
        action_text="重试评分",
    )
    if action:
        elements.append(action)
    return card("🛑 评分异常告警", "red", elements)


def build_circuit_breaker_card(
    *,
    record_id: str,
    observed_count: int,
    window_minutes: int,
    max_messages: int,
) -> dict[str, Any]:
    return card(
        "🚨 卡片发送回路已熔断",
        "red",
        [
            markdown(
                f"记录 **{record_id}** 在 {window_minutes} 分钟窗口内已发送 "
                f"{observed_count} 张卡片（上限 {max_messages}），后续卡片已停发。\n\n"
                "该保护只作用于发送侧，请检查事件回声或状态机循环。"
            )
        ],
    )


def action_element(
    *,
    record_url: str = "",
    action_value: dict[str, str] | None = None,
    action_text: str = "重新评分",
) -> dict[str, Any] | None:
    actions: list[dict[str, Any]] = []
    if record_url:
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "查看并修改"},
                "url": record_url,
                "type": "default",
            }
        )
    if action_value:
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": action_text},
                "value": action_value,
                "type": "primary",
            }
        )
    return {"tag": "action", "actions": actions} if actions else None


def markdown(content: str) -> dict[str, Any]:
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def card(title: str, template: str, elements: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        },
        "elements": elements,
    }
