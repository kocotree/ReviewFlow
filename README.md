# RPA Score — 飞书多维表格 AI 评分服务

基于飞书开放平台构建的自动化评审服务。用户通过收集表单提交内容后，服务自动调用 AI 对文本、文档链接和附件进行综合评分，评分不达标时通过飞书机器人通知提报人修改，形成闭环评审流程。

## 核心流程

```
收集表单提交 → 记录写入多维表格
  → Webhook 触发评分服务
  → 收集内容（文本 + 飞书文档内容 + 附件文本）
  → AI 综合评分 → 分数写回表格
  → 判断：
      ✅ ≥ 阈值 → 标记"已通过"
      ❌ < 阈值 → 标记"未通过" → 机器人通知提报人修改
      🔄 修改后重新评分 → 直到通过或达到最大修改轮次
```

## 项目结构

```
.
├── main.py              # FastAPI 入口 + Webhook 端点
├── orchestrator.py      # 评分编排核心（状态机 + 内容收集 + 通知调度）
├── feishu_client.py     # 飞书 Open API 封装（Bitable / Doc / IM）
├── ai_client.py         # AI 评分调用（支持 DeepSeek / OpenAI / Claude / 豆包）
├── document_parser.py   # 文档文本提取（PDF / Word / Markdown / 飞书文档链接）
├── notification.py      # 飞书消息卡片 + 通知频率控制
├── config.py            # 配置管理（环境变量）
├── field_mapping.py     # 多维表格字段名映射（按需调整）
├── requirements.txt     # Python 依赖
├── Dockerfile           # 容器构建
├── docker-compose.yml   # 容器部署
├── .env.example         # 环境变量模板
└── .python-version      # Python 版本锁定 (3.12)
```

## 快速开始

### 1. 环境准备

- Python 3.12+
- [飞书企业自建应用](https://open.feishu.cn/app)（需具备以下权限）

| 权限 | 用途 |
|------|------|
| `bitable:app` | 读写多维表格记录 |
| `docs:event:subscribe` | 订阅多维表格记录变更事件 |
| `docx:document:readonly` | 读取飞书在线文档内容 |
| `drive:drive` | 附件下载 |
| `im:message:send_as_bot` | 以机器人身份发送通知 |
| `base:app:create` | 通过 API 创建多维表格（可选） |

### 2. 安装

```bash
# 安装 uv（推荐）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 创建虚拟环境并安装依赖
uv venv --python 3.12
source .venv/bin/activate
uv pip install -r requirements.txt
```

### 3. 配置

```bash
cp .env.example .env
```

编辑 `.env`，填入必要配置：

| 变量 | 说明 |
|------|------|
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 飞书应用凭证 |
| `BITABLE_APP_TOKEN` / `BITABLE_TABLE_ID` | 多维表格 ID（从表格 URL 提取） |
| `WEBHOOK_VERIFICATION_TOKEN` / `WEBHOOK_ENCRYPT_KEY` | 事件订阅安全配置 |
| `AI_PROVIDER` / `AI_API_KEY` / `AI_MODEL` | AI 服务配置 |
| `SCORE_THRESHOLD` | 评分通过线（默认 70） |
| `MAX_REVISION_ROUNDS` | 最大修改轮次（默认 5） |

### 4. 多维表格字段

在飞书多维表格中创建以下字段（字段名可通过 `field_mapping.py` 自定义）：

**用户输入字段：**
- `提报人`（人员）— 收集表单自动填充
- `原始描述`（多行文本）
- `需求文档`（超链接）— 飞书在线文档链接
- `需求附件`（附件）— Word / PDF / Markdown

**AI 输出字段：**
- `AI评分`（数字）
- `AI评分详情`（多行文本）
- `AI评分时间`（日期）

**控制字段：**
- `评分状态`（单选：待评分 / 评分中 / 已通过 / 未通过 / 已驳回）
- `修改轮次`（数字）

### 5. 启动

```bash
source .venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8000 --log-level info
```

飞书要求回调地址为 HTTPS 公网地址，本地开发可使用 ngrok：

```bash
ngrok http 8000
# 将输出的 https://xxx.ngrok-free.dev 配置为飞书事件订阅 URL
```

### 6. 配置飞书事件订阅

1. 飞书开放平台 → 你的应用 → 事件订阅
2. **请求 URL**：`https://your-domain/webhook/event`
3. 添加事件：`drive.file.bitable_record_changed_v1`
4. 发布应用版本

### 7. 订阅多维表格事件（必须）

**仅添加事件类型还不够，必须调用订阅 API：**

```python
from lark_oapi.api.drive.v1 import SubscribeFileRequest

req = SubscribeFileRequest.builder() \
    .file_token("<你的多维表格 App Token>") \
    .file_type("bitable") \
    .build()
client.drive.v1.file.subscribe(req)
```

也可以使用飞书 CLI：
```bash
lark-cli api POST "/open-apis/drive/v1/files/<BITABLE_APP_TOKEN>/subscribe?file_type=bitable" --as bot
```

### 8. 授权应用到多维表格

打开多维表格 → 右上角 `...` → 更多 → 添加文档应用 → 搜索并添加你的应用。

## Docker 部署

```bash
docker-compose up -d
```

## 状态机

```
待评分 → 评分中 → 已通过（结束）
                 → 未通过 → 等待用户修改
                           → 重新触发评分 → 循环
                           → 超过最大修改轮次 → 已驳回
```

服务只在记录状态为「待评分」或「未通过」时执行评分，其他状态（已通过、已驳回、评分中）忽略，避免重复处理和消息骚扰。

## AI 支持

| Provider | 配置值 |
|----------|--------|
| DeepSeek | `deepseek` |
| OpenAI | `openai` |
| Claude | `claude` |
| 豆包 | `doubao` |

评分 Prompt 在 `ai_client.py` 的 `SCORING_SYSTEM_PROMPT` 中定义，可按需修改评分维度和标准。

## 通知控制

- 同一记录两次通知之间默认间隔 60 分钟
- 同一用户每天最多 3 次通知
- 评分通过时发送一次性通过通知
- 超过最大修改轮次后通知用户并驳回
