# ReviewFlow

> 基于飞书开放平台的 Webhook 驱动式 AI 评审服务

<p>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.110+-009688?logo=fastapi&logoColor=white">
  <img alt="Feishu" src="https://img.shields.io/badge/飞书-Open%20API-00D6B9">
  <img alt="Doubao" src="https://img.shields.io/badge/AI-豆包%20多模态-FF6A00">
</p>

用户通过收集表单向飞书多维表格提交内容，ReviewFlow 自动收集其中的文本、在线文档和附件，交由 AI 综合打分并写回表格；未达通过线时以机器人卡片通知提报人修改，直到通过或超出最大修改轮次为止——形成一套无人值守的闭环评审流程。

支持文件模态的豆包模型下，在线文档、PDF、Word、图片都会被**直传**给模型评审（而非仅抽取纯文本），因此表格、图表、截图中的信息同样纳入评分。

默认选用豆包的原因是火山方舟中调用豆包 api 可以直接传入文件。

## 特性

- **多来源内容采集** — 文本字段 + 飞书在线文档（docx / 旧版 docs / wiki 知识库）+ 附件（PDF / Word / Markdown / 图片）。
- **多模态直传** — file-capable 豆包型号下，在线文档导出为 PDF、Word 经 LibreOffice 转 PDF、图片原样直传，让模型「看见」排版与图像；非多模态型号自动降级为纯文本抽取。
- **一致性评分** — 严格扣分制 + 分档标尺 + 温度 0 的确定性采样，让同一份内容多次评分结果稳定可复现。
- **闭环状态机** — `待评分 → 评分中 → 已通过 / 未通过`，未通过则通知修改并循环重评，超出最大轮次自动驳回。
- **防死循环** — 基于「评分内容指纹」识别评分写回触发的回声事件并跳过，从根源杜绝自触发死循环。
- **通知频控** — 记录级冷却 + 用户每日上限，避免消息骚扰；AI 系统性失败时向告警群推送。
- **健壮兜底** — 事件去重、并发保护、附件格式白名单、空内容保护、AI 响应三级 JSON 解析兜底。

## 工作原理

```
收集表单提交 → 记录写入多维表格
  → 飞书推送 record_changed 事件 → POST /webhook/event
    → 事件去重 + 状态门控 + 内容指纹去回声
      → 采集内容（文本字段 / 在线文档 / 附件）
        → AI 综合评分 → 分数、详情、状态写回表格
          → 判定：
              ≥ 阈值  → 「已通过」（结束）
              < 阈值  → 「未通过」→ 机器人通知提报人修改 → 重新评分（循环）
              超轮次  → 「已驳回」
              AI 失败 → 「评分异常」（终止态，推送告警群）
```

### 模块职责

| 模块 | 职责 |
|---|---|
| `app/main.py` | FastAPI 应用、`/webhook/event` 端点、飞书事件解密分发、事件去重 |
| `app/orchestrator.py` | 单条记录的完整编排：状态机、内容采集、评分、写回、通知调度 |
| `app/ai.py` | 豆包评分与文档转写调用（OpenAI 兼容接口 + PDF/图片文件模态）、JSON 解析兜底 |
| `app/feishu.py` | 飞书 Open API 封装：多维表格读写、文档读取/导出 PDF、wiki 解析、消息发送、附件下载 |
| `app/parser.py` | PDF / Word / 文本抽取、飞书文档链接解析、图片占位符清洗、智能截断 |
| `app/docx_convert.py` | docx / doc → PDF 转换（LibreOffice headless，供豆包文件模态直传） |
| `app/notification.py` | 飞书卡片模板、记录级冷却、用户每日上限、告警群推送 |
| `app/config.py` | 环境变量集中定义与校验，`get_config()` 缓存单例 |
| `app/field_mapping.py` | 多维表格字段名 ↔ 代码常量映射（按你的表结构调整） |

## 快速开始

### 环境要求

- Python 3.12+
- LibreOffice（可选，仅 Word 附件直传需要；容器镜像已内置）
- [飞书企业自建应用](https://open.feishu.cn/app)，并开通以下权限：

| 权限 | 用途 |
|---|---|
| `bitable:app` | 读写多维表格记录 |
| `docs:event:subscribe` | 订阅多维表格记录变更事件 |
| `docx:document:readonly` | 读取飞书在线文档内容 |
| `drive:drive` | 附件下载、文档导出 PDF |
| `wiki:wiki:readonly` | 解析知识库（wiki）节点挂载文档 |
| `im:message:send_as_bot` | 以机器人身份发送通知 |

### 1. 安装

```bash
# 推荐使用 uv 作为包管理器
uv venv --python 3.12
source .venv/bin/activate
uv pip install -r requirements.txt
```

### 2. 配置

```bash
cp .env.example .env
# 按下方「配置项」编辑 .env
```

### 3. 准备多维表格字段

在多维表格中创建下列字段（字段名可在 `app/field_mapping.py` 中自定义）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `提报人` | 人员 | 收集表单自动填充，用于定向通知 |
| `原始描述` | 多行文本 | 用户输入的文本内容 |
| `需求文档` | 超链接 | 飞书在线文档链接（docx / docs / wiki） |
| `需求附件` | 附件 | PDF / Word / Markdown / 图片 |
| `AI评分` | 数字 | AI 输出分数 |
| `AI评分详情` | 多行文本 | AI 输出的扣分点与改进建议 |
| `AI评分时间` | 日期 | 评分完成时间 |
| `评分状态` | 单选 | `待评分` / `评分中` / `已通过` / `未通过` / `已驳回` / `评分异常` |
| `修改轮次` | 数字 | 已评分次数，达上限触发驳回 |
| `文档内容缓存` | 多行文本 | 后端自动回填（文档/附件转写文本，供飞书 AI 字段复用） |

### 4. 启动

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --log-level info
```

访问 `GET /` 可做健康检查，返回当前 provider、阈值等信息。

飞书要求回调为 HTTPS 公网地址，本地开发可用 ngrok 打通：

```bash
ngrok http 8000
# 将 https://xxxx.ngrok-free.dev 作为事件订阅 URL
```

### 5. 配置飞书事件订阅

1. 飞书开放平台 → 你的应用 → **事件订阅**
2. 请求 URL 填 `https://<你的域名>/webhook/event`（SDK 会自动完成 challenge 校验）
3. 添加事件：`drive.file.bitable_record_changed_v1`
4. 发布应用版本

### 6. 订阅多维表格（必须的一步）

> [!IMPORTANT]
> 仅在控制台添加事件类型**不足以**收到推送。必须显式调用一次订阅 API，将你的多维表格与事件绑定，否则记录变更永远不会推送到 webhook。

```python
from lark_oapi.api.drive.v1 import SubscribeFileRequest

req = SubscribeFileRequest.builder() \
    .file_token("<BITABLE_APP_TOKEN>") \
    .file_type("bitable") \
    .build()
client.drive.v1.file.subscribe(req)
```

### 7. 授权应用访问表格

打开多维表格 → 右上角 `···` → **更多** → **添加文档应用** → 搜索并添加你的应用。

## 配置项

在 `.env` 中配置（完整示例见 `.env.example`）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | — | 飞书应用凭证（必填） |
| `BITABLE_APP_TOKEN` / `BITABLE_TABLE_ID` | — | 多维表格标识（必填，从表格 URL 提取） |
| `FEISHU_BASE_URL` | 空 | 租户域名，用于通知卡片的记录跳转按钮；留空则不显示按钮 |
| `WEBHOOK_VERIFICATION_TOKEN` / `WEBHOOK_ENCRYPT_KEY` | — | 事件订阅安全参数 |
| `AI_PROVIDER` | `doubao` | AI 供应商（当前仅支持豆包） |
| `AI_API_KEY` | — | 豆包 API Key |
| `AI_MODEL` | `gpt-4o` | 模型名。`doubao-seed-2-0-{pro,lite,mini}-*` 系列支持 PDF/图片直传，其余走纯文本 |
| `AI_BASE_URL` | 方舟地址 | OpenAI 兼容接口地址 |
| `AI_TEMPERATURE` | `0` | 采样温度，默认确定性采样以稳定评分 |
| `AI_SCORE_MAX_TOKENS` | `4000` | 评分响应上限（短 JSON，留足余量防截断） |
| `AI_TRANSCRIBE_MAX_TOKENS` | `16000` | 文档转写响应上限（仅影响缓存回填，不影响评分） |
| `SCORE_THRESHOLD` | `60` | 评分通过线 |
| `MAX_REVISION_ROUNDS` | `5` | 最大修改轮次，超出则驳回 |
| `NOTIFICATION_COOLDOWN_MINUTES` | `60` | 同一记录两次通知的最小间隔 |
| `MAX_DAILY_NOTIFICATIONS_PER_USER` | `3` | 单用户每日通知上限 |
| `NOTIFICATION_GROUP_CHAT_ID` | 空 | 评分异常告警群 `chat_id`；留空则不推送 |
| `HOST` / `PORT` / `LOG_LEVEL` | `0.0.0.0` / `8000` / `INFO` | 服务监听与日志级别 |

> [!NOTE]
> Word 附件的直传依赖容器内的 LibreOffice（`soffice`）。本地直接运行时若未安装 LibreOffice，docx 会自动回退为 `python-docx` 抽取纯文本。

## 评分标准

评分维度与分档标尺定义在 `app/ai.py` 的 `SCORING_SYSTEM_PROMPT` 中，采用「先扣分、后给分」的严格立场，输出固定 JSON 结构：

| 维度 | 满分 | 说明 |
|---|---|---|
| `completeness` 内容完整性 | 30 | 信息是否完整、要素是否齐全 |
| `logic` 逻辑清晰度 | 30 | 表达是否清晰、逻辑是否连贯 |
| `format` 格式规范性 | 20 | 格式是否规范、排版是否整洁 |
| `quality` 深度与质量 | 20 | 是否有深度、是否具备实用价值 |

```json
{
  "score": 82,
  "detail": "先列扣分点，再给具体可执行的改进建议（≤500字）",
  "dimensions": { "completeness": 25, "logic": 24, "format": 18, "quality": 15 }
}
```

修改评分维度、分档或阈值锚点时，编辑该 Prompt 即可，但请保持上述 JSON schema 一致。

## 状态机

```
待评分 ─┐
        ├─→ 评分中 ─┬─→ 已通过         （结束）
未通过 ─┘           ├─→ 未通过 ─→ 通知修改 ─→（用户编辑后重新触发）
                    ├─→ 已驳回         （修改轮次超限）
                    └─→ 评分异常       （AI 系统性失败，终止态 + 告警群）
```

> [!IMPORTANT]
> 只有 `待评分` 和 `未通过` 两个状态会触发评分，其余状态一律跳过。这既避免重复处理，也是防止评分写回事件自触发死循环的关键——配合「内容指纹去回声」，只有用户真正编辑了内容（指纹变化）才会重新评分。

## Docker 部署

镜像已内置 LibreOffice 与中文字体，开箱即用：

```bash
docker-compose up -d
```

`docker-compose.yml` 已配置健康检查（探测 `GET /`）、日志滚动与 `restart: unless-stopped`。运行前确保 `.env` 已就绪。

### 通过 Traefik 暴露 HTTPS（生产推荐）

飞书要求回调为**公网 HTTPS**。`docker-compose.yml` 已内置 Traefik 标签，由服务器上已有的 Traefik 实例自动发现本容器并完成 TLS 终止与域名路由——因此默认**不再向宿主机直接暴露端口**（`ports:` 已注释，流量只经 Traefik）。

部署前需按你的环境改动以下 **3 处**（均在 compose 文件注释中标注）：

| 位置 | 默认值 | 改成 |
|---|---|---|
| `routers.reviewflow.rule=Host(...)` | `example.com` | 你的真实域名（DNS 需先解析到本机） |
| `routers.reviewflow.tls.certresolver` | `letsencrypt` | 你 Traefik 实例里**实际存在**的证书解析器名 |
| `networks` / 顶层 `traefik-network` | `traefik-network` | 与 Traefik 所在 external 网络**同名** |

> [!WARNING]
> `certresolver` 必须填 Traefik 里真实存在的名字，否则路由会被**静默丢弃**、访问报 `ERR_CONNECTION_CLOSED`。
> 查法：`docker inspect <traefik容器> | grep certificatesresolvers`，取 `--certificatesresolvers.<名字>.acme...` 里的 `<名字>`；网络名用 `docker network ls` 确认。

启动并验证链路：

```bash
docker compose up -d
curl -i https://<你的域名>/          # 返回 200 即说明 Traefik → 容器已通
```

> [!NOTE]
> 需要本地/内网直连调试时，取消 compose 中 `ports:` 的注释即可临时把 `8000` 映射回宿主机。

### 回调地址

Traefik 就绪后，飞书事件订阅的**请求网址**为：

```
https://<Host() 中填的域名>/webhook/event
```

即上面配置的域名接上固定路径 `/webhook/event`（应用的 Webhook 端点，见 `app/main.py`）。把它填入[事件订阅](#5-配置飞书事件订阅)，再完成[订阅多维表格](#6-订阅多维表格必须的一步)那一步，回调才会真正开始推送。

## 通知机制

| 场景 | 通知对象 | 频控 |
|---|---|---|
| 评分未通过 | 提报人（红色卡片，含改进建议与跳转按钮） | 记录冷却 + 每日上限 |
| 评分通过 | 提报人（绿色卡片） | 不计入频控 |
| 修改超轮次驳回 | 提报人（可选同时通知管理员） | — |
| 附件格式不符 | 提报人（跳过评审，提示允许格式） | 记录冷却 + 每日上限 |
| 无可评审内容 | 提报人（如仅上传图片但模型不支持） | 记录冷却 + 每日上限 |
| AI 评分异常 | 告警群 `chat_id` | 记录级群冷却 |

## 项目结构

```
.
├── app/
│   ├── main.py            # FastAPI 入口 + Webhook 端点 + 事件去重
│   ├── orchestrator.py    # 评分编排核心（状态机 + 采集 + 通知调度）
│   ├── ai.py              # 豆包评分/转写调用 + JSON 解析兜底
│   ├── feishu.py          # 飞书 Open API 封装
│   ├── parser.py          # 文档/附件文本抽取与清洗
│   ├── docx_convert.py    # docx/doc → PDF（LibreOffice）
│   ├── notification.py    # 卡片模板 + 通知频控
│   ├── config.py          # 环境变量配置
│   └── field_mapping.py   # 多维表格字段名映射
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── CLAUDE.md              # 面向 Claude Code 的工程说明
```

## 排障要点

> [!WARNING]
> 以下是接入飞书时最容易踩的坑，出问题时优先排查：

- **事件收不到** — 除了在控制台加事件类型，务必调用一次[订阅 API](#6-订阅多维表格必须的一步)。
- **事件里没有 `record_id`** — `record_changed` 事件的 record_id 在 `event.action_list[i].record_id`，且一个事件可含多条记录动作。
- **签名校验失败** — FastAPI 会把请求头统一转小写，而 SDK 期望 `X-Lark-Request-Timestamp` 等混合大小写，需手动还原（`main.py` 已处理）。
- **附件下载 400** — 下载 URL 必须带 `Authorization: Bearer <tenant_access_token>`，裸 `httpx.get()` 会失败。
- **wiki 链接导出失败** — wiki 链接里的 token 是知识库节点 token，需先经 Wiki API 解析出挂载文档的真实 `obj_token`，否则报 file token invalid。
- **日期字段写入报错** — 多维表格日期字段要求**毫秒级 Unix 时间戳（int）**，而非 ISO 8601 字符串。

更多工程细节见 [`CLAUDE.md`](./CLAUDE.md)。
