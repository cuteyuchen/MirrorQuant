# Task: Remediate Critical and High Vulnerabilities in MirrorQuant

You are tasked with fixing all CRITICAL and HIGH severity issues identified in `CODEX_AUDIT_REPORT.md` within the MirrorQuant codebase at `/home/ubuntu/projects/mirror-quant`.

All 22 existing tests in `backend/tests/` must continue to pass (`backend/.venv/bin/python -m pytest backend/`).

---

## CRITICAL USER INSTRUCTION: Position Sizing and Dynamic Leverage Mathematical Principles

"需要注意的是，根据监控的带单员开单只计算开单仓位折合带单余额的多少仓位，然后折合成杠杆计算出最合适的杠杆，不需要固定杠杆，只要仓位控制得当无论多少倍杠杆都不会爆仓"

Ensure in `backend/app/services/engine/sizer.py` and `router.py`:
1. **Position Sizing is strictly determined by:**
   `lead_order_ratio = lead_order_notional / lead_margin_balance`
   `target_follower_notional = follower_equity * lead_order_ratio * risk_scale` (+ pending_debt if any)
   `target_follower_qty = target_follower_notional / (ref_price * spec.ct_val)` (quantized to lot_sz with `ROUND_DOWN` using `Decimal`).
2. **Dynamic Leverage calculation:**
   Do NOT hardcode a fixed static leverage.
   In cross margin, actual liquidation risk is 100% defined by total position notional relative to account equity (`position_notional / follower_equity`). Leverage only sets initial margin requirement fraction (`notional / leverage`).
   Optimal leverage should be dynamically chosen to:
   - Keep initial margin occupied comfortably low (e.g. 5%~10% of equity).
   - Comply with exchange symbol max leverage / risk tiers (`min(computed_lvr, spec.max_lvr, binding.max_instrument_leverage)`).
   - Dynamically adjust leverage via `await adapter.ensure_leverage(...)`.

In `PositionSizer` (in `sizer.py`), add:
```python
@staticmethod
def compute_dynamic_leverage(
    target_notional: Any,
    equity: Any,
    max_symbol_leverage: int = 100,
    target_im_fraction: float = 0.08, # ~8% of equity as initial margin
) -> int:
    """Compute optimal cross-margin leverage so initial margin occupied stays comfortable (5%-10%)."""
    notional = float(exact(target_notional))
    eq = float(exact(equity))
    if notional <= 0 or eq <= 0:
        return min(10, max_symbol_leverage)
    # initial_margin = notional / leverage <= eq * target_im_fraction
    # leverage >= notional / (eq * target_im_fraction)
    desired_lvr = int(math.ceil(notional / (eq * target_im_fraction)))
    # Bound between 2 and max_symbol_leverage
    optimal_lvr = max(2, min(desired_lvr, max_symbol_leverage))
    return optimal_lvr
```
And in `router.py`, replace static `target_lvr = int(min(binding.max_instrument_leverage, spec.max_lvr))` with:
```python
max_allowed_lvr = int(min(binding.max_instrument_leverage, spec.max_lvr))
target_lvr = PositionSizer.compute_dynamic_leverage(
    target_notional=sized.estimated_notional or (float(sized.contracts) * ref_px * float(spec.ct_val)),
    equity=my_equity,
    max_symbol_leverage=max_allowed_lvr,
)
```

---

## Required Vulnerability Remediations

### 1. [CRIT-01] SPA Path Traversal in `backend/app/main.py`
**File:** `backend/app/main.py`
**Problem:** In `spa_fallback(full_path: str)`, `candidate = static_dir / full_path` serves files without verifying containment. Encoded parent directory segments (`..%2f` etc.) allow arbitrary file read.
**Remediation:**
Import `HTTPException` from `fastapi`.
Replace `spa_fallback` body with:
```python
@app.get("/{full_path:path}")
async def spa_fallback(full_path: str):
    root = static_dir.resolve()
    candidate = (root / full_path).resolve()
    if not candidate.is_relative_to(root):
        raise HTTPException(status_code=404, detail="Not found")
    if full_path and candidate.is_file():
        return FileResponse(str(candidate))
    return FileResponse(str(root / "index.html"))
```

---

### 2. [CRIT-02 & CRIT-03] Eliminate Blind POST Retries & Implement Reconciled Order Placement
**Files:**
- `backend/app/services/adapters/okx.py`
- `backend/app/services/adapters/binance.py`
- `backend/app/services/adapters/bybit.py`
- `backend/app/services/adapters/bitget.py`
- `backend/app/services/engine/router.py`

**Problem:**
- In all exchange adapters, `_request()` retries POST requests on network exceptions/timeouts. If a timeout occurs after exchange acceptance, the retry creates a duplicate order.
- In `router.py`, `_place_order_idempotent()` catches transient exceptions from `place_order()`, attempts `get_order()`, and if `existing` is None or fails, it blindly executes a second `place_order()`.
- Binance, Bybit, and Bitget `get_order()` swallow all `ExchangeAPIError` into `None`, conflating rate-limits / auth failures with "order not found".

**Remediation:**
1. In `adapters/okx.py`, `binance.py`, `bybit.py`, `bitget.py`:
   - In `_request()`: Only retry idempotent GET requests. For POST, PUT, DELETE: set `attempts = 1` (or `attempts = retries if method.upper() == "GET" else 1`). NEVER auto-retry trading mutations on network/timeout errors!
   - In `get_order()`: Only return `None` when the exchange explicitly returns an "order not found" error code:
     - Binance: error code in `("-2013", "-2011")`
     - Bybit: error code in `("110001", "170213")` or empty result list
     - Bitget: error code in `("40768", "40762")` or empty result
     - OKX: error code in `("51603", "51001")` or empty data list
     For all other errors (rate limits, auth errors, network errors), re-raise the exception!
2. In `router.py`:
   - Define `class UnknownOrderOutcome(RuntimeError): pass`
   - In `_place_order_idempotent()`:
     When `first_exc` occurs on `place_order()`:
     Query `adapter.get_order(inst_id, cl_ord_id=cl_ord_id)`.
     If `existing and existing.get("ordId")`: return the reconciled `OrderResult`.
     If `existing is None` or querying raises an exception: DO NOT retry `place_order`! Log a critical error and raise `UnknownOrderOutcome(f"Order placement outcome ambiguous for {cl_ord_id}") from first_exc` (or handle gracefully without resending).

---

### 3. [CRIT-04] Shared Account Position Over-closing (Binding-level Quota / Ledger)
**Files:**
- `backend/app/models/domain.py`
- `backend/app/services/engine/router.py`

**Problem:**
Positions queried from `adapter.get_positions()` are account-level. When multiple bindings share an account, or when manual positions exist on the account, full close or partial reduction calculates size based on the entire account position, closing other bindings' positions.

**Remediation:**
1. In `domain.py`:
   Define `PositionAllocation`:
   ```python
   class PositionAllocation(Base):
       __tablename__ = "position_allocations"
       __table_args__ = (
           UniqueConstraint("binding_id", "inst_id", "pos_side", name="uq_pos_alloc_binding_inst_side"),
       )

       id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
       binding_id: Mapped[int] = mapped_column(
           Integer, ForeignKey("copy_bindings.id", ondelete="CASCADE"), nullable=False
       )
       account_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
       inst_id: Mapped[str] = mapped_column(String(64), nullable=False)
       pos_side: Mapped[str] = mapped_column(String(16), nullable=False)  # long | short
       owned_qty: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
       entry_cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
       updated_at: Mapped[datetime] = mapped_column(
           DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
       )

       binding: Mapped["CopyBinding"] = relationship(back_populates="allocations")
   ```
   Add `allocations: Mapped[List["PositionAllocation"]] = relationship(back_populates="binding", cascade="all, delete-orphan")` to `CopyBinding`.
2. In `router.py`:
   - Add helper `async def _get_allocation(self, db: AsyncSession, binding_id: int, account_id: int, inst_id: str, pos_side: str) -> PositionAllocation`
   - When an opening order fills (`fill_sz > 0`): increment `allocation.owned_qty += fill_sz`.
   - When closing / reducing:
     Look up `allocation.owned_qty`.
     If `allocation.owned_qty <= 0`:
       Skip with `SKIPPED_NO_LOCAL_POS` (this binding owns 0 of this position).
     Calculate reducible size: `effective_pos_sz = min(allocation.owned_qty, local_pos_sz)`.
     Use `effective_pos_sz` as `local_pos_sz` in `PositionSizer.size_close_order(...)`.
     Ensure `close_sz <= allocation.owned_qty`.
     When close order fills (`close_fill_sz > 0`): decrement `allocation.owned_qty = max(0.0, allocation.owned_qty - close_fill_sz)`.
   - In `reconcile_closed_positions`:
     Look up `allocation.owned_qty`. If `allocation.owned_qty <= 0`: return/skip.
     Size the close using `min(allocation.owned_qty, target_pos.pos)`.
     On fill, decrement `allocation.owned_qty`.
   - In `reconcile_orphan_positions`:
     Look up `allocation.owned_qty`. If `allocation.owned_qty <= 0`: skip this position.
     Size the close using `min(allocation.owned_qty, pos.pos)`.
     On fill, decrement `allocation.owned_qty`.

---

### 4. [CRIT-05] Preserve Chronological Batch Order & Prevent Causality Reversal
**Files:**
- `backend/app/services/lead_collector/binance_bapi.py`
- `backend/app/services/engine/router.py`

**Problem:**
- `group_orders_into_batches()` sorted batches with `reverse=True` (newest first).
- `router.py` used `flip_close_first`, sorting all closes before all opens.
- When a lead opens and then closes between polls, the follower closed first (finding 0 pos), then opened, leaving the follower with a dangling open position!

**Remediation:**
1. In `binance_bapi.py`:
   In `group_orders_into_batches()`: Sort batches in chronological ASCENDING order:
   ```python
   batches.sort(key=lambda x: (x.get("startTime", 0), x.get("endTime", 0), x.get("key", "")))
   ```
2. In `router.py`:
   - Batches must be processed strictly in ascending chronological order:
     ```python
     batches = sorted(
         group_orders_into_batches(new_orders),
         key=lambda b: (b.get("startTime", 0), b.get("endTime", 0), b.get("key", "")),
     ) if new_orders else []
     ```
   - In `_execute_batch_for_binding`:
     Add conservative admission guard for opening orders:
     ```python
     if is_open and active_lead_positions.get(f"{symbol}_{ps}", 0.0) <= 0:
         # Skip opening because lead has already closed this position
         await self._record_execution(
             db, binding, lead, account, symbol, inst_id, side, ps, action,
             "SKIPPED_CLOSED_CYCLE", lead_qty, lead_avg_px, lead_notional,
             details="Lead position is already closed in current snapshot; skipping historical open",
             notify_cfg=notify_cfg,
         )
         return
     ```

---

### 5. [CRIT-06] Fix `0.0 or close_sz` Falsy Evaluation Bug & Explicit Fill Checks
**File:** `backend/app/services/engine/router.py`
**Problem:**
Lines like:
`fill_sz = float((ord_info or {}).get("accFillSz") or res.fill_sz or close_sz)`
and
`fill_sz = float((ord_info2 or {}).get("accFillSz") or res.fill_sz or float(capped_qty))`
If `accFillSz` is `0.0` (zero fill on order acknowledgement or reject), Python's `or` evaluates `0.0` as falsy and falls back to `close_sz` or `capped_qty`, recording a 0-fill order as 100% executed!

**Remediation:**
Explicitly check `is not None` and handle `0.0` fills correctly:
```python
raw_acc = (ord_info or {}).get("accFillSz") if ord_info else None
if raw_acc is not None:
    fill_sz = float(raw_acc)
elif res.fill_sz is not None and res.fill_sz > 0:
    fill_sz = float(res.fill_sz)
else:
    fill_sz = 0.0
```
Do the same for `ord_info2`.
If `fill_sz <= 0`:
Do NOT treat the order as "EXECUTED"! Do not clear debt or delete guards on 0 fills.

---

### 6. [CRIT-07] Order Dedup Key & Harmonize Retention Queue Size
**Files:**
- `backend/app/services/lead_collector/binance_bapi.py`
- `backend/app/services/engine/router.py`

**Problem:**
- `order_dedup_key()` included `executedQty` and `avgPrice`, which mutate during partial fills.
- `STORE_MAX = 6000` in `binance_bapi.py`, while `router.py` truncated seen keys at `[-3000:]`.

**Remediation:**
1. In `binance_bapi.py`:
   Remove mutable fields (`executedQty`, `avgPrice`) from `order_dedup_key()`:
   ```python
   def order_dedup_key(o: Dict[str, Any]) -> str:
       """Stable SHA-1 dedup key excluding mutable orderUpdateTime, executedQty, avgPrice."""
       raw = json.dumps(
           [
               o.get("orderId") or o.get("id"),
               o.get("orderTime"),
               o.get("symbol"),
               o.get("side"),
               o.get("positionSide"),
               o.get("type"),
           ],
           sort_keys=True,
       ).encode("utf-8")
       return hashlib.sha1(raw).hexdigest()[:24]
   ```
2. In `router.py`:
   Harmonize retention size to `STORE_MAX = 6000`:
   ```python
   lead.seen_order_keys_json = json.dumps(list(seen_order_keys)[-6000:])
   lead.seen_closed_ids_json = json.dumps(list(seen_closed_ids)[-1000:])
   ```

---

### 7. [HIGH-03] Decimal & ROUND_DOWN Sizing to Prevent Lot Overflow
**File:** `backend/app/services/engine/sizer.py`
**Problem:**
Float operations cause precision loss. In `size_close_order()`, if `local_dec <= spec.min_sz`, it returns `local_dec` (closing 100% of follower position on a 1% lead reduction). If `close_sz < spec.min_sz`, it promoted to `spec.min_sz`.

**Remediation:**
In `backend/app/services/engine/sizer.py`:
1. Use `Decimal` and `ROUND_DOWN`:
```python
from decimal import Decimal, ROUND_DOWN

def exact(val: Any) -> Decimal:
    if isinstance(val, Decimal):
        res = val
    else:
        res = Decimal(str(val))
    if not res.is_finite():
        raise ValueError("Non-finite financial input")
    return res

def quantize_down(val: Any, step: Any) -> Decimal:
    d_val = exact(val)
    d_step = exact(step)
    if d_val < 0 or d_step <= 0:
        return Decimal("0")
    return (d_val / d_step).to_integral_value(rounding=ROUND_DOWN) * d_step
```
2. In `PositionSizer.size_close_order`:
```python
@staticmethod
def size_close_order(
    local_pos_sz: Any,
    is_full_close: bool,
    lead_close_qty: Any,
    lead_remaining_qty: Any,
    spec: InstrumentSpec,
) -> Decimal:
    local = exact(local_pos_sz)
    closing = exact(lead_close_qty)
    remaining = exact(lead_remaining_qty)

    if local <= 0 or closing <= 0:
        return Decimal("0")
    if remaining < 0:
        raise ValueError("Invalid lead inventory")
    if is_full_close:
        return quantize_down(local, spec.lot_sz)

    prior = closing + remaining
    if prior <= 0:
        return quantize_down(local, spec.lot_sz)

    fraction = min(Decimal("1"), closing / prior)
    qty = quantize_down(local * fraction, spec.lot_sz)
    # If partial close is below minimum size, return 0 (do not round up or close entire position!)
    return qty if qty >= spec.min_sz else Decimal("0")
```
3. In `router.py`:
If `close_sz <= Decimal("0")`:
Skip closing order with details indicating partial close below min_sz.

---

### 8. [HIGH-14] WebSocket Token `typ` and Origin Authentication
**Files:**
- `backend/app/core/security.py`
- `backend/app/api/ws.py`

**Problem:**
WebSocket accepts any valid JWT signed by the secret key, including CAPTCHA challenge tokens (`typ: mq_captcha`), allowing unauthenticated users to connect and read private state.

**Remediation:**
1. In `security.py`:
   - In `create_access_token`: add `"typ": "mq_access"`.
   - In `decode_access_token`:
     ```python
     def decode_access_token(token: str) -> Optional[Dict[str, Any]]:
         settings = get_settings()
         try:
             payload = jwt.decode(
                 token,
                 settings.resolved_jwt_secret(),
                 algorithms=["HS256"],
             )
             if payload.get("typ") != "mq_access":
                 return None
             return payload
         except (jwt.PyJWTError, TypeError, ValueError):
             return None
     ```
2. In `ws.py`:
   Verify `payload and payload.get("typ") == "mq_access" and payload.get("sub")`. If not valid, close immediately with `status.WS_1008_POLICY_VIOLATION`.

---

### 9. [HIGH-15] Secure 2FA Enrollment, Verification and Disabling
**File:** `backend/app/api/auth.py`
**Problem:**
- `setup_totp` allowed replacing an existing active 2FA secret without confirming the existing factor.
- `disable_totp` only checked `payload.code` if provided (`and payload.code`), allowing 2FA to be disabled without providing a TOTP code.
- `login` checked `if user.totp_enabled and user.totp_secret_enc:`, allowing inconsistent enabled-without-secret state to bypass 2FA.

**Remediation:**
1. In `setup_totp`:
   If `current_user.totp_enabled`:
       raise `HTTPException(status_code=400, detail="Existing 2FA must be disabled with current code before re-enrolling")`
2. In `disable_totp`:
   If `current_user.totp_enabled`:
       if not current_user.totp_secret_enc or not payload.code or not payload.code.strip():
           raise `HTTPException(status_code=400, detail="Current 2FA verification code required")`
       vault = CredentialVault()
       sec = vault.decrypt_string(current_user.totp_secret_enc)
       if not verify_totp_code(sec, payload.code.strip()):
           raise `HTTPException(status_code=400, detail="Invalid 2FA verification code")`
3. In `login`:
   If `user.totp_enabled and not user.totp_secret_enc`:
       raise `HTTPException(status_code=403, detail="2FA state corrupted; administrator recovery required")`

---

### 10. [HIGH-16] Session Invalidation on Password Change / Logout / 2FA Changes
**Files:**
- `backend/app/models/domain.py`
- `backend/app/core/security.py`
- `backend/app/api/deps.py`
- `backend/app/api/auth.py`

**Problem:**
Stateless JWT tokens remain valid after password changes, 2FA modifications, or logout until the expiration time.

**Remediation:**
1. In `domain.py`:
   Add to `User`:
   `session_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)`
2. In `security.py`:
   Update `create_access_token`:
   ```python
   def create_access_token(
       user_id: int,
       username: str,
       role: str = "admin",
       session_version: int = 0,
       expires_minutes: Optional[int] = None,
   ) -> str:
       ...
       payload = {
           "sub": str(user_id),
           "username": username,
           "role": role,
           "typ": "mq_access",
           "ver": session_version,
           "iat": int(now.timestamp()),
           "exp": int((now + timedelta(minutes=exp_min)).timestamp()),
       }
   ```
3. In `deps.py`:
   In `get_current_user_optional`:
   ```python
   user = await db.scalar(select(User).where(User.id == user_id, User.is_active == True))
   if not user:
       return None
   # Check session version if present in token
   token_ver = payload.get("ver")
   if token_ver is not None and token_ver != user.session_version:
       return None
   return user
   ```
4. In `auth.py`:
   - Pass `session_version=user.session_version` in `login()` when generating token.
   - In `change_password()`: increment `current_user.session_version += 1`.
   - In `logout()`: if `current_user`: increment `current_user.session_version += 1`.
   - In `enable_totp()` and `disable_totp()`: increment `current_user.session_version += 1`.

---

### Verification
Once all changes are applied:
1. Run `backend/.venv/bin/python -m pytest backend/` to ensure all 22 existing tests pass.
2. Ensure no syntax errors or broken imports exist.
