# AI 新闻分析系统 · 架构设计文档

> **Status**: Proposed v3（融入生产级可靠性 review，待用户确认后进入实现）
> **Architect**: Software Architect Agent
> **实现方式**: 由 subagent 基于本文档从零实现
> **注意**: 目录下旧的单机脚本代码（`src/`, `main.py` 等）已作废，以本文档为准

---

## 1. 背景与目标

### 1.1 业务目标
监控路透社、BBC、WSJ、FT 等英文主流媒体 + Google News 关键词订阅，自动筛出对 A 股 / 美股有影响的新闻，并用大模型产出可执行的市场影响分析，通过 IM 推送和报告交付。**面向 7×24 高可用运行**。

### 1.2 核心约束（已与用户确认）
| # | 决策点 | 选择 |
|---|--------|------|
| 1 | LLM 部署 | 两套独立、可配置的 OpenAI-compatible API（默认 gpt-4o-mini + gpt-4o） |
| 2 | 运行形态 | 容器化服务（Docker + Celery + Redis + PostgreSQL） |
| 3 | 实时性 | 采集 1-3min 可调，LLM 队列驱动 |
| 4 | 输出 | 报告 + IM Webhook 推送 |
| 5 | 全文抓取 | v0.1 引入 trafilatura + 付费墙容错 |
| 6 | 网络 | HTTP/SOCKS 代理（应用层配置） |
| 7 | 报告归档 | Obsidian YAML frontmatter |
| 8 | 可靠性 | 7×24 高可用，反爬快速降级、防雪崩、绝对幂等 |

---

## 2. 领域模型与限界上下文

6 个限界上下文：采集(Acquisition) → 去重(Deduplication) → 内容获取(Content Acquisition) → 分类(Classification) → 分析(Analysis) → 交付(Delivery)。

**状态机**（贯穿 fetch→classify→analyze，决定幂等与去重）：
```
collected → fetched → classified → analyzed
                ↓          ↓
        duplicate_dropped  (classified 后非 important 终止)
```
- `collected`: RSS 入库
- `fetched`: 全文获取完成（含二次 SimHash 通过）
- `duplicate_dropped`: 二次 SimHash 命中，终止流水线
- `classified`: 分类完成（非 important 终止）
- `analyzed`: 深度分析或降级速报完成

---

## 3. 架构总览

四层解耦：触发层(Beat) → 采集层(Collector+Dedup) → 内容获取+处理层(Fetcher→Classifier→Analyzer) → 输出层(PG+Notifier+API)。各层通过 Redis 队列串联，并发度独立。**所有 task 传 item_id，入口幂等检查，限流防雪崩**。

---

## 4. ADR（架构决策记录）

### ADR-001: 采用 Celery 队列驱动而非同步流水线
**Status**: Accepted
**Decision**: Celery + Redis 把采集/抓取/分类/分析拆成独立 task，队列驱动，各层并发度独立。
**Consequences**: 采集不受 LLM 延迟影响；各层可水平扩展；引入 Redis 依赖。

### ADR-002: 分层去重（长文本 SimHash + 短文本标题相似度）
**Status**: Accepted
**Decision**: 三层去重：URL hash 精确 → 长文本(>200字) SimHash 汉明≤3 → 短文本(≤200字) 标题归一化+Jaccard≥0.8。48h 时间窗口。
**Consequences**: 短文本快讯不误杀；长文本转载被识别；Jaccard 阈值需调参抽检。

### ADR-003: 双模型级联而非单模型
**Status**: Accepted
**Decision**: 独立配置小模型做分类筛选，仅将 important 新闻送往独立配置的大模型分析。默认示例为 gpt-4o-mini + gpt-4o，也可分别连接不同的 OpenAI-compatible API。
**Consequences**: 成本可控；小模型漏判需可调阈值+定期抽检。

### ADR-004: PostgreSQL 而非 SQLite
**Status**: Accepted
**Decision**: PostgreSQL 持久化，Redis 做队列和去重指纹。
**Consequences**: 多 worker 并发安全；JSONB 查询灵活。

### ADR-005: 去重指纹存 Redis 而非 DB
**Status**: Accepted
**Decision**: URL hash 用 Redis String(TTL 48h)；SimHash 用 Sorted Set；标题指纹用 Set。
**Consequences**: 亚毫秒级判断；自动过期；Redis 重启丢指纹可接受。

### ADR-006: 配置全外置 YAML，Prompt 用 Jinja2 模板
**Status**: Accepted
**Decision**: `config/*.yaml` 管理源/股票/设置；`config/prompts.yaml` Jinja2 运行时渲染。

### ADR-007: Celery 任务传 ID 而非完整 payload
**Status**: Accepted
**Decision**: 队列只传 `item_id: int`，worker 从 PostgreSQL 读取完整内容。
**Consequences**: Redis 消息轻载；消息可永久重放；多一次 DB 查询（亚毫秒级）。

### ADR-008: 付费墙内容获取的混合级联策略（含反爬快速降级）
**Status**: Accepted（v3 修订，强化 archive.ph 反爬处理）
**Context**: WSJ/FT 硬付费墙，trafilatura 抓不到全文。大模型对摘要做深度分析易幻觉。
**Decision**: 4 步级联，**archive.ph 环节强制反爬快速降级**：
1. **轻量初筛**：小模型基于标题+摘要判定 is_major，常规直接入库
2. **archive.ph 抓取**（仅 is_major）：**8 秒硬超时**；检测到 HTTP 403/429 或响应体含 `cf-browser-verification`（Cloudflare 5秒盾）→ **立即判定失败，严禁重试**，秒级切入下一步
3. **同主题开放源替代**：用 Google News 检索同主题免费开放源（Reuters/BBC/AP）作文本替代
4. **优雅降级**：所有正文获取均失败，放弃大模型分析（避免幻觉），推送低置信度"速报预警"供人工阅读
**Consequences**:
- ✅ 反爬触发时秒级降级，不卡 worker
- ✅ 从源头规避幻觉
- ❌ archive.ph 成功率受限（反爬频发），需依赖步骤 3 兜底

### ADR-009: LLM 全局速率限制 + 防雪崩（v3 修订，补充防雪崩）
**Status**: Accepted
**Context**: Analyzer 多并发可能触发 LLM TPM/RPM 限流。Celery `rate_limit` 是 per-worker 非全局。
**Decision**: Redis 分布式令牌桶，LLMClient 层拦截。**防雪崩**：
- `wait_and_acquire(timeout=10)`：等待令牌最多 10 秒
- 超时抛 `RateLimitTimeoutError`，**严禁进程内 sleep 死等**
- Celery task 捕获该异常 → `self.retry(countdown=30)` 放回队列延后调度，**释放 worker 资源处理其他任务**
**Consequences**:
- ✅ 全局速率可控
- ✅ 限流时优雅延后而非阻塞 worker
- ❌ 任务延后 30s，端到端延迟略增（可接受）

### ADR-010: 采集层支持 HTTP/SOCKS 代理
**Status**: Accepted
**Decision**: SourceConfig 加 `proxy` 可选字段；httpx client 支持；全局默认 + 源级覆盖。

### ADR-011: 全文抓取后二次 SimHash 拦截（v3 新增）
**Status**: Accepted
**Context**: collect_task 阶段新闻可能仅有标题，初次去重（基于摘要/标题）失效。fetch_task 拿到正文后，"同文异名"的媒体通稿才会暴露。
**Decision**: fetch_task 成功获取正文后，**投递 classify 前必须做第二次 SimHash**：
- 对正文计算 SimHash，查 Redis 48h 内长文本指纹
- 命中 → status 置为 `duplicate_dropped`，**终止后续流水线**
- 未命中 → 记录指纹，继续投递 classify_task
**Consequences**:
- ✅ 拦截"同文异名"媒体通稿，避免重复分析
- ✅ 二次拦截与初次（标题级）互补
- ❌ 多一次 SimHash 计算（微秒级，可忽略）

### ADR-012: 分类阶段小模型语义去重（v3 新增）
**Status**: Accepted
**Context**: 不同媒体对同一事件的报道标题/正文不同，SimHash 拦不住语义重复。若都送大模型，浪费成本且报告重复。
**Decision**: classify_task 调小模型前：
- 从 DB 提取过去 24h `is_important=True` 的历史重大新闻标题列表
- 作为 Context 注入小模型 System Prompt
- 新增判定规则：小模型检查当前新闻是否与历史列表某条"核心事实完全一致"
- 若同一事件 → **强制输出 `is_important=False`**，reason 标记"语义重复"
- 物理隔绝进入大模型链路
**Consequences**:
- ✅ 语义级去重，省大模型成本
- ✅ 报告不出现同事件重复条目
- ❌ 小模型 prompt 变长（多 20-50 条历史标题），token 略增（可接受）
- ❌ 小模型可能误判语义重复（需定期抽检）

### ADR-013: 任务状态机绝对幂等保护（v3 新增）
**Status**: Accepted
**Context**: 网络闪断导致 Celery 任务重放时，可能重复调用大模型 API（资金损耗）+ 重复推送。
**Decision**: 所有接收 `item_id` 的 Celery task 入口处：
- 先查 DB 获取该条目当前 `status`
- 若已处于目标状态或后续状态（如 analyze_task 发现 status 已是 `analyzed`）→ **直接 return 结束**
- 状态机：`collected → fetched → classified → analyzed`，每步只允许前进
**Consequences**:
- ✅ 重放安全，不重复调 LLM、不重复推送
- ✅ 与 ADR-007（传 ID）天然契合
- ❌ 状态更新需原子（用 DB 事务或 `UPDATE ... WHERE status=...` 乐观锁）

### ADR-014: 多通道通知抽象层（Strategy 模式 + 多通道并行）（v3.1 新增）
**Status**: Accepted
**Context**: 单一飞书通道不够。用户需支持飞书/Telegram/Discord/企微/钉钉/Slack/Bark/Server酱等。各通道 payload 格式不同，但触发时机和过滤逻辑一致。iMessage/WhatsApp 门槛高，v0.1 不实现但需预留扩展位。
**Decision**:
- 用 **Strategy 模式**：`BaseNotifier` 抽象统一接口（`send(item)`），每通道一个子类实现 `_build_payload` + `_post`
- **多通道并行**：一条新闻可同时推多个通道，通道列表由配置决定，并行发送（`asyncio.gather` 或线程池）
- **通道工厂**：按 `channel_type` 从注册表实例化，新增通道只加子类不改调用方
- **v0.1 实现 8 通道**：飞书/Telegram/Discord/企微/钉钉/Slack/Bark/Server酱
- **预留接口位**：iMessage（经 Pushover/Bark 桥接或 macOS AppleScript）、WhatsApp（Twilio/官方 Cloud API），v0.2+ 实现
- **统一过滤**：`alert_threshold`（深度分析）和 `brief_alert_threshold`（速报）在 BaseNotifier 层统一判定，各子类只管格式
**Consequences**:
- ✅ 加通道只加子类，开闭原则
- ✅ 多通道并行，单通道失败不影响其他
- ✅ iMessage/WhatsApp 预留位，后续平滑接入
- ❌ 多通道并行增加出站请求量（可接受，通知非高频）
- ❌ iMessage/WhatsApp 实现门槛仍在（v0.2 需额外决策）

---

## 5. 模块划分与接口契约

### 5.1 项目结构
```
news-analyzer/
├── docker-compose.yml / Dockerfile / requirements.txt / .env.example
├── config/
│   ├── rss_sources.yaml      # RSS 源(含 proxy/is_paywalled)
│   ├── watchlist.yaml        # 股票关注列表
│   ├── settings.yaml         # 模型/阈值/调度/限流/超时
│   └── prompts.yaml          # Jinja2 prompt(含语义去重规则)
├── app/
│   ├── celery_app.py / config.py / models.py / schemas.py
│   ├── collectors/           # base/rss/rsshub/google_news(含 proxy)
│   ├── dedup.py              # 分层去重 + 二次 SimHash 拦截
│   ├── fetcher.py            # trafilatura + archive.ph(8s硬超时) + 同主题
│   ├── rate_limiter.py       # Redis 令牌桶(10s 超时防雪崩)
│   ├── llm_client.py         # 统一客户端(含限流拦截)
│   ├── classifier.py         # 小模型分类(含语义去重)
│   ├── analyzer.py           # 大模型分析(含降级速报)
│   ├── notifier/            # 多通道通知(Strategy, v0.1 八通道)
│   │   ├── base.py          # BaseNotifier 抽象 + 工厂 + 并行调度
│   │   ├── feishu.py        # 飞书
│   │   ├── telegram.py      # Telegram Bot API
│   │   ├── discord.py       # Discord Webhook
│   │   ├── wecom.py         # 企业微信
│   │   ├── dingtalk.py      # 钉钉
│   │   ├── slack.py         # Slack
│   │   ├── bark.py          # Bark (iOS)
│   │   ├── serverchan.py    # Server酱 (微信)
│   │   └── _stub.py         # iMessage/WhatsApp 预留接口位(v0.2)
│   ├── tasks.py              # Celery 任务(传ID/幂等/防雪崩)
│   └── api.py                # FastAPI(Obsidian 报告)
└── tests/
```

### 5.2 数据契约（Pydantic schemas）

```python
class NewsItem(BaseModel):
    id: int | None = None
    title: str
    url: str
    source: str
    source_type: str                    # rss / rsshub / google_news
    published: str | None
    summary: str | None = None
    content: str | None = None
    content_source: str = "rss_summary" # rss_summary|full_text|archive_ph|google_news_alt
    lang: str = "en"
    collected_at: str
    url_hash: str
    content_fingerprint: str = ""
    classification: Classification | None = None
    analysis: Analysis | None = None
    alert_type: str = "analysis"        # analysis|brief
    status: str = "collected"           # collected|fetched|duplicate_dropped|classified|analyzed

class Classification(BaseModel):
    is_financial: bool
    is_important: bool
    importance_score: float
    is_major: bool = False              # 付费墙初筛用
    is_semantic_duplicate: bool = False # ← v3 语义去重标记
    category: str
    related_tickers: list[str] = []
    affected_markets: list[str] = []
    reason: str = ""                    # 含"语义重复"标记
    model: str = ""
    classified_at: str

class Analysis(BaseModel):
    headline: str
    what_happened: str
    why_it_matters: str
    impact_assessments: list[ImpactAssessment] = []
    key_risks: list[str] = []
    confidence: float = 0.0
    actionable: str = ""
    degraded: bool = False
    model: str = ""
    analyzed_at: str
```

### 5.3 采集模块接口
```python
class BaseCollector(ABC):
    def __init__(self, config: SourceConfig, timeout: int = 20,
                 default_proxy: str | None = None):  # proxy 支持
        self.proxy = config.proxy or default_proxy
    @abstractmethod
    def fetch(self) -> list[NewsItem]: ...

class SourceConfig(BaseModel):
    name: str
    type: str                           # rss / rsshub / google_news
    lang: str = "en"
    priority: str = "medium"
    proxy: str | None = None            # 源级代理
    url: str = ""
    rsshub_base: str = ""
    route: str = ""
    query: str = ""
    is_paywalled: bool = False          # 付费墙标记
```

### 5.4 去重模块接口（v3：含二次 SimHash 拦截）
```python
class Deduplicator:
    def __init__(self, redis_client,
                 use_simhash: bool = True,
                 hamming_threshold: int = 3,
                 short_text_threshold: int = 200,
                 title_jaccard_threshold: float = 0.8,
                 ttl_hours: int = 48): ...

    def is_duplicate(self, item: NewsItem) -> bool:
        """初次去重(collect 阶段): URL hash + 分层指纹(标题/摘要级)"""
        ...

    def remember(self, item: NewsItem) -> None:
        """写入指纹,TTL 48h"""
        ...

    def check_duplicate_by_content(self, content: str) -> bool:
        """← v3 新增: 二次 SimHash 拦截(fetch 阶段)
        对正文计算 SimHash,查 48h 内长文本指纹。
        命中返回 True → fetch_task 置 status=duplicate_dropped。
        未命中返回 False → 记录指纹,继续 classify。
        """
        ...
```

**Redis 键**:
- `dedup:url:{url_hash}` → String TTL 48h
- `dedup:simhash` → Sorted Set（长文本 SimHash）
- `dedup:title:{prefix}` → Set（短文本标题指纹）

### 5.5 全文抓取模块（v3：archive.ph 8s 硬超时 + 反爬检测）
```python
class ContentFetcher:
    def __init__(self, proxy: str | None = None,
                 archive_ph_timeout: int = 8,       # ← v3: 8s 硬超时
                 source_timeout: int = 20,
                 cloudflare_markers: list[str] = None):  # ← v3: 反爬标记
        self.cloudflare_markers = cloudflare_markers or [
            "cf-browser-verification", "cf-challenge", "just a moment"
        ]

    def fetch_full_text(self, item: NewsItem) -> tuple[str | None, str]:
        """返回 (content, content_source)。4 步级联。"""
        ...

    def _fetch_via_trafilatura(self, url: str) -> str | None: ...

    def _fetch_via_archive_ph(self, url: str) -> str | None:
        """← v3: 8s 硬超时 + 反爬快速失败
        - httpx.get(timeout=8)
        - 状态码 403/429 → 立即返回 None,不重试
        - 响应体含 cf-browser-verification → 立即返回 None,不重试
        - 严禁 tenacity 重试此方法
        """
        ...

    def _fetch_via_google_news_alt(self, title: str) -> str | None:
        """Google News 检索同主题开放源,取第一条非付费墙结果"""
        ...
```

### 5.6 速率限制模块（v3：10s 超时防雪崩）
```python
class RateLimitTimeoutError(Exception):
    """令牌等待超时,Celery task 应 self.retry(countdown=30)"""

class RateLimiter:
    def __init__(self, redis_client, key: str,
                 rpm: int = 500, tpm: int = 150000): ...

    def acquire(self, estimated_tokens: int = 1000) -> bool:
        """非阻塞尝试,返回是否获准。"""
        ...

    def wait_and_acquire(self, estimated_tokens: int = 1000,
                         timeout: float = 10.0) -> None:
        """← v3: 阻塞等待令牌,最多 timeout 秒。
        超时抛 RateLimitTimeoutError。
        严禁无限 sleep 死等。
        """
        ...
```

### 5.7 LLM 客户端接口
```python
class LLMClient:
    def __init__(self, provider, model, api_key,
                 base_url=None, temperature=0.2, max_tokens=1000,
                 rate_limiter: RateLimiter | None = None): ...

    def chat(self, system: str, user: str) -> str:
        """调用前 rate_limiter.wait_and_acquire(timeout=10)。
        可能抛 RateLimitTimeoutError(由上层 task 捕获重试)。"""
        ...

    def chat_json(self, system: str, user: str) -> dict: ...
```

### 5.8 分类器接口（v3：含语义去重）
```python
class Classifier:
    def __init__(self, client: LLMClient, prompts: dict,
                 watchlist_keywords: list[str],
                 importance_threshold: float = 0.6,
                 history_repo: "HistoryRepository"):  # ← v3: 历史查询

    def classify(self, item: NewsItem) -> Classification:
        """← v3 语义去重:
        1. 从 DB 取过去 24h is_important=True 的历史标题列表
        2. 注入 System Prompt 作为 Context
        3. 小模型判定是否与历史某条'核心事实完全一致'
        4. 若同一事件 → is_important=False, is_semantic_duplicate=True,
           reason='语义重复'
        5. 物理隔绝进入大模型链路
        """
        ...
```

### 5.9 分析器接口
```python
class Analyzer:
    def __init__(self, client: LLMClient, prompts: dict,
                 watchlist_context: str): ...

    def analyze(self, item: NewsItem) -> Analysis:
        """大模型深度分析。content 过短(信息不足)时:
        - 不调大模型(避免幻觉)
        - 调 make_brief_alert 生成降级速报
        """
        ...

    def make_brief_alert(self, item: NewsItem) -> Analysis:
        """降级速报:不调大模型,基于分类结果生成低置信度速报。"""
        ...
```

### 5.10 Celery 任务定义（v3：幂等 + 防雪崩 + 二次拦截）

```python
# app/tasks.py

def _check_idempotent(item_id: int, target_status: str, session) -> bool:
    """← v3: 幂等守卫。返回 True 表示应跳过(已处目标或后续状态)。"""
    item = session.get(NewsItemORM, item_id)
    if item is None:
        return True  # 不存在,跳过
    order = ["collected", "fetched", "duplicate_dropped",
             "classified", "analyzed"]
    if order.index(item.status) >= order.index(target_status):
        return True  # 已达或超过目标状态,跳过
    return False

@celery.task(name="collect")
def collect_task():
    """Beat 触发。遍历源采集 → 去重 → 入库 → 投递 fetch_task(item_id)。"""
    ...

@celery.task(name="fetch", bind=True, max_retries=2)
def fetch_task(self, item_id: int):
    """← v3: 幂等 + 二次 SimHash
    1. _check_idempotent(item_id, "fetched") → 已 fetched/analyzed 则 return
    2. 从 DB 读 item → 全文抓取
    3. 若拿到正文: dedup.check_duplicate_by_content(content)
       - 命中 → status=duplicate_dropped, return(终止流水线)
       - 未命中 → 记录指纹,更新 content, status=fetched, 投递 classify_task
    4. 未拿到正文: status=fetched(用 summary), 投递 classify_task
    """
    ...

@celery.task(name="classify", bind=True, max_retries=2)
def classify_task(self, item_id: int):
    """← v3: 幂等
    1. _check_idempotent(item_id, "classified") → 已 classified/analyzed 则 return
    2. 从 DB 读 item → 分类(含语义去重)
    3. 更新 classification, status=classified
    4. 若 is_important: 投递 analyze_task
       否则: 终止(仅入库)
    """
    ...

@celery.task(name="analyze", bind=True, max_retries=3)
def analyze_task(self, item_id: int):
    """← v3: 幂等 + 防雪崩
    1. _check_idempotent(item_id, "analyzed") → 已 analyzed 则 return
    2. 从 DB 读 item → 分析(或降级速报)
       - LLMClient 调用可能抛 RateLimitTimeoutError
    3. 捕获 RateLimitTimeoutError → raise self.retry(countdown=30)
       (放回队列延后,释放 worker,严禁 sleep 死等)
    4. 更新 analysis, status=analyzed, 投递 notify_task
    """
    ...

@celery.task(name="notify")
def notify_task(item_id: int):
    """从 DB 读 item → IM Webhook 推送(分析或速报)。"""
    ...
```

**队列路由**: collect / fetch(并发 10-15) / classify(并发 8) / analyze(并发 5-8) / notify

**Beat**: `crontab(minute="*/3")` 可调

### 5.11 数据模型（PostgreSQL）
```sql
CREATE TABLE news_items (
    id BIGSERIAL PRIMARY KEY,
    url_hash VARCHAR(16) UNIQUE NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    source VARCHAR(64) NOT NULL,
    source_type VARCHAR(16) NOT NULL,
    is_paywalled BOOLEAN DEFAULT FALSE,
    published TIMESTAMPTZ,
    summary TEXT,
    content TEXT,
    content_source VARCHAR(32) DEFAULT 'rss_summary',
    lang VARCHAR(8) DEFAULT 'en',
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    content_fingerprint VARCHAR(32),
    classification JSONB,
    analysis JSONB,
    alert_type VARCHAR(16) DEFAULT 'analysis',
    status VARCHAR(20) DEFAULT 'collected',  -- ← v3: 含 duplicate_dropped
    CHECK (status IN ('collected','fetched','duplicate_dropped','classified','analyzed'))
);
CREATE INDEX idx_news_published ON news_items(published DESC);
CREATE INDEX idx_news_status ON news_items(status);
CREATE INDEX idx_news_importance ON news_items ((classification->>'importance_score')) DESC;
CREATE INDEX idx_news_important_recent ON news_items(published DESC)
    WHERE classification->>'is_important' = 'true';  -- ← v3: 语义去重历史查询
```

### 5.12 FastAPI 接口（Obsidian 报告）
```python
GET  /health
GET  /api/news                    # ?status=&limit=&since=
GET  /api/news/{id}
GET  /api/report                  # ?hours=24,返回 Obsidian Markdown
POST /api/trigger/collect
```

**报告格式**（Obsidian YAML frontmatter）:
```markdown
---
created: 2026-08-05T01:50:00+08:00
source: reuters_world
importance: 0.92
confidence: 0.85
tickers: [AAPL, NVDA]
tags: [news-analysis, macro, earnings]
degraded: false
status: analyzed
---
# 标题
**事件**...**意义**...**影响**...**建议**...[原文](url)
```

### 5.13 多通道通知模块（v3.1：Strategy 模式 + 8 通道）

```python
# app/notifier/base.py
class BaseNotifier(ABC):
    """通知抽象基类。统一过滤,子类只管 payload 格式。"""
    channel_type: str   # feishu|telegram|discord|wecom|dingtalk|slack|bark|serverchan

    def __init__(self, config: dict, alert_threshold: float = 0.75,
                 brief_alert_threshold: float = 0.85): ...

    def send(self, item: NewsItem) -> bool:
        """统一入口: _should_send 判定 → _build_payload → _post。
        - analysis 且 importance >= alert_threshold → 发
        - brief(速报) 且 importance >= brief_alert_threshold → 发
        返回是否实际发送。"""
        ...

    def _should_send(self, item: NewsItem) -> bool:
        """按 alert_type + importance_score 统一过滤。"""
        ...

    @abstractmethod
    def _build_payload(self, item: NewsItem) -> dict:
        """子类实现: NewsItem → 该通道 payload 格式。"""
        ...

    @abstractmethod
    def _post(self, payload: dict) -> bool:
        """子类实现: 发送到通道。失败返回 False,不抛异常。"""
        ...

# v0.1 八个通道子类
class FeishuNotifier(BaseNotifier): ...      # msg_type=text, content.text
class TelegramNotifier(BaseNotifier): ...   # POST api.telegram.org/bot{token}/sendMessage
class DiscordNotifier(BaseNotifier): ...    # webhook, content + embeds
class WeComNotifier(BaseNotifier): ...      # msgtype=text, text.content
class DingTalkNotifier(BaseNotifier): ...   # msgtype=text + 签名 sign
class SlackNotifier(BaseNotifier): ...      # blocks (mrkdwn)
class BarkNotifier(BaseNotifier): ...       # {device_key}/{title}/{body}
class ServerChanNotifier(BaseNotifier): ... # sct.ftqq.com/{key}.send, title+desp

# 预留接口位(v0.2,实现后取消注释注册)
class IMessageNotifier(BaseNotifier): ...   # 经 Pushover/Bark 桥接 或 macOS AppleScript
class WhatsAppNotifier(BaseNotifier): ...   # Twilio API 或官方 Cloud API

# app/notifier/__init__.py
NOTIFIER_REGISTRY = {
    "feishu": FeishuNotifier, "telegram": TelegramNotifier, "discord": DiscordNotifier,
    "wecom": WeComNotifier, "dingtalk": DingTalkNotifier, "slack": SlackNotifier,
    "bark": BarkNotifier, "serverchan": ServerChanNotifier,
    # "imessage": IMessageNotifier,  # v0.2
    # "whatsapp": WhatsAppNotifier,  # v0.2
}

class NotifierDispatcher:
    """多通道并行调度。从配置加载启用通道,并行发送。"""
    def __init__(self, channels: list[BaseNotifier]): ...
    def dispatch(self, item: NewsItem) -> dict[str, bool]:
        """并行发送到所有启用通道(线程池/asyncio)。返回 {channel_type: success}。"""
        ...
```

**通道配置约定**: 每通道密钥/URL 走 env（`FEISHU_WEBHOOK_URL` / `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` / `DISCORD_WEBHOOK_URL` 等）。加通道 = 写子类 + 注册 REGISTRY + 配置加一条，**零改调用方**。

---

## 6. 配置项清单

### 6.1 config/rss_sources.yaml
源列表：name/type/lang/priority/proxy/is_paywalled + (url|rsshub_base+route|query)。预置 Reuters、BBC、WSJ(RSSHub, is_paywalled=true)、FT(RSSHub, is_paywalled=true)、Google News。

### 6.2 config/watchlist.yaml
A 股 + 美股关注列表（code/name/aliases/sector）+ macro_themes。

### 6.3 config/settings.yaml（v3 修订）
```yaml
small_model:
  provider: openai
  provider_env: SMALL_MODEL_PROVIDER
  model: gpt-4o-mini
  model_env: SMALL_MODEL_NAME
  api_key_env: SMALL_MODEL_API_KEY
  base_url_env: SMALL_MODEL_BASE_URL
  temperature: 0.1
  max_tokens: 300
large_model:
  provider: openai
  provider_env: LARGE_MODEL_PROVIDER
  model: gpt-4o
  model_env: LARGE_MODEL_NAME
  api_key_env: LARGE_MODEL_API_KEY
  base_url_env: LARGE_MODEL_BASE_URL
  temperature: 0.3
  max_tokens: 1500
importance_threshold: 0.6
rate_limit:
  small_model_rpm: 500
  large_model_rpm: 500
  large_model_tpm: 150000
  wait_timeout: 10                   # ← v3: 令牌等待超时(秒)
  retry_countdown: 30                # ← v3: 限流重试延后(秒)
schedule:
  collect_cron: "*/3 * * * *"
proxy:
  default: ""
fetcher:
  archive_ph_timeout: 8              # ← v3: 8s 硬超时
  source_timeout: 20
  min_content_length: 200
  cloudflare_markers:                # ← v3: 反爬检测标记
    - "cf-browser-verification"
    - "cf-challenge"
    - "just a moment"
dedup:
  use_simhash: true
  hamming_threshold: 3
  short_text_threshold: 200
  title_jaccard_threshold: 0.8
  ttl_hours: 48
  second_pass_simhash: true          # ← v3: 启用二次 SimHash 拦截
semantic_dedup:                      # ← v3: 语义去重
  enabled: true
  history_hours: 24                  # 回溯 24h is_important 历史
  max_history_items: 50              # 注入 prompt 的最大条数
notifier:                           # ← v3.1: 多通道(Strategy + 并行)
  enabled: false
  alert_threshold: 0.75             # 深度分析推送阈值
  brief_alert_threshold: 0.85       # 速报预警推送阈值
  parallel: true                    # 多通道并行发送
  channels:                         # 启用通道(v0.1 八个,按需删减)
    - type: feishu
      webhook_url_env: FEISHU_WEBHOOK_URL
    - type: telegram
      bot_token_env: TELEGRAM_BOT_TOKEN
      chat_id_env: TELEGRAM_CHAT_ID
    - type: discord
      webhook_url_env: DISCORD_WEBHOOK_URL
    - type: wecom
      webhook_url_env: WECOM_WEBHOOK_URL
    - type: dingtalk
      webhook_url_env: DINGTALK_WEBHOOK_URL
      secret_env: DINGTALK_SECRET   # 钉钉签名
    - type: slack
      webhook_url_env: SLACK_WEBHOOK_URL
    - type: bark
      device_key_env: BARK_DEVICE_KEY
      server: https://api.day.app   # Bark 服务端
    - type: serverchan
      sendkey_env: SERVERCHAN_SENDKEY
    # v0.2 预留(取消注释即启用):
    # - type: imessage
    #   bridge: pushover            # pushover | applescript
    #   user_key_env: PUSHOVER_USER_KEY
    # - type: whatsapp
    #   provider: twilio            # twilio | official
    #   account_sid_env: TWILIO_SID
    #   auth_token_env: TWILIO_TOKEN
database:
  url_env: DATABASE_URL
redis:
  url_env: REDIS_URL
report:
  format: obsidian
  output_dir: data/reports
idempotency:                         # ← v3: 幂等守卫
  enabled: true
```

### 6.4 config/prompts.yaml
四个 Jinja2 模板。**classifier_system 新增语义去重规则**（v3）：
- 注入 `{{ history_important_titles }}`（过去 24h 重要新闻标题列表）
- 规则："若当前新闻与历史列表某条报道的核心事实完全一致（同一事件），输出 is_important=false 并在 reason 标注'语义重复'"
subagent 参考旧代码 `config/prompts.yaml` 实现并补充此规则。

---

## 7. 成本与频率分析

**关键洞察**：采集频率不影响 LLM 成本（去重后全天总量固定）。

**成本估算（3min 采集）**：小模型 ~1440 次/天 ¥1 + 大模型 ~216 次/天 ¥12 = **¥13/天 ≈ ¥400/月**。

**v3 可靠性增强对成本的影响**：二次 SimHash + 语义去重会进一步减少送大模型的条数（拦截通稿和同事件重复），实际成本可能**低于** ¥400/月。

**频率上限**：采集 1min 可达；LLM 由队列+限流驱动。

---

## 8. 非功能性需求

| 属性 | 要求 |
|------|------|
| 可靠性 | 单源失败隔离；LLM 重试 3 次；archive.ph 反爬秒级降级；幂等防重放 |
| 防雪崩 | 限流 10s 超时 → self.retry(countdown=30) 释放 worker；严禁 sleep 死等 |
| 去重 | 三层（URL/SimHash/标题）+ 二次 SimHash + 语义去重，五道拦截 |
| 可观测性 | 结构化日志；Flower 监控；/health；限流计数器；duplicate_dropped 统计 |
| 可配置性 | 频率/阈值/模型/Prompt/代理/限流/超时全 YAML |
| 可扩展性 | 加源改 yaml；加 worker 改并发 |
| 安全性 | API key/Webhook 走 env |
| 优雅降级 | 付费墙全文失败→速报预警；LLM 限流→延后重试非报错；语义重复→物理隔绝 |

---

## 9. 部署（docker-compose）

```yaml
services:
  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: news
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes: ["pgdata:/var/lib/postgresql/data"]
  worker:
    build: .
    command: celery -A app.celery_app worker -Q collect,fetch,classify,analyze,notify -c 8
    env_file: .env
    depends_on: [redis, postgres]
  beat:
    build: .
    command: celery -A app.celery_app beat
    env_file: .env
    depends_on: [redis]
  api:
    build: .
    command: uvicorn app.api:app --host 0.0.0.0 --port 8000
    ports: ["8000:8000"]
    env_file: .env
    depends_on: [postgres]
  flower:
    image: mher/flower
    command: celery --broker=redis://redis:6379/0 flower --port=5555
    ports: ["5555:5555"]
volumes:
  pgdata:
```

---

## 10. 演进路线

| 阶段 | 目标 |
|------|------|
| v0.1 | 跑通全链路 + 全文抓取 + 付费墙容错 + v3 可靠性机制 |
| v0.2 | 中文源扩展（财联社/华尔街见闻 RSSHub） |
| v0.3 | 向量检索（pgvector，历史新闻语义检索） |
| v0.4 | 回测验证（历史新闻+股价验证准确率） |
| v0.5 | 本地模型部署模板与性能优化（现已可通过 OpenAI-compatible Base URL 接入） |

---

## 11. Subagent 实现指引

### 11.1 实现顺序
1. **基础设施**: docker-compose + Dockerfile + requirements.txt + 4 配置文件
2. **数据层**: models.py + schemas.py + DB 迁移（含状态机 CHECK 约束）
3. **采集层**: collectors/(含 proxy) + dedup.py(分层 + 二次 SimHash)
4. **内容层**: fetcher.py(trafilatura + archive.ph 8s + 同主题) + rate_limiter.py(10s 超时)
5. **LLM 层**: llm_client.py(含限流) + classifier.py(语义去重) + analyzer.py(降级)
6. **任务层**: celery_app.py + tasks.py(传 ID + 幂等守卫 + 防雪崩 retry)
7. **交付层**: notifier.py + api.py(Obsidian 报告)
8. **测试**: 单元 + 集成（重点测幂等/降级/防雪崩/语义去重）

### 11.2 实现约束
- Python 3.11+
- 依赖: celery[redis], feedparser, simhash, jinja2, openai, anthropic, sqlalchemy, psycopg2-binary, pydantic, fastapi, uvicorn, tenacity, pyyaml, python-dotenv, httpx, trafilatura, beautifulsoup4
- **archive.ph 严禁 tenacity 重试**，8s 硬超时 + 反爬检测后直接降级
- **所有 task 入口幂等检查**（_check_idempotent）
- **RateLimitTimeoutError 必须由 task 捕获并 self.retry(countdown=30)**，不得 sleep
- **fetch_task 必须做二次 SimHash**，命中置 duplicate_dropped
- **classify_task 必须注入 24h 历史**，语义去重
- 状态更新用乐观锁 `UPDATE ... WHERE status=? AND id=?` 保证原子
- **通知用 Strategy 模式**：每通道继承 BaseNotifier，加通道只加子类+注册，零改调用方
- **v0.1 实现 8 通知通道**：飞书/Telegram/Discord/企微/钉钉/Slack/Bark/Server酱
- **iMessage/WhatsApp 预留接口位**（v0.2 实现，v0.1 仅 _stub 占位）
- **多通道并行发送**，单通道失败不影响其他（_post 返回 False 不抛异常）
- 通知密钥全部走 env，不硬编码

### 11.3 验收标准
- `docker compose up` 启动全部服务
- 新闻流转 collect→fetch→classify→analyze→notify
- 非付费墙源抓全文；付费墙重大新闻走 archive.ph，反爬时秒级降级
- `POST /api/trigger/collect` 手动触发
- `GET /api/report?hours=24` 返回 Obsidian frontmatter 报告
- 单源/单步失败不影响整体
- LLM 限流时任务延后 30s 重试，不阻塞 worker
- 任务重放不重复调 LLM（幂等）
- 同文异名通稿被二次 SimHash 拦截（duplicate_dropped）
- 同事件不同源报道被语义去重（is_semantic_duplicate=true）
- 多通道通知：启用的通道能收到推送，单通道失败不影响其他通道
- 加通知通道只改配置 + 加子类，不改调用方（开闭原则）

### 11.4 review 修订对照表

| 版本 | review 点 | 对应模块 | 关键改动 |
|------|-----------|---------|---------|
| v2 | 传 ID 非 payload | tasks.py | item_id 入参 |
| v2 | 上下文饥饿 | fetcher/analyzer | 全文抓取 + 降级速报 |
| v2 | SimHash 短文本 | dedup.py | 分层去重 |
| v2 | 代理 | collectors/SourceConfig | proxy + httpx |
| v2 | 速率限制 | rate_limiter/llm_client | Redis 令牌桶 |
| v2 | Obsidian 报告 | api.py | YAML frontmatter |
| **v3** | **archive.ph 反爬快速降级** | **fetcher.py** | **8s 硬超时 + 403/429/cf-browser 检测 + 禁止重试** |
| **v3** | **二次 SimHash 拦截** | **dedup.py + fetch_task** | **正文二次 SimHash → duplicate_dropped** |
| **v3** | **小模型语义去重** | **classifier.py + prompts** | **24h 历史注入 + is_semantic_duplicate** |
| **v3** | **限流防雪崩** | **rate_limiter + analyze_task** | **10s 超时 → self.retry(countdown=30)** |
| **v3** | **状态机绝对幂等** | **tasks.py** | **入口 status 检查 + 乐观锁** |
| **v3.1** | **多通道通知扩展** | **notifier/** | **Strategy 模式 + 8 通道并行; iMessage/WhatsApp 预留 v0.2** |
