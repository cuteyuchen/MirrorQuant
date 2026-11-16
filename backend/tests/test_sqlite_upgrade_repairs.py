"""旧 SQLite 通知索引的幂等升级不接触真实数据。"""
import json

import pytest
from sqlalchemy import create_engine, text

from app.core import database
from app.core.database import _apply_sqlite_migrations


@pytest.mark.parametrize("existing", ["", ", execution_id INTEGER", ", event_key VARCHAR(160)"])
def test_notification_relation_columns_backfill_when_either_column_was_missing(existing):
    # 兼容上次中断只新增一个列的数据库，不通过重建表丢失旧通知。
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE notification_outbox (id INTEGER PRIMARY KEY, payload TEXT"
                                    + existing + ")"))
            connection.execute(text("INSERT INTO notification_outbox (id,payload) VALUES (1,:payload),(2,:bad)"), {
                "payload": json.dumps({"execution_id": 7, "event_key": "execution:7"}),
                "bad": "not-json",
            })
            _apply_sqlite_migrations(connection)
            _apply_sqlite_migrations(connection)
            rows = connection.execute(text(
                "SELECT id, execution_id, event_key FROM notification_outbox ORDER BY id"
            )).all()
            assert rows == [(1, 7, "execution:7"), (2, None, None)]
            indexes = {row[1] for row in connection.execute(text("PRAGMA index_list(notification_outbox)"))}
            assert {"ix_notification_outbox_execution_id", "ix_notification_outbox_event_key"} <= indexes
    finally:
        engine.dispose()


async def test_startup_schema_upgrade_failure_rolls_back_ddl(monkeypatch):
    # Schema 升级失败不得把半迁移表留给下一次启动，也不接触真实数据库。
    engine = database.get_engine()

    def fail_upgrade(connection):
        connection.execute(text("CREATE TABLE upgrade_rollback_probe (id INTEGER PRIMARY KEY)"))
        raise RuntimeError("synthetic schema upgrade failure")

    monkeypatch.setattr(database, "_apply_sqlite_migrations", fail_upgrade)
    with pytest.raises(RuntimeError, match="schema upgrade failure"):
        await database.init_db()
    async with engine.connect() as connection:
        assert await connection.scalar(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='upgrade_rollback_probe'"
        )) is None
