# 飞书多维表格字段名映射

# 以下字段名必须与飞书多维表格中实际使用的字段名保持一致。
# 如果多维表格中的字段名不同，请修改以下常量。

# --- 用户输入字段 ---
FIELD_SUBMITTER = "提报人"           # 飞书人员字段
FIELD_TEXT_CONTENT = "原始描述"       # 多行文本
FIELD_DOC_LINK = "需求文档"       # 超链接
FIELD_ATTACHMENT = "需求附件"            # 附件

# --- AI 评分输出字段 ---
FIELD_AI_SCORE = "AI评分"            # 数字
FIELD_AI_SCORE_DETAIL = "AI评分详情"  # 多行文本
FIELD_AI_SCORE_TIME = "AI评分时间"    # 日期

# --- 工作流控制字段 ---
FIELD_SCORE_STATUS = "评分状态"       # 单选: 待评分/评分中/已通过/未通过/已驳回
FIELD_REVISION_ROUNDS = "修改轮次"    # 数字
FIELD_DOC_CACHE = "文档内容缓存"      # 多行文本（后端自动填充）
