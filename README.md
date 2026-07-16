# ReviewFlow

> 基于飞书开放平台的 Webhook 驱动式 AI 评审服务

<p>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.110+-009688?logo=fastapi&logoColor=white">
  <img alt="Feishu" src="https://img.shields.io/badge/飞书-Open%20API-00D6B9">
  <img alt="Doubao" src="https://img.shields.io/badge/AI-豆包%20多模态-FF6A00">
</p>

用户首次提交后自动评分；首次评分完成后的普通编辑只更新材料，不会再次触发。用户确认附件或在线文档修改完毕后，在机器人卡片中点击“重新评分”，系统重新读取最新内容并提交唯一任务。

每次评分都会把全部在线需求文档导出为 PDF，把 Word、Markdown、文本和图片附件统一转换为 PDF，再按稳定顺序合并成一份带来源分隔页的总 PDF。原始描述作为总 PDF 之外的补充文本，与总 PDF一起通过一次模型调用生成综合分数。

## 特性

- **完整内容快照** — 多个在线文档与全部附件每次实时读取，任一材料失败即停止整次评分，绝不使用旧缓存兜底。
- **唯一总 PDF** — 在线文档在前、附件在后；每份材料前有来源分隔页，转写和评分使用完全相同的 PDF 字节。
- **一致性评分** — 严格扣分制 + 分档标尺 + 温度 0 的确定性采样，让同一份内容多次评分结果稳定可复现。
- **显式重评** — 只有首次 `待评分` 自动触发；`未通过` 后由原提报人点击卡片按钮重评。
- **并发与恢复** — 完整记录键串行、回调幂等、递增 fencing、防僵尸写回、优雅 drain 与“评分中”孤儿清道夫。
- **严格失败分类** — 用户材料问题进入“未通过”；瞬时故障单步重试；系统硬失败进入“评分异常”并只告警管理员。
- **安全边界** — 飞书 SDK 移出事件循环，附件优先通过 Drive SDK 下载，URL 兜底有 HTTPS/域名白名单、流式限额和日志脱敏。
- **发送侧熔断** — 不限制用户提报，仅在同一记录短时间异常发出大量卡片时停发并告警。

## 工作原理

```
收集表单提交 → 记录写入多维表格
  → 飞书推送 record_changed 事件 → POST /webhook/event
    → 事件去重 + 仅“待评分”自动准入
      → 完整采集 → 唯一总 PDF → 合并转写 → AI 综合评分
        → fencing 校验 → 分数、详情、状态、轮次、缓存单次写回
          → 未通过卡片：用户修改完成后 POST /webhook/card-action 显式重评
```

### 模块职责

| 模块 | 职责 |
|---|---|
| `app/main.py` | FastAPI 应用、事件/卡片回调、运行时组装与关闭 drain |
| `app/record_coordinator.py` / `app/task_registry.py` | 状态准入、身份/回调幂等、每记录串行、任务状态和 fencing |
| `app/scoring_workflow.py` / `app/result_writer.py` | 固定步骤、失败分类、轮次/终态计算和原子写回 |
| `app/content_collector.py` / `app/pdf_bundle.py` | 多链接解析、稳定去重、附件转换/校验、来源分隔页和总 PDF |
| `app/ai.py` | 豆包总 PDF 转写与综合评分、严格 Pydantic 响应校验 |
| `app/feishu.py` | 类型化异步网关：记录、Wiki、PDF 导出、消息、附件与清道夫查询 |
| `app/docx_convert.py` | PDF 直通、图片经 Pillow 转 PDF，Word/文本/Markdown 经 LibreOffice 转 PDF |
| `app/notification.py` / `app/card_templates.py` | 通知策略、卡片模板、重评动作与发送侧熔断 |
| `app/scavenger.py` | 按系统 `last_modified_time` 恢复超时且无内存活任务的“评分中”记录 |
| `app/config.py` | 环境变量集中定义与校验，`get_config()` 缓存单例 |
| `app/field_mapping.py` | 多维表格字段名 ↔ 代码常量映射（按你的表结构调整） |

## 快速开始

### 环境要求

- Python 3.12+
- LibreOffice（必需，用于所有来源分隔页以及 Word/文本/Markdown 转 PDF；图片由 Pillow 转 PDF，容器镜像已内置全部依赖）
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
uv pip install -r requirements.lock
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
| `需求标题` | 单行文本 | 需求的可读名称，用于评分任务日志标识 |
| `是否RPA` | 单选/文本 | 只有值为`是`的记录才允许进入评分流程 |
| `提报人` | 人员 | 收集表单自动填充，用于定向通知 |
| `原始描述` | 多行文本 | 用户输入的文本内容 |
| `需求文档` | 超链接 | 飞书在线文档链接（docx / docs / wiki） |
| `需求附件` | 附件 | PDF / Word / Markdown / 图片 |
| `AI评分` | 数字 | AI 输出分数 |
| `AI评分详情` | 多行文本 | AI 输出的扣分点与改进建议 |
| `AI评分时间` | 日期 | 评分完成时间 |
| `评分状态` | 单选 | `待评分` / `评分中` / `已通过` / `未通过` / `已驳回` / `评分异常` |
| `修改轮次` | 数字 | 用户手动重评次数；首次评分和技术重试不计数 |
| `文档内容缓存` | 多行文本 | 本次“在线文档 + 全部附件”总 PDF 的合并转写，不含原始描述，也不作为评分兜底 |

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
4. 将消息卡片交互回调 URL 配为 `https://<你的域名>/webhook/card-action`
5. 发布应用版本

卡片按钮使用 P2 `card.action.trigger` 回调。服务通过事件分发器按
`WEBHOOK_ENCRYPT_KEY` 校验 SHA-256 签名并解密请求，不使用旧版卡片回调的
Verification Token + SHA-1 协议。

项目锁定 `lark-oapi==1.7.1`。该版本使用
`register_p2_drive_file_bitable_record_changed_v1` 注册普通事件；运行时代码也会在
SDK 暴露 `registration_p2_drive_file_bitable_record_changed_v1` 时注册同一处理器，
兼容旧版/补发事件命名。两类事件都进入相同的去重与 `request_score()` 路径。

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
| `AI_MODEL` | `doubao-seed-2-0-pro-260215` | 必须是支持 PDF 文件输入的豆包 Seed 2.0 pro/lite/mini 型号 |
| `AI_BASE_URL` | 方舟地址 | OpenAI 兼容接口地址 |
| `AI_TEMPERATURE` | `0` | 采样温度，默认确定性采样以稳定评分 |
| `AI_SCORE_MAX_TOKENS` | `4000` | 评分响应上限（短 JSON，留足余量防截断） |
| `AI_TRANSCRIBE_MAX_TOKENS` | `16000` | 文档转写响应上限（仅影响缓存回填，不影响评分） |
| `SCORE_THRESHOLD` | `60` | 评分通过线（0-100） |
| `MAX_REVISION_ROUNDS` | `5` | 最大修改轮次，超出则驳回 |
| `NOTIFICATION_GROUP_CHAT_ID` | 空 | 评分异常告警群 `chat_id`；留空则不推送 |
| `SEND_CIRCUIT_BREAKER_WINDOW_MINUTES` / `SEND_CIRCUIT_BREAKER_MAX_MESSAGES` | `5` / `20` | 同一记录异常发卡熔断；不限制正常提报 |
| `SCAVENGER_INTERVAL_SECONDS` / `SCORING_ORPHAN_TIMEOUT_SECONDS` | `60` / `900` | “评分中”孤儿扫描与恢复阈值 |
| `MAX_ATTACHMENT_COUNT` / `MAX_SINGLE_ATTACHMENT_MB` / `MAX_TOTAL_ATTACHMENT_MB` | `20` / `20` / `100` | 附件数量与大小限制 |
| `MAX_PDF_PAGES` / `MAX_IMAGE_COUNT` / `DOC_CACHE_MAX_CHARS` | `300` / `20` / `5000` | PDF 页数、图片数与缓存转写长度限制 |
| `ATTACHMENT_ALLOWED_HOSTS` | 飞书官方域名 | 无 `file_token` 时安全 URL 下载的域名白名单 |
| `HOST` / `PORT` / `LOG_LEVEL` | `0.0.0.0` / `8000` / `INFO` | 服务监听与日志级别 |

> [!NOTE]
> 服务启动时会检查飞书配置、文件能力模型和 LibreOffice。任一项不满足会直接拒绝启动；不会降级为纯文本评分。

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
  "highlights": "值得肯定的亮点（≤150字）",
  "improvements": "进一步提升方向（≤250字）",
  "dimensions": { "completeness": 25, "logic": 24, "format": 18, "quality": 15 }
}
```

修改评分维度、分档或阈值锚点时，编辑该 Prompt 即可，但请保持上述 JSON schema 一致。

## 状态机

```
待评分 ──首次事件──→ 评分中 ─┬─→ 已通过
                             ├─→ 未通过 ──用户修改──点击重新评分──┐
                             ├─→ 已驳回                         │
                             └─→ 评分异常 ──管理员重试───────────┘
```

> [!IMPORTANT]
> 所有触发来源都会先检查 `是否RPA`，只有值为`是`时才允许评分。Bitable 普通事件还要求状态为 `待评分`；`未通过` 下的文本、附件和在线文档编辑都不会自动触发，用户必须点击卡片按钮。系统写回事件靠状态门控忽略，不使用内容指纹。

## Docker 部署

镜像已内置 LibreOffice 与中文字体，开箱即用：

```bash
docker-compose up -d
```

镜像使用锁文件安装依赖，并以非 root 的 `reviewflow` 用户运行。`docker-compose.yml` 已配置健康检查（探测 `GET /`）、日志滚动与 `restart: unless-stopped`。运行前确保 `.env` 已就绪。

### 端口与 Traefik

当前 Compose 默认把 `${PORT:-8000}` 映射到容器 8000，`traefik.enable=false`，并创建普通内部网络，适合本地或由宿主机其他反向代理接入。

飞书生产回调需要公网 HTTPS。若要让已有 Traefik 直接发现本服务，请同时完成以下调整：

1. 删除或注释 `ports:`（可选，取决于是否仍需宿主机直连）。
2. 将 `traefik.enable` 改为 `true`。
3. 把 Host 与证书解析器改成真实值。
4. 将顶层 `traefik-network.external` 改为 `true`，并确认该网络已由 Traefik 创建。

启用 Traefik 时还需按环境改动以下配置（均在 compose 文件注释中标注）：

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

默认直连验证：

```bash
docker compose up -d
curl -i http://localhost:${PORT:-8000}/
```

### 回调地址

Traefik 就绪后，飞书事件订阅的**请求网址**为：

```
https://<Host() 中填的域名>/webhook/event
```

即上面配置的域名接上固定路径 `/webhook/event`（应用的 Webhook 端点，见 `app/main.py`）。把它填入[事件订阅](#5-配置飞书事件订阅)，再完成[订阅多维表格](#6-订阅多维表格必须的一步)那一步，回调才会真正开始推送。

## 通知机制

| 场景 | 通知对象 | 动作与保护 |
|---|---|---|
| 评分未通过 | 原提报人 | “查看并修改” + “重新评分” |
| 格式、损坏、缺文件或超限 | 原提报人 | 一次列出全部问题材料 + “重新评分” |
| 评分通过 | 原提报人 | 亮点与可提升方向 |
| 达到修改上限 | 原提报人 | 立即驳回，无重评按钮 |
| 评分异常 | 管理员群 | “重试评分”，提交人不收技术异常卡片 |

所有卡片只使用记录级发送熔断；不设置用户冷却或每日上限。
通过、未通过、驳回、材料异常、管理员异常及熔断告警卡片都会显示当前记录的
`需求标题`，便于用户和管理员定位对应需求。

## 项目结构

```
.
├── app/
│   ├── main.py                 # FastAPI 入口、事件/卡片回调、生命周期
│   ├── record_coordinator.py   # 状态/身份准入与统一 request_score
│   ├── task_registry.py        # 任务引用、串行、drain 与 fencing
│   ├── scoring_workflow.py     # 固定评分步骤与错误分类
│   ├── content_collector.py    # 在线文档与附件完整采集
│   ├── pdf_bundle.py           # 来源分隔页与唯一总 PDF
│   ├── result_writer.py        # 状态和最终结果写回
│   ├── scavenger.py            # 评分中孤儿恢复
│   ├── ai.py / feishu.py       # AI 与飞书类型化网关
│   ├── notification.py         # 通知策略与发送侧熔断
│   ├── card_templates.py       # 纯卡片模板
│   └── config.py               # 环境变量与启动校验
├── tests/                      # 纯 Fake Client 自动化测试
├── requirements.txt
├── requirements.lock
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
- **附件下载失败** — 有 `file_token` 时优先通过 Drive Media SDK 下载；部分多维表附件 token 会被该接口以 HTTP 400 拒绝，此时才对记录中的临时 URL 做 HTTPS/域名白名单校验并附带 tenant token 安全兜底。大小超限不会走兜底。
- **wiki 链接导出失败** — wiki 链接里的 token 是知识库节点 token，需先经 Wiki API 解析出挂载文档的真实 `obj_token`，否则报 file token invalid。
- **日期字段写入报错** — 多维表格日期字段要求**毫秒级 Unix 时间戳（int）**，而非 ISO 8601 字符串。
- **大量 `GET /.env 404`** — 这是公网自动扫描器在探测是否错误暴露了环境变量文件。返回 404 表示当前应用没有泄露该文件；这类 4xx/5xx 异常访问日志会保留，便于发现扫描。可在 Traefik、Nginx 或 WAF 层进一步封禁点文件路径和做限流。

成功的 `GET /` 健康检查及 `POST /webhook/event`、`POST /webhook/card-action`
访问日志会被过滤；失败请求仍保留。评分任务日志会记录 `需求标题`、记录 ID、
触发来源、fence 和执行耗时；标题为空时使用记录 ID 兜底。

更多工程细节见 [`CLAUDE.md`](./CLAUDE.md)。
