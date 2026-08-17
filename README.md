# AI News Analyzer

自动监控英文主流媒体新闻，用大模型分析对 A 股 / 美股的市场影响，支持多通道 IM 推送。

> [!WARNING]
> **Vibe Coding 产品 / 公网部署风险**
>
> 本项目包含 AI 辅助（vibe coding）生成与修改的代码，仅适合学习、研究和受控环境中的实验。未经独立的安全审计、依赖漏洞扫描、权限与密钥管理检查、网络边界加固及持续运维验证前，**请勿直接部署到公网或用于生产环境**。不要提交真实 API Key、数据库密码、Webhook 或用户数据；若必须对外提供服务，请使用防火墙/反向代理、TLS、强认证、最小权限、访问限流与日志监控，并由具备经验的工程师完成上线评审。

## 架构概览

```
采集 (RSS/RSSHub/Google News)
    ↓
去重 (URL → SimHash → 标题相似度 → 二次SimHash → 语义去重)
    ↓
全文抓取 (trafilatura → archive.ph → 同主题替代)
    ↓
分类 (可自定义小模型，过滤非重要新闻)
    ↓
深度分析 (可自定义大模型，内容不足时降级为速报)
    ↓
交付 (PostgreSQL 归档 + 8 通道 IM 推送)
```

## 快速开始

### 1. 克隆项目

```bash
git clone <repo-url>
cd news-analyzer
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`，分别配置小模型和大模型。两者可以使用不同的
OpenAI-compatible API、模型和 API Key：

```bash
# 小模型：分类与语义去重
SMALL_MODEL_PROVIDER=openai
SMALL_MODEL_BASE_URL=                  # 官方 OpenAI 留空
SMALL_MODEL_NAME=gpt-4o-mini
SMALL_MODEL_API_KEY=sk-xxx

# 大模型：深度分析；可使用另一家 OpenAI-compatible 服务
LARGE_MODEL_PROVIDER=openai_compatible
LARGE_MODEL_BASE_URL=https://llm.example.com/v1
LARGE_MODEL_NAME=vendor-large-model
LARGE_MODEL_API_KEY=vendor-key

# 必填：基础设施与管理接口凭据（请使用 URL-safe 随机值）
POSTGRES_PASSWORD=<long-random-password>
REDIS_PASSWORD=<long-url-safe-random-password>
API_AUTH_TOKEN=<long-random-bearer-token>
FLOWER_BASIC_AUTH=admin:<long-random-password>

# 可选：通知通道 (至少启用一个)
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxx
TELEGRAM_BOT_TOKEN=xxx
TELEGRAM_CHAT_ID=xxx
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/xxx
# ... 其他通道见 .env.example
```

### 3. 启动服务

```bash
docker compose up -d
```

启动 6 个服务：

| 服务 | 端口 | 说明 |
|------|------|------|
| redis | 6379 | 消息队列 + 去重指纹 |
| postgres | 5432 | 数据持久化 |
| worker | - | Celery 任务执行 |
| beat | - | 定时采集触发 (每 3 分钟) |
| api | 8000 | FastAPI 接口 |
| flower | 5555 | Celery 监控面板 |

### 4. 验证

```bash
# 健康检查
curl http://localhost:8000/health

# 以下 /api 端点都需要 Bearer Token
curl -H "Authorization: Bearer $API_AUTH_TOKEN" \
  -X POST http://localhost:8000/api/trigger/collect

# 查看新闻列表
curl -H "Authorization: Bearer $API_AUTH_TOKEN" \
  "http://localhost:8000/api/news?limit=20"

# 生成 24 小时报告 (Obsidian 格式)
curl -H "Authorization: Bearer $API_AUTH_TOKEN" \
  "http://localhost:8000/api/report?hours=24"

# Flower 监控
open http://localhost:5555
```

## 项目结构

```
news-analyzer/
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env.example
├── config/
│   ├── rss_sources.yaml      # 新闻源配置
│   ├── watchlist.yaml        # 股票关注列表
│   ├── settings.yaml         # 全局设置
│   └── prompts.yaml          # LLM Prompt 模板
├── app/
│   ├── config.py             # 配置加载
│   ├── db.py                 # 数据库会话
│   ├── models.py             # SQLAlchemy ORM
│   ├── schemas.py            # Pydantic 数据契约
│   ├── celery_app.py         # Celery 应用
│   ├── tasks.py              # 5 个 Celery 任务
│   ├── collectors/           # 新闻采集器
│   │   ├── rss.py            # RSS
│   │   ├── rsshub.py         # RSSHub (WSJ, FT)
│   │   └── google_news.py    # Google News
│   ├── dedup.py              # 五层去重
│   ├── fetcher.py            # 全文抓取 (4 步级联)
│   ├── rate_limiter.py       # Redis 令牌桶限流
│   ├── llm_client.py         # LLM 客户端
│   ├── classifier.py         # 小模型分类
│   ├── analyzer.py           # 大模型分析
│   ├── notifier/             # 8 通道通知
│   │   ├── feishu.py         # 飞书
│   │   ├── telegram.py       # Telegram
│   │   ├── discord.py        # Discord
│   │   ├── wecom.py          # 企业微信
│   │   ├── dingtalk.py       # 钉钉
│   │   ├── slack.py          # Slack
│   │   ├── bark.py           # Bark (iOS)
│   │   └── serverchan.py     # Server酱
│   └── api.py                # FastAPI 接口
└── tests/
    └── test_dedup.py         # 去重模块测试
```

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 (Redis + PostgreSQL) |
| GET | `/api/news` | 新闻列表 (`?status=&limit=&since=`) |
| GET | `/api/news/{id}` | 新闻详情 |
| GET | `/api/report` | Obsidian 报告 (`?hours=24`) |
| POST | `/api/trigger/collect` | 手动触发采集 |

## 新闻状态机

```
collected → fetched → classified → analyzed
    ↓          ↓
duplicate_dropped  (非 important 终止)
```

- **collected**: RSS 入库
- **fetched**: 全文获取完成 (含二次 SimHash 通过)
- **duplicate_dropped**: 二次 SimHash 命中，终止流水线
- **classified**: 分类完成 (非 important 终止)
- **analyzed**: 深度分析或降级速报完成

## 配置说明

### 模型与 API (`.env`)

小模型和大模型完全独立配置：

| 用途 | Provider | API Base URL | 模型 | API Key |
|------|----------|--------------|------|---------|
| 分类/语义去重 | `SMALL_MODEL_PROVIDER` | `SMALL_MODEL_BASE_URL` | `SMALL_MODEL_NAME` | `SMALL_MODEL_API_KEY` |
| 深度分析 | `LARGE_MODEL_PROVIDER` | `LARGE_MODEL_BASE_URL` | `LARGE_MODEL_NAME` | `LARGE_MODEL_API_KEY` |

- 官方 OpenAI：`PROVIDER=openai`，`BASE_URL` 留空。
- 第三方或本地服务：`PROVIDER=openai_compatible`，`BASE_URL` 填完整兼容端点，通常以 `/v1` 结尾。
- 当前客户端使用 OpenAI Chat Completions 协议；第三方服务必须兼容该协议。
- 即使本地服务不校验密钥，也要给 `API_KEY` 设置一个非空占位值。
- 修改 `.env` 后用 `docker compose up -d --force-recreate worker` 重新创建 Worker，普通 restart 不会重新加载容器环境变量。

### 新闻源 (config/rss_sources.yaml)

```yaml
sources:
  - name: reuters_world
    type: rss
    url: https://feeds.reuters.com/reuters/worldNews
    is_paywalled: false
  - name: wsj_world
    type: rsshub
    rsshub_base: https://rsshub.app
    route: /wsj/en-us/world
    is_paywalled: true
```

支持 3 种源类型：`rss` / `rsshub` / `google_news`

### 关注列表 (config/watchlist.yaml)

```yaml
a_shares:
  - code: "600519"
    name: "贵州茅台"
    aliases: ["Kweichow Moutai"]
    sector: "consumer"
us_stocks:
  - code: "AAPL"
    name: "Apple"
    aliases: ["Apple Inc"]
    sector: "tech"
```

### 通知通道 (config/settings.yaml)

默认关闭，启用后支持 8 通道并行推送：

```yaml
notifier:
  enabled: true  # 改为 true 启用
  alert_threshold: 0.75      # 深度分析推送阈值
  brief_alert_threshold: 0.85 # 速报推送阈值
```

## 可靠性设计

| 机制 | 说明 |
|------|------|
| **幂等保护** | 所有任务入口检查状态，重放不重复调 LLM |
| **防雪崩** | 限流 10s 超时 → self.retry(30s)，释放 worker |
| **五层去重** | URL → SimHash → 标题相似度 → 二次 SimHash → 语义去重 |
| **反爬降级** | archive.ph 8s 硬超时 + 403/429 秒级降级 |
| **付费墙容错** | 全文失败 → 速报预警，避免幻觉 |
| **单源隔离** | 单个源失败不影响其他源采集 |
| **出站请求防护** | 仅允许公网 HTTP(S) 地址，逐跳校验重定向，拒绝私网/回环/链路本地目标 |
| **管理面防护** | API Bearer Token + Flower Basic Auth；Compose 端口仅绑定 `127.0.0.1` |

## 成本估算

| 默认模型示例 | 用途 | 频率 | 月成本 |
|------|------|------|--------|
| gpt-4o-mini | 分类筛选 | ~1440 次/天 | ~¥30/月 |
| gpt-4o | 深度分析 | ~216 次/天 | ~¥370/月 |
| **合计** | | | **~¥400/月** |

## 常用命令

```bash
# 查看日志
docker compose logs -f worker
docker compose logs -f api

# 重启单个服务
docker compose restart worker

# 停止全部
docker compose down

# 清除数据 (谨慎!)
docker compose down -v  # 删除 PostgreSQL 数据

# 进入 worker 调试
docker compose exec worker bash

# Flower 监控
open http://localhost:5555
```

## 开发

```bash
# 本地开发 (不使用 Docker)
pip install -r requirements.txt

# 启动 Redis + PostgreSQL
docker compose up -d redis postgres

# 配置本地 .env
DATABASE_URL=postgresql://postgres:<POSTGRES_PASSWORD>@localhost:5432/news
REDIS_URL=redis://:<REDIS_PASSWORD>@localhost:6379/0

# 启动 worker
celery -A app.celery_app worker -Q collect,fetch,classify,analyze,notify -c 4

# 启动 beat
celery -A app.celery_app beat

# 启动 API
uvicorn app.api:app --reload

# 运行测试
pytest tests/ -v
```

## 许可证

MIT
