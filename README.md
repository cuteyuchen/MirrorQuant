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
│  │  • Leverage Caps         • IOC-to-Market Fallback & Full-Close Sync  │  │
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
  - **High-Water-Mark (HWM) Drawdown Breaker & Global Kill Switch**: Automatically halts new open orders on excessive drawdown while preserving `reduceOnly` and 100% full-close sync.
- **Proxy Pool & Multi-Channel Alerts (代理池与多通道通知)**
  - HTTP/SOCKS5 proxy node manager with one-click latency testing and priority failover.
  - Real-time execution and risk alerts via **Telegram Bot**, **WeCom / DingTalk**, and **Generic JSON Webhooks**.
- **Privacy-Isolated Public Radar (`/public`) (公私分离观察台)**
  - Optionally expose opted-in lead snapshots and PA analysis on `/public` without ever leaking follower accounts, API keys, or execution records.

---

## Quick Start with Docker / Docker 一键部署

### 1. Clone & Start / 克隆并启动

```bash
git clone https://github.com/cuteyuchen/MirrorQuant.git
cd MirrorQuant

# Configure the deployment before allowing access
cp .env.example .env
# Generate a private bootstrap token, then put it in MQ_BOOTSTRAP_TOKEN in .env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# Build and start MirrorQuant + Nginx reverse proxy
docker compose up -d --build
```

### 2. Access the Terminal / 访问控制台

- **Web UI (via Nginx)**: `http://localhost:8080`
- **Direct FastAPI / API Docs**: `http://localhost:8000` (Swagger UI at `/docs`)
- On first visit, use the **Setup Wizard** (`/setup`) and your deployment's private `MQ_BOOTSTRAP_TOKEN` to create the administrator account. Remove that environment value after setup. The API equivalent requires the `X-MQ-Bootstrap-Token` header.
- Add your exact console origin to `MQ_CORS_ORIGINS` for authenticated WebSockets. Use HTTPS and `MQ_COOKIE_SECURE=true` for remote deployments.

---

## Local Development / 本地开发运行

### Backend (Python 3.12+)

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

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
| `MQ_DATABASE_URL` | `sqlite+aiosqlite:///./data/mirrorquant.db` | Async SQLAlchemy DB URL (WAL mode auto-enabled) |
| `MQ_MASTER_KEY` | *(auto-generated for a new local database)* | URL-safe base64 encoding of 32 random bytes; missing keys for an existing database must be restored, never regenerated |
| `MQ_JWT_SECRET` | *(auto-generated)* | HS256 secret for HttpOnly JWT cookies |
| `MQ_BOOTSTRAP_TOKEN` | *(required for initial setup)* | Private random token of at least 32 characters authorizing the first administrator |
| `MQ_CORS_ORIGINS` | *(local console origins)* | Exact allowed HTTP/WebSocket origins; add the deployed console's HTTPS origin |
| `MQ_TRUSTED_PROXY_CIDRS` | `127.0.0.0/8,::1/128` | Comma-separated CIDRs allowed to forward client IPs; Compose additionally trusts its fixed Nginx address `172.30.0.3/32` |
| `MQ_ENABLE_BACKGROUND_ENGINE` | `true` | Enable background lead polling & copy routing loop |
| `MQ_COOKIE_SECURE` | `false` | Set `true` when serving strictly over HTTPS |

Password changes, logout, and 2FA changes invalidate all existing sessions, including WebSockets. Existing pre-upgrade access tokens also require signing in again. 2FA enrollment requires the current password, expires after ten minutes, and cannot replace an active factor until it has been disabled with its current code. Back up database and vault keys together; startup refuses unreadable credentials. Existing installations using a non-base64 master-key string need a deliberate key migration before upgrading.

Run Uvicorn with `--no-proxy-headers` so the application can validate the actual socket peer against `MQ_TRUSTED_PROXY_CIDRS`. The supplied Nginx configuration replaces incoming forwarded IP headers with its socket peer and strips `CF-Connecting-IP`. Compose reserves `172.30.0.0/24` for its network; deployments using another subnet must update both the Nginx address and its trusted CIDR together. Password hashing and verification run off the event loop. CAPTCHA challenges expire after 120 seconds and are consumed atomically on verification, including incorrect answers. Their bounded cache belongs to the single deployed Uvicorn worker; a restart invalidates pending challenges. Multiple workers require a shared nonce cache or routing each challenge and its verification to the same worker.

---

## Disclaimer & Acknowledgments / 免责声明与致谢

- **Risk Disclaimer**: Perpetual futures trading and automated copy-trading carry substantial risk of loss. All Price-Action / SMC indicators are strictly objective technical references and do not constitute financial advice. Always test thoroughly on **OKX Demo** or **Binance Testnet** before allocating live capital.
- **Methodology Reference**: Trade cycle grouping (同轮开平仓归组), reliable baseline cutoff awareness, and CSV review export workflows drew conceptual inspiration from [binance-copy-trading-review](https://github.com/Ibook000/binance-copy-trading-review).

---

## License / 开源协议

Licensed under the **Apache License 2.0**. See [LICENSE](LICENSE) for full details.
