from __future__ import annotations

import json
import math
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import inspect, select, update
from sqlalchemy.orm.attributes import set_committed_value

from app.models.domain import TradingAccount

from app.core.time_util import BEIJING_TZ
from app.services.adapters.base import BalanceInfo


def risk_state(account):
    raw = getattr(account, "risk_equity_json", None)
    if raw is None:
        legacy = any(getattr(account, field, 0) for field in
                     ("usdt_equity", "hwm_equity", "day_start_equity"))
        return {"legacy_unverified": legacy, "assets": {},
                "legacy": {field: getattr(account, field, None) for field in
                           ("usdt_equity", "avail_equity", "hwm_equity", "day_start_equity", "day_start_date")}}
    state = json.loads(raw)
    if not isinstance(state, dict):
        raise ValueError("Invalid persisted collateral risk state")
    state.setdefault("assets", {})
    return state


def observe_equity(account, balance):
    """Persist currency-local baselines.

    Ambiguous migrated history blocks entries until a verified USDT baseline
    supersedes it: the legacy snapshot is kept for audit, but its numbers are
    never inherited into the fresh baseline, so once USDT is observed the
    quarantine has served its purpose and lifts. A quarantine with no exit
    would suspend entries forever even though verified data exists.
    """
    for field in ("eq", "avail_eq", "cash_bal", "upl"):
        value = getattr(balance, field)
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)) or not math.isfinite(value) or (field != "upl" and value < 0):
            raise ValueError("Invalid collateral observation")
    balance = BalanceInfo(float(balance.eq), float(balance.avail_eq), float(balance.cash_bal), float(balance.upl), balance.currency)
    state = risk_state(account)
    currency = balance.currency
    assets = state["assets"]
    baseline = assets.get(currency)
    today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
    if baseline is None:
        baseline = {"hwm": balance.eq, "day_start": balance.eq, "date": today}
        if currency == "USDT" and not state.get("legacy_unverified"):
            baseline["hwm"] = max(balance.eq, account.hwm_equity or 0)
            if account.day_start_date == today and (account.day_start_equity or 0) > 0:
                baseline["day_start"] = account.day_start_equity
        assets[currency] = baseline
    for field in ("hwm", "day_start"):
        if not isinstance(baseline[field], (int, float)) or not math.isfinite(baseline[field]) or baseline[field] < 0:
            raise ValueError("Invalid persisted collateral baseline")
    baseline["hwm"] = max(baseline["hwm"], balance.eq)
    if baseline["date"] != today or baseline["day_start"] <= 0:
        baseline["date"], baseline["day_start"] = today, balance.eq
    baseline["equity"], baseline["available"] = balance.eq, balance.avail_eq
    account.risk_equity_json = json.dumps(state, allow_nan=False)
    if currency == "USDT":
        state["display_usdt_confirmed"] = True
        # 新鲜 USDT 基线已建立且未继承任何 legacy 数字：隔离解除，
        # legacy 快照保留在 state["legacy"] 中供审计。
        state["legacy_unverified"] = False
        account.risk_equity_json = json.dumps(state, allow_nan=False)
        account.usdt_equity, account.avail_equity = balance.eq, balance.avail_eq
        account.hwm_equity = baseline["hwm"]
        account.day_start_equity, account.day_start_date = baseline["day_start"], baseline["date"]
    return baseline


def observe_account_equity(account, balance):
    observations = {balance.currency: balance}
    for currency in ("USDT", "USDC"):
        if currency in balance.asset_balances:
            asset = balance.asset_balances[currency]
            observations[currency] = BalanceInfo(asset["equity"], asset["available"],
                                                asset["wallet"], asset["upl"], currency)
    for observation in observations.values():
        observe_equity(account, observation)
    return observations


async def persist_equity(db, account, balance=None, *, all_assets=False, display=None, cash_flow=None):
    """Merge observations into the latest committed state with a transactional CAS.

    cash_flow: {"net_flow", "date", "updated_at_ms"} or None. When given, the
    day's net cash flow (spot<->futures transfers, +in / -out) is written into
    the USDT asset state in the same CAS update as the equity observation, so
    the two can never clobber each other. None means "leave stored flow alone".
    """
    fields = ("risk_equity_json", "usdt_equity", "avail_equity", "hwm_equity",
              "day_start_equity", "day_start_date")

    def apply(snapshot):
        observations = None
        if balance is not None:
            if all_assets:
                observations = observe_account_equity(snapshot, balance)
            else:
                observe_equity(snapshot, balance)
        if display is not None:
            state = risk_state(snapshot)
            state.update(display_usdt_confirmed=True, display_usdc=display["usdcEquity"])
            snapshot.risk_equity_json = json.dumps(state, allow_nan=False)
            snapshot.usdt_equity, snapshot.avail_equity = display["usdtEquity"], display["availEquity"]
        if cash_flow is not None:
            state = risk_state(snapshot)
            asset = state["assets"].setdefault("USDT", {})
            asset["today_net_flow"] = float(cash_flow["net_flow"])
            asset["flow_date"] = cash_flow["date"]
            asset["flow_updated_at"] = int(cash_flow["updated_at_ms"])
            snapshot.risk_equity_json = json.dumps(state, allow_nan=False)
        return observations

    if inspect(account, raiseerr=False) is None:
        return apply(account)
    with db.no_autoflush:
        for attempt in range(8):
            row = (await db.execute(select(*(getattr(TradingAccount, field) for field in fields))
                                    .where(TradingAccount.id == account.id))).one()
            snapshot = SimpleNamespace(**row._mapping)
            previous = snapshot.risk_equity_json
            observations = apply(snapshot)
            values = {field: getattr(snapshot, field) for field in fields}
            result = await db.execute(update(TradingAccount).where(
                TradingAccount.id == account.id, TradingAccount.risk_equity_json == previous)
                .values(**values).execution_options(synchronize_session=False))
            if result.rowcount == 1:
                for field, value in values.items():
                    set_committed_value(account, field, value)
                return observations
    raise RuntimeError("Concurrent collateral risk update; retry required")


def equity_baseline(account, currency):
    state = risk_state(account)
    if state.get("legacy_unverified"):
        raise ValueError("Legacy collateral risk currency unverified; entries suspended")
    baseline = state["assets"].get(currency)
    if baseline is None:
        if currency != "USDT" or getattr(account, "risk_equity_json", None) is None:
            raise ValueError("Collateral risk baseline unavailable; entries suspended")
        return account.hwm_equity, account.day_start_equity or 0
    return baseline["hwm"], baseline["day_start"]


def collateral_available(account, currency):
    state = risk_state(account)
    baseline = state["assets"].get(currency)
    if baseline is not None:
        return baseline["available"]
    if currency == "USDT" and not state.get("legacy_unverified"):
        return account.avail_equity
    raise ValueError("Collateral available margin unavailable")


def display_equity(balance):
    """totalEquity and availEquity are USDT units, never an unconverted portfolio sum."""
    assets = balance.asset_balances
    if "USDT" in assets:
        usdt = assets["USDT"]["equity"]
        available = assets["USDT"]["available"]
    elif balance.currency == "USDT" and not assets:
        usdt, available = balance.eq, balance.avail_eq
    else:
        usdt, available = 0.0, 0.0
    usdc = assets.get("USDC", {}).get("equity", balance.usdc_eq)
    return {"totalEquity": usdt, "usdtEquity": usdt, "usdcEquity": usdc,
            "availEquity": available}


def account_equity_display(account):
    state = risk_state(account)
    confirmed = not state.get("legacy_unverified") or state.get("display_usdt_confirmed")
    return {"totalEquity": account.usdt_equity if confirmed else 0.0,
            "usdtEquity": account.usdt_equity if confirmed else 0.0,
            "usdcEquity": state["assets"].get("USDC", {}).get("equity", state.get("display_usdc", 0.0)),
            "availEquity": account.avail_equity if confirmed else 0.0}


def account_usdt_hwm(account):
    state = risk_state(account)
    return state["assets"].get("USDT", {}).get("hwm", 0.0 if state.get("legacy_unverified") else account.hwm_equity)


def account_usdt_day_start(account):
    state = risk_state(account)
    return state["assets"].get("USDT", {}).get("day_start", 0.0 if state.get("legacy_unverified") else account.day_start_equity or 0.0)
