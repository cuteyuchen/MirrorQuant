"""Cash-flow-adjusted reporting helpers; no exchange cash-flow ingestion here."""
from __future__ import annotations

from decimal import Decimal


def cash_flow_adjusted_realized_pnl(
    start_equity, end_equity, *, deposits=0, withdrawals=0,
    start_unrealized_pnl=0, end_unrealized_pnl=0,
) -> Decimal:
    """Return net realized PnL in a single common reporting currency.

    Deposits and withdrawals are positive external transfer totals for the
    interval. Remove those cash flows and the change in unrealized PnL from
    exchange equity growth. Equity must already include trading fees (positive
    charge, negative rebate) and funding (positive receipt, negative payment),
    so neither is subtracted again. For example, a 200 deposit, 30 unrealized
    gain and 50 net realized gain turn 1000 starting equity into 1280: result 50.

    Callers must supply reconciled balances, transfers and unrealized values;
    ExecutionRecord fee/funding/fill identifiers remain NULL until populated by
    exchange reconciliation. This helper does not infer them from order ACKs.
    """
    values = [Decimal(str(value)) for value in (
        start_equity, end_equity, deposits, withdrawals, start_unrealized_pnl, end_unrealized_pnl,
    )]
    if not all(value.is_finite() for value in values):
        raise ValueError("PnL inputs must be finite")
    start, end, incoming, outgoing, start_upl, end_upl = values
    if incoming < 0 or outgoing < 0:
        raise ValueError("Deposits and withdrawals must be non-negative")
    return end - start - incoming + outgoing - (end_upl - start_upl)
