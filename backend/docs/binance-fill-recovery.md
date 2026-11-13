# Binance 带单成交去重修复与重建验收

有 `orderId` 的记录继续使用原有订单身份，q/p 的变化不改变 key；无 `orderId` 的记录在原有时间、合约、方向、持仓方向、类型之外，仅加入规范化后的 `executedQty` 和 `avgPrice`。不使用未经确认的 `id`、`positionId`、`orderKey`，不使用更新时间或页内序号。`fillKind` 明确标记累计订单或独立成交。

累计订单的数量回退仍抛出 `Regressing cumulative lead fill`。逐笔成交不执行累计回退检查；同一逐笔身份的数量或价格冲突直接拒绝。采集、存量加载、持仓重建、发布和路由校验有限正数量与价格，NaN、Inf、零和负数量均 fail-closed。

这次只修改 `backend/`。未修改任何既有测试及其期望；未访问线上数据库、未重启服务、未 push。

## 代码位置

| 文件:行号 | 变更 |
| --- | --- |
| `app/services/lead_collector/binance_bapi.py:43` | 两种身份、q/p 十进制规范化、累计回退与逐笔冲突校验 |
| `app/services/lead_collector/binance_bapi.py:104` | 保存 `fillKind`，保留旧游标别名，重复同步结果稳定 |
| `app/services/lead_collector/binance_bapi.py:172` | 派生持仓拒绝非法存量数量 |
| `app/services/lead_collector/binance_bapi.py:783` | 原子采集、旧订单身份恢复、重建要求完整历史且拒绝容量溢出 |
| `app/services/lead_collector/binance_bapi.py:864` | 重建接受完整已抓取的 position-history，不截断为 80 条 |
| `app/services/engine/router.py:555` | 累计订单取增量，独立成交只记账一次，进度冲突拒绝路由 |
| `app/services/engine/orchestrator.py:329` | 每次失败 ERROR，持久化失败次数，故障通知去重 |
| `app/services/engine/orchestrator.py:363` | 独立成交发布校验，拒绝重建前启动的旧采集结果 |
| `app/services/lead_collector/freshness.py:11` | 共用原始生成时间和陈旧判断 |
| `app/api/dashboard.py:195` | 公开返回 `stale/generatedAt/snapshotAgeSec/consecutiveSyncFailures` |
| `app/main.py:100` | `/api/ready` 返回 `leadSync` 降级信息，HTTP 503 |
| `app/core/config.py:95` | 默认陈旧阈值 300 秒，连续失败阈值 N=3 |
| `app/maintenance/__init__.py:1` | 显式运维模块入口，不在应用启动时执行 |
| `app/maintenance/rebuild_lead_store.py:46` | 带 UTC 时间戳的备份、0600 权限、文件及目录 fsync、绝对路径日志 |
| `app/maintenance/rebuild_lead_store.py:95` | prepare：先备份，完整抓取两种历史，从空 store 重建 |
| `app/maintenance/rebuild_lead_store.py:152` | apply：再次备份，条件更新，幂等替换 |
| `app/maintenance/rebuild_lead_store.py:193` | rollback：恢复原始 JSON 和状态，较新数据须显式允许且先备份 |
| `tests/test_bapi_fill_recovery.py:1` | 本次全部新增回归测试，包括题述脏记录和 SPCX 1425.17 夹具 |
| `docs/binance-fill-recovery.md:1` | 本报告和执行命令 |
| `docs/binance-fill-recovery-pytest-results.md:1` | 最终全量 pytest 统计、每个测试实例名称与实际结果 |

## 可观测性

`generatedAt` 为 UTC epoch 毫秒，来自快照的原始 `meta.generatedAt`；老快照没有该字段时回退到最后成功同步时间。失败不会刷新该时间。没有有效生成时间、生成时间在未来、超过阈值或同步状态为 error 时，`stale=true`。公开接口同时返回快照年龄和失败次数。

同步失败且快照超过 300 秒，或连续失败达到 3 次，使用既有 `NotificationOutbox` 和当前通知配置加入一次 error 通知。标志与错误状态在同一事务保存；成功同步清除故障计数和通知标志，新的故障可再次通知。外部通知送达依赖既有已启用的通知渠道。每次失败都记录 ERROR，`/api/ready` 对陈旧或连续失败的启用带单返回 HTTP 503 和 `degradedLeadIds`；`/api/health` 保留进程存活检查的语义。

可用 `MQ_LEAD_SNAPSHOT_STALE_SEC`、`MQ_LEAD_SYNC_FAILURE_LIMIT` 配置阈值。

## 执行重建

以下命令由验收代理执行。本次仅编写和在隔离测试数据库验证脚本。运行目录为 `backend/`，使用服务相同的 MQ 配置和数据库配置。脚本复用数据库中的现有代理，不在参数中传入凭据。

先备份和生成可审阅 bundle，数据库不发生写入：

```bash
./.venv/bin/python -m app.maintenance.rebuild_lead_store prepare \
  --lead-id 1 --backup-dir ./data/lead-rebuild-backups
```

日志中的 `backup artifact=` 是含原始 `orders_store_json`、`snapshot_json`、closed cache、seen keys、最后成功时间和状态的时间戳文件；`bundle artifact=` 是完整重建的固定输入结果。审阅 bundle 的 `after` 后，使用日志给出的实际 bundle 路径：

```bash
./.venv/bin/python -m app.maintenance.rebuild_lead_store apply \
  --bundle "./data/lead-rebuild-backups/<日志中的bundle文件名>.json" \
  --backup-dir ./data/lead-rebuild-backups
```

apply 在写库前生成第二份 `rollback artifact=`，备份应用时的真实状态。相同 bundle 执行两次不改动已重建的 store/snapshot；重新 prepare 同样的完整历史后再 apply 也不改动结果。整条命令使用既有跨进程执行锁，且条件更新拒绝并发写入；重建时间标志阻止重建前已启动的采集结果再发布。prepare 后仅有失败元数据变化可以应用，新的成功快照或 store 变化要求重新 prepare。

需要单条命令完成同一流程时：

```bash
./.venv/bin/python -m app.maintenance.rebuild_lead_store rebuild \
  --lead-id 1 --backup-dir ./data/lead-rebuild-backups
```

重建从空 store 抓取 order-history，直到最后一页；position-history 校验总数、分页重复及闭仓区间。不完整、页数超限、超过现有 6000 条 store 容量、非法数量等情况均拒绝写入并保留备份。替换整个派生 store，不按特定哈希逐条修补。重建历史加入已见 keys，防止修复后的旧成交被重新下发为跟单事件；不修改交易执行记录或持仓保护逻辑。重建快照先标记为非权威，正常同步后才可授权金融路由。

应用后由验收代理触发 `POST /api/leads/1/sync`，检查 `ok=true`、`last_error` 清空，并确认公开快照只剩 `SPCXUSDT SHORT 1425.17`。线上结果需由验收代理确认；本地夹具已验证这一数量及 ETH 消失。

## 回滚

使用 apply 日志中的 `rollback` 文件，不能使用 prepare 阶段的原始 backup 代替：

```bash
./.venv/bin/python -m app.maintenance.rebuild_lead_store rollback \
  --backup "./data/lead-rebuild-backups/<日志中的rollback文件名>.json" \
  --backup-dir ./data/lead-rebuild-backups
```

回滚恢复备份中的原始 JSON、最后同步时间、seen keys 和状态；重复执行结果不变。如果应用后同步已更新源数据，默认拒绝覆盖。验收后决定覆盖这些较新数据时，显式执行：

```bash
./.venv/bin/python -m app.maintenance.rebuild_lead_store rollback \
  --backup "./data/lead-rebuild-backups/<日志中的rollback文件名>.json" \
  --backup-dir ./data/lead-rebuild-backups --allow-newer
```

这条命令先生成 `rollback-rescue` 时间戳备份，再恢复原始状态；该 rescue 文件同样可以作为 `--backup`，用于撤销此次覆盖。回滚只恢复数据库派生状态，代码版本回滚由验收代理另行处理。

## 测试

最终运行命令：

```bash
./.venv/bin/python -m pytest -q --junitxml=/tmp/mirrorquant-fill-recovery-pytest.xml
```

最终结果：**591 passed, 1 warning in 44.23s**（新增 32 项、既有 559 项，失败/错误/跳过均为 0）。所有测试的名称、参数和结果见 [pytest 结果](binance-fill-recovery-pytest-results.md)。该结果文件从实际 JUnit XML 生成，包含本次新增测试和全部既有测试。既有测试未做任何修改，也没有修改既有期望值。
