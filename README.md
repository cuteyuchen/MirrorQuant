# MirrorQuant

**Open-Source Multi-Lead Copy-Trading & Quantitative Risk Monitoring Platform**  
**开源多带单源跟单与量化风控监控平台**

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Vue](https://img.shields.io/badge/Vue-3.5-4FC08D?logo=vuedotjs&logoColor=white)](https://vuejs.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)

---

## Overview / 项目简介

**MirrorQuant** is a self-hosted, multi-lead crypto perpetual copy-trading engine and quantitative telemetry terminal. Designed with institutional-grade risk guards and cryptographic credential storage, it enables traders and researchers to monitor Binance copy-trading lead portfolios (including hidden positions via order-flow reconstruction), perform Price-Action / SMC technical analysis, replay historical trade cycles, and mirror trades across **OKX V5** and **Binance USDT-M Futures** (Live & Demo/Testnet) accounts through a sleek Vue 3 dark terminal UI.

**MirrorQuant** 是一款可自部署的开源多带单源合约跟单引擎与量化监控终端。系统采用金融级风控拦截与高强度本地密钥加密，支持多路采集币安带单交易员公开/隐藏持仓（基于历史订单流重构净仓位与同轮开平仓复盘）、自动执行 Price Action / SMC 技术结构分析，并将信号按 N:M 路由矩阵精准同步至多个 **OKX V5** 或 **币安合约**（支持实盘与模拟盘）子账户。

---

## Core Architecture / 核心架构

```
┌────────────────────────────────────────────────────────────────────────────┐
│                        MirrorQuant Terminal (Vue 3 + TS)                   │
│   Dashboard · Leads & Trade Cycles · Accounts · Copy Matrix · Proxies      │
└───────────────────────────────▲───────────────────────▲────────────────────┘
                                │ REST + HttpOnly JWT   │ WebSocket (/ws/stream)
┌───────────────────────────────┴───────────────────────┴────────────────────┐
│                         FastAPI Orchestrator Engine                        │
│                                                                            │
│  ┌─────────────────────┐   ┌─────────────────────┐   ┌──────────────────┐  │
│  │ BinanceBapiCollector│   │  PAAnalyzer (SMC)   │   │  ProxyManager    │  │
│  │ • 11012005 Backoff  │   │  • 1H/4H EMA/RSI/ATR│   │  • HTTP/SOCKS5   │  │
│  │ • SHA-1 Dedup       │──▶│  • 72H Swing Zones  │   │  • Latency Check │  │
│  │ • Hidden Pos Walk   │   │  • Unmitigated FVG  │   │  • Auto Failover │  │
│  │ • Trade Cycle Replay│   └─────────────────────┘   └──────────────────┘  │
│  └──────────┬──────────┘                                                   │
│             ▼                                                              │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                 CopyRouter & RiskEngine (N:M Matrix)                 │  │
│  │  • Proportional Sizing   • Micro-Order Debt Accumulator              │  │
│  │  • Slippage Guard        • HWM Drawdown Breaker                      │  │
│  │  • Leverage Caps         • Idempotent Orders & Owned-Close Ledger  │  │
│  └──────────┬───────────────────────────────────┬───────────────────────┘  │
│             ▼                                   ▼                          │
│  ┌─────────────────────┐             ┌─────────────────────┐               │
│  │   OKXAdapter (V5)   │             │ BinanceAdapter(FAPI)│               │
│  │   Live & Simulated  │             │   Live & Testnet    │               │
│  └─────────────────────┘             └─────────────────────┘               │
│                                                                            │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │      SQLite (WAL Mode) + AES-256-GCM CredentialVault + Argon2id      │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────────┘
```

---

## Key Features / 核心功能

- **Zero-Popup Modern Auth & Encrypted Vault (安全认证与密钥保险箱)**
  - First-run Setup Wizard & clean dark terminal Login UI (no ugly browser Basic Auth popups).
  - Passwords hashed with **Argon2id**; sessions secured via **HttpOnly JWT** cookies.
  - Exchange API keys, secrets, and passphrases encrypted at rest using **AES-256-GCM** with random 12-byte nonces.
- **Hidden Position Reconstruction & Trade Cycle Replay (隐藏持仓推算与同轮复盘)**
  - Handles Binance BAPI `positionShow=false` portfolios via chronological net-position walking, `position-history` phantom-leg reconciliation, and official maintenance-margin bracket lookup.
  - Groups fills into **Trade Cycles (Rounds)** tracking entry count, scale-in frequency, holding duration, volume-weighted entry/exit price, and realized PnL — with one-click **CSV Export** for review notes.
- **Price-Action / SMC Entry Analysis (自动量价与结构分析)**
  - Computes 1H/4H EMA(21/55) trend alignment, Wilder RSI(14), ATR(14), 72H Swing Premium/Discount percentile, unmitigated Fair Value Gaps (FVG), and technical invalidation/target levels.
- **N:M Copy Routing & Risk Engine (N:M 跟单路由与风控引擎)**
  - Bind any monitored lead to multiple OKX or Binance accounts with independent `riskScale`, single-symbol/portfolio leverage caps, slippage guards, and symbol whitelist/blacklist/mapping.
  - **Micro-Order Debt Accumulator**: When scaled target notional is below exchange `minSz` or `minNotional`, accumulates pending USD debt into subsequent orders so capital tracks faithfully.
  - **High-Water-Mark (HWM) Drawdown Breaker & Global Kill Switch**: Halts new entries while permitting evidence-backed, allocation-scoped reduce-only exits. Partial/unknown outcomes must be reconciled before replacement orders; an IOC remainder is never blindly resubmitted as a market order.
- **Proxy Pool & Multi-Channel Alerts (代理池与多通道通知)**
  - HTTP/SOCKS5 proxy node manager with latency testing. Explicitly assigned proxies fail closed when unavailable; unassigned accounts follow the configured exchange egress policy.
  - Real-time execution and risk alerts via **Telegram Bot**, **WeCom / DingTalk**, and **Generic JSON Webhooks**.
- **Privacy-Isolated Public Radar (`/public`) (公私分离观察台)**
  - Optionally expose opted-in lead snapshots and PA analysis on `/public` without ever leaking follower accounts, API keys, or execution records.

---

## Quick Start with Docker / Docker 一键部署

后端依赖 Linux 的 `fcntl` 文件锁及 POSIX 文件权限，**仅支持 Linux / Docker Linux 容器**，不能原生在 Windows 运行。Windows 请使用 Docker Desktop 的 Linux 容器或 WSL2 中的 Linux 环境。

### 1. Clone & Start / 克隆并启动

```bash
git clone https://github.com/cuteyuchen/MirrorQuant.git
cd MirrorQuant

# Configure the deployment before allowing access
cp .env.example .env
# Generate a private bootstrap token, then put it in MQ_BOOTSTRAP_TOKEN in .env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# Linux 绑定目录需由非 root 容器用户 UID/GID 10001 可写。
mkdir -p data
sudo chown 10001:10001 data
sudo chmod 700 data

# Build and start MirrorQuant + Nginx reverse proxy
docker compose up -d --build
```

### 2. Access the Terminal / 访问控制台

- **Web UI (via Nginx)**: `http://localhost:8080`
- **Direct FastAPI / API Docs**: `http://127.0.0.1:8000` (only bound to host loopback; Swagger UI at `/docs`)
- On first visit, use the **Setup Wizard** (`/setup`) and your deployment's private `MQ_BOOTSTRAP_TOKEN` to create the administrator account. Remove that environment value after setup. The API equivalent requires the `X-MQ-Bootstrap-Token` header.
- Add your exact console origin to `MQ_CORS_ORIGINS` for authenticated WebSockets. Use HTTPS and `MQ_COOKIE_SECURE=true` for remote deployments.

---

## Local Development / 本地开发运行

### Backend (Linux, Python 3.12+)

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

# 本地 Linux 使用本目录下 data；Docker 示例使用 /app/data。
export MQ_DATA_DIR="$PWD/data"
export MQ_DATABASE_URL="sqlite+aiosqlite:///$PWD/data/mirrorquant.db"

# Run unit & API integration tests
pytest -v tests/

# Start FastAPI server (auto-reload)
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --no-proxy-headers
```

### Frontend (Node 20+ / Vue 3 + Vite)

```bash
cd frontend
npm install

# Start Vite dev server with /api & /ws proxy to port 8000
npm run dev

# Production build
npm run build
```

---

## Configuration Reference / 环境变量说明

| Variable | Default | Description |
| :--- | :--- | :--- |
| `MQ_PORT` | `8000` | Backend FastAPI listening port |
| `MQ_WEB_PORT` | `8080` | Nginx reverse-proxy public port |
| `MQ_DATA_DIR` | `/app/data` | Persistent storage for SQLite DB & auto-generated `.master_key` |
| `MQ_DATABASE_URL` | Docker: `sqlite+aiosqlite:////app/data/mirrorquant.db` | 与 `/app/data` 挂载一致；留空时由 `MQ_DATA_DIR` 推导 |
| `MQ_MASTER_KEY` | *(auto-generated for a new local database)* | URL-safe base64 encoding of 32 random bytes; missing keys for an existing database must be restored, never regenerated |
| `MQ_JWT_SECRET` | *(auto-generated)* | HS256 secret for HttpOnly JWT cookies |
| `MQ_BOOTSTRAP_TOKEN` | *(required for initial setup)* | Private random token of at least 32 characters authorizing the first administrator |
| `MQ_CORS_ORIGINS` | *(local console origins)* | Exact allowed HTTP/WebSocket origins; add the deployed console's HTTPS origin |
| `MQ_TRUSTED_PROXY_CIDRS` | Compose: `127.0.0.0/8,::1/128,172.30.0.3/32` | `.env` 显式值优先；只信任受控代理地址，应用自身缺省仅信任 loopback |
| `MQ_ENABLE_BACKGROUND_ENGINE` | `true` | Enable background lead polling & copy routing loop |
| `MQ_ALLOW_DIRECT_EGRESS` | `true` | Legacy direct-egress fallback for unknown/unspecified exchanges; explicit exchange lists take precedence |
| `MQ_PROXY_REQUIRED_EXCHANGES` | `binance` | Comma-separated or JSON exchange list that requires a healthy enabled proxy; overrides both the direct list and global flag |
| `MQ_DIRECT_EGRESS_EXCHANGES` | `okx,bybit,bitget` | 未绑定代理时允许直连；显式指定的代理失效后不得直连或切换其他节点 |
| `MQ_ENABLE_EXPERIMENTAL_EXCHANGES` | `false` | 仅设为 `true` 才允许创建 Bybit/Bitget 账户；这不代表适配器已达到正式支持标准 |
| `MQ_PA_KLINE_MAX_STALENESS_MULTIPLIER` | `3.0` | Maximum candle age as a multiple of its timeframe before analysis reports `stale: true` |
| `MQ_READINESS_HEARTBEAT_MAX_AGE_SEC` | `30` | Maximum age of the orchestrator's monotonic heartbeat for readiness |
| `MQ_READINESS_DB_TIMEOUT_SEC` | `2` | Timeout for the readiness database probe |
| `MQ_NOTIFICATION_OUTBOX_MAX_ATTEMPTS` | `8` | 默认退避 10/20/40/80/160/300/300 秒，覆盖约十五分钟，耗尽后保留 failed |
| `MQ_NOTIFICATION_OUTBOX_POLL_SEC` | `1.0` | Background notification outbox polling interval |
| `MQ_COOKIE_SECURE` | `false` | Set `true` when serving strictly over HTTPS |
| `MQ_LOGIN_ATTEMPT_WINDOW_SEC` | `900` | 无新失败超过窗口后失败计数衰减；硬锁仅针对用户名与 IP 的组合 |
| `MQ_SQLITE_BUSY_TIMEOUT_SEC` | `30` | SQLite timeout 与 busy_timeout 的统一秒数，写入使用 BEGIN IMMEDIATE |
| `MQ_RETENTION_DAYS` | `90` | 执行、审计、终态通知、失效登录限制的保留期；未决和待审依赖证据保留 |
| `MQ_RETENTION_CLEANUP_INTERVAL_SEC` | `3600` | 引擎低频清理间隔，秒；引擎关闭时不运行清理 |

Password changes, logout, and 2FA changes invalidate all existing sessions, including WebSockets. Existing pre-upgrade access tokens also require signing in again. 2FA enrollment requires the current password, expires after ten minutes, and cannot replace an active factor until it has been disabled with its current code. Back up database and vault keys together; startup refuses unreadable credentials. Existing installations using a non-base64 master-key string need a deliberate key migration before upgrading.

Run Uvicorn with `--no-proxy-headers` so the application can validate the actual socket peer against `MQ_TRUSTED_PROXY_CIDRS`. The supplied Nginx configuration replaces incoming forwarded IP headers with its socket peer and strips `CF-Connecting-IP`. Compose reserves `172.30.0.0/24` for its network; deployments using another subnet must update both the Nginx address and its trusted CIDR together. Password hashing and verification run off the event loop in a dedicated pool capped at two workers. CAPTCHA challenges expire after 120 seconds and are consumed atomically on verification, including incorrect answers. Their bounded cache belongs to the single deployed Uvicorn worker; a restart invalidates pending challenges. Multiple workers require a shared nonce cache or routing each challenge and its verification to the same worker.

`GET /api/health` is a cheap process-liveness probe. `GET /api/ready` checks database connectivity and the orchestrator heartbeat, returning HTTP 200 with `status: ready` or HTTP 503 with `status: degraded`. An engine that is disabled, has not started, or has stopped is not ready. Readiness does not certify individual exchange connections or trading profitability.

Management mutations require an active `admin`; viewers retain the existing authenticated read endpoints and the read-only WebSocket stream. The new board, intents, and execution listing require `admin`. Notification secrets (`telegramBotToken`, `webhookUrl`, `wecomWebhookUrl`, `dingtalkWebhookUrl`, stored as snake_case) return a fixed mask plus a `*_configured` boolean. Empty strings and that mask preserve stored secrets on PUT. Proxy URLs expose `***:***` in place of userinfo and `url_configured` indicates whether credentials exist. A masked update preserves credentials for the same proxy endpoint; changing endpoints requires explicit new credentials.

Execution and security notifications enter `notification_outbox` in their database transaction. The background drainer resolves current channel settings, retries failures with backoff, and retains exhausted rows with `status='failed'` as the dead-letter queue. Graceful shutdown waits for the current delivery with a bounded timeout; queued work and backoff survive restart instead of exhausting all retries during shutdown. Delivery is at least once: a process crash after a sink accepts a message but before its acknowledgement is stored can duplicate that message. Webhooks require HTTPS; WeCom/DingTalk require `errcode=0`, Telegram requires `ok=true` and messages are truncated to 4096 characters. The optional JSONL sink must be a file within `MQ_DATA_DIR`; relative paths are resolved under that directory.

### Trading Boundaries / 交易边界

- 目前新开仓仅支持 **hedge（双向持仓）+ cross（全仓）**。Schema 中保留的其他模式字段不代表可交易；不支持的模式会被下单门禁拒绝。
- `bybit` / `bitget` 是半成品实验适配器，默认禁止新建。只有部署者设置 `MQ_ENABLE_EXPERIMENTAL_EXCHANGES=true` 才放行创建；正式使用前仍需独立验证。
- 软件止盈止损由进程轮询行情并对已确认归属的仓位执行平仓，不是交易所托管止损。进程退出、断网、代理故障、行情缺失、限速或订单未决时都可能无法及时执行；触发价不保证成交价，不保证无滑点或止损成功。
- 全局急停只阻止开仓，不撤销交易所既有订单，也不保证账户自动清仓。修改只能走 `/api/bindings/kill-switch`；通用设置 PUT 的旧急停字段被忽略。
- 出场来源证据不足记录为 `DEFERRED_SOURCE_UNPROVEN` 并等待后续观察，不累加下单失败或触发熔断。明确业务拒单记录为 `REJECTED`；提交结果不明的订单仍保持 `UNKNOWN` 隔离，绝不盲目重发。

### Admin Recovery & Board / 运维与看板

登录硬锁只针对（用户名、客户端 IP）组合；用户名维度只施加有上限的递增延迟。计数在时间窗内无新失败后衰减，不会永久累积。管理员无法登录时，在拥有数据目录访问权的 Linux 主机或容器内执行：

```bash
# 只解除登录限制，不修改密码、2FA 或会话；可加 --ip 203.0.113.10 限定组合锁。
docker compose exec mirrorquant python -m app.maintenance.unlock_admin --username admin
# 本地 Linux，从 backend/ 运行：
python -m app.maintenance.unlock_admin --username admin
```

`GET /api/dashboard/board` 只读持久化快照、allocation 与执行记录，按 UTC+8 自然日汇总，未知值返回 `null`。账户金额是 USDT 口径，不按一比一合并 USDC；`todayPnl` 是同日净值基线差额，未扣除充值/提现，基线缺失则为 `null`。跟仓数量换算为基币，偏差不超过 5% 视为 aligned；缺少合约换算证据则 unknown。软件 TP/SL 多绑定价位不一致时返回 `null`。事件只展示有可判定开加减平来源证据的记录，倒序最多 100 条；不能证明当日逐笔收益完整性时日内已实现收益为 `null`。

`GET /api/dashboard/intents?state=UNKNOWN,SUBMITTING` 返回未决意图；`GET /api/dashboard/executions` 支持 `binding_id/status/limit/offset`，返回 `total` 和 `details`。三个接口均需管理员鉴权。公开看板默认关闭，配置行缺失也不会自动开放。

生产依赖与开发测试依赖已分离。Linux 离线回归使用 `backend/requirements-dev.txt`，生产镜像只安装 `requirements.txt`。升级前同时备份数据库与密钥；SQLite 的新增字段和索引会在应用启动时幂等补齐。非 SQLite 既有数据库需要由部署者执行等价 schema 迁移，本项目不会自动迁移生产 PostgreSQL。

Analysis reports `asOf` and `analyzedAt` from the oldest contributing timeframe's latest candle open timestamp, conservatively avoiding an unfinished candle's future close time. Stale cached data is refetched; when the upstream remains unavailable, any usable fallback is explicitly marked stale.

`ExecutionRecord` now has nullable `fee`, `funding`, and `exchange_fill_id` columns, with an idempotent SQLite upgrade. They remain unknown until exchange reconciliation supplies them; adapter-level fee/funding ingestion is not implemented here. The documented `cash_flow_adjusted_realized_pnl()` helper in `backend/app/services/engine/accounting.py` computes equity growth minus net deposits and the change in unrealized PnL using `Decimal`. Inputs must share a reporting currency and include fees/funding in equity already, so those charges are not counted twice.

---

## Disclaimer & Acknowledgments / 免责声明与致谢

- **Risk Disclaimer**: Perpetual futures trading and automated copy-trading carry substantial risk of loss. All Price-Action / SMC indicators are strictly objective technical references and do not constitute financial advice. Always test thoroughly on **OKX Demo** or **Binance Testnet** before allocating live capital.
- **Methodology Reference**: Trade cycle grouping (同轮开平仓归组), reliable baseline cutoff awareness, and CSV review export workflows drew conceptual inspiration from [binance-copy-trading-review](https://github.com/Ibook000/binance-copy-trading-review).

---

## License / 开源协议

Licensed under the **Apache License 2.0**. See [LICENSE](LICENSE) for full details.
