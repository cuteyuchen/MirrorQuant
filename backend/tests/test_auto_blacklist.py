import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.engine.auto_blacklist import (
    AutoBlacklistDecision,
    apply_auto_blacklist_changes,
    compute_auto_blacklist_changes,
)


def _alloc(inst_id, qty):
    return SimpleNamespace(inst_id=inst_id, owned_qty=qty, owned_qty_exact=None)


def _pos(inst_id, qty):
    return SimpleNamespace(inst_id=inst_id, symbol=inst_id, pos=qty)


def _order(inst_id):
    return SimpleNamespace(inst_id=inst_id, symbol=inst_id)


def test_unowned_position_is_added():
    d = compute_auto_blacklist_changes(
        enabled=True, current_blacklist=[], auto_entries=[],
        positions=[_pos("SOXLUSDT", "22.22")], open_orders=[], allocations=[],
    )
    assert d.add == ("SOXLUSDT",) and d.remove == ()


def test_owned_position_is_not_added():
    d = compute_auto_blacklist_changes(
        enabled=True, current_blacklist=[], auto_entries=[],
        positions=[_pos("NVDAUSDT", "15.19")], open_orders=[],
        allocations=[_alloc("NVDAUSDT", Decimal("15.19"))],
    )
    assert d.add == () and d.remove == ()


def test_pending_order_only_is_added():
    d = compute_auto_blacklist_changes(
        enabled=True, current_blacklist=[], auto_entries=[],
        positions=[], open_orders=[_order("VELVETUSDT")], allocations=[],
    )
    assert d.add == ("VELVETUSDT",)


def test_auto_entry_removed_when_flat():
    d = compute_auto_blacklist_changes(
        enabled=True, current_blacklist=["SOXLUSDT"], auto_entries=["SOXLUSDT"],
        positions=[], open_orders=[], allocations=[],
    )
    assert d.remove == ("SOXLUSDT",)


def test_manual_entry_never_removed():
    d = compute_auto_blacklist_changes(
        enabled=True, current_blacklist=["SNDKUSDT"], auto_entries=[],
        positions=[], open_orders=[], allocations=[],
    )
    assert d.remove == ()


def test_auto_entry_kept_while_order_pending():
    d = compute_auto_blacklist_changes(
        enabled=True, current_blacklist=["VELVETUSDT"], auto_entries=["VELVETUSDT"],
        positions=[], open_orders=[_order("VELVETUSDT")], allocations=[],
    )
    assert d.remove == ()


def test_disabled_switch_no_changes():
    d = compute_auto_blacklist_changes(
        enabled=False, current_blacklist=["SOXLUSDT"], auto_entries=["SOXLUSDT"],
        positions=[_pos("EVAAUSDT", "10")], open_orders=[], allocations=[],
    )
    assert d.add == () and d.remove == ()


class _ScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeDb:
    def __init__(self):
        self.added = []
        self.deleted = []

    async def scalars(self, _stmt):
        return _ScalarResult([])

    def add(self, row):
        self.added.append(row)

    async def delete(self, row):
        self.deleted.append(row)


@pytest.mark.asyncio
async def test_apply_auto_blacklist_changes_is_silent():
    db = _FakeDb()
    binding = SimpleNamespace(
        id=6, symbol_blacklist_json="[]", name="test", lead_id=1, owner_user_id=1,
    )

    result = await apply_auto_blacklist_changes(
        db, binding, decisions=AutoBlacklistDecision(add=("TRUMPUSDT",), remove=()),
    )

    assert result.add == ("TRUMPUSDT",)
    assert json.loads(binding.symbol_blacklist_json) == ["TRUMPUSDT"]
    assert [type(row).__name__ for row in db.added] == ["AutoBlacklistEntry"]
    assert db.deleted == []
