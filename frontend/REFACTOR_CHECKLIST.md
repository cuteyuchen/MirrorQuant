# MirrorQuant 前端重构交付清单

核对日期：2026-11-15。范围：仅 `frontend/`，包含上个会话保留的改动和本次接续收尾。

## 状态说明

- “已做”表示源码已实现，并通过下述构建或对应自动测试；不等于真实后端、实盘交易或浏览器视觉验收已通过。
- “未做”明确列出尚未实现、接口不能支持或未执行的验收。接口限制详见 L1-L7。
- 已先盘点 `git status`、`git diff -- frontend`，没有推倒既有前端改动。
- 没有编辑 `backend/` 或根目录部署文件，没有提交、推送、切分支、checkout 或 reset。工作区中这些目录的其他改动保留。

## 第一阶段：跟单看板

| 项目 | 已做 | 未做 | 原因或限制 |
| --- | --- | --- | --- |
| 全局状态条 | `App.vue` 挂载急停、实时流状态、逐秒数据年龄、告警数量及列表；急停整条红色 | 真实连接状态联调 | L2；登录后各页面共用，认证页不请求私有数据 |
| 紧凑账户摘要 | 逐账户权益、今日盈亏金额和百分比、未实现盈亏、LIVE/DEMO；正负号和颜色同时表达 | 实际挂屏首屏验收 | L1；已补长名称及大额数字的收缩、换行约束 |
| 持仓对照主表 | 带单员开/平笔数、今日已实现、快照年龄；品种/方向、数量、均价、重字号现价、盈亏%、止盈止损、我的持仓与状态 | 超大持仓数量时首屏容量验收 | L1；数据本身超过首屏容量时仍需纵向滚动，不隐藏账户行 |
| 跟单五种状态 | 已跟上、部分跟上及我的量/应有量、未跟、被拦截、未知；异常行高亮、原因翻译 | 真实执行与持仓状态一致性联调 | L2；状态消费后端契约，不在浏览器重算对齐 |
| 排序及残留仓 | 今日新仓优先，其余按开仓时间倒序；多个绑定账户分行；orphans 单独置顶告警 | 实际残留仓交易验证 | L2；界面不自动执行平仓 |
| 空仓及未知 | 明确空仓、未知仓位分别显示；隐藏/未知公开性下的空数组不冒充已平仓 | 无代码缺口 | 所有未知资金、数量、价格保留为“—” |
| 今日动态 | 时间倒序开/加/减/平，紧邻对应账户成交量、价格、滑点或失败/跳过 details | 真实事件流联调 | L2；所有已知动作/状态枚举翻译 |
| 深度分析移位 | 总览移除拓扑及重复对齐图；深度分析、仓位计算器移到 `/leads/:id` 下部页签 | 无代码缺口 | 主表与详情复用对照组件 |
| 设计及响应式 | 集中主题色、一个强调色及盈亏/警示语义色；最小 12px、系统字体、等宽右对齐数字；桌面表格、手机逐持仓卡片 | 1440/375 无溢出、无重叠及第一眼信息层级的截图验收 | L1；静态约束与组件测试不能替代浏览器验收 |
| 契约及样例 | `types/api.ts` 完整可空 board/intents/executions 类型；`mocks/board.ts` 覆盖五状态、多账户、空仓、未知、过期、残留仓及今日动态 | 样例下模拟写入/交易 | 样例模式明确只读，所有写入拒绝，不伪造保存或成交成功 |

## 第一阶段：20 项修复

| 编号 | 已做 | 未做 | 原因或限制 |
| --- | --- | --- | --- |
| 1 | 设置按当前页签构造 payload，绝不回写 `global_kill_switch`；无未保存修改时激活强制刷新；422 `detail[]` 映射字段提示 | 真实 422 联调 | L2；有组件和 payload 回归 |
| 2 | 急停/恢复、绑定启停、熔断/欠单重置、四类删除、批量应用默认、关闭或减弱保护统一确认和异步 loading/toast/错误翻译；恢复及 LIVE 强确认；同步带单也确认 | LIVE 真实操作验收 | L2；另加退出后的会话代次及确认框所有权保护 |
| 3 | 共用红底 LIVE/灰底 DEMO `EnvBadge`，用于看板、管理行、执行流和确认框；新建 LIVE 绑定默认停用 | LIVE 新建与启用联调 | L2 |
| 4 | 全局 401 清会话并保留安全 redirect 跳登录；AbortController 请求超时 | 真实超时/失效 cookie 联调 | L2；自动测试覆盖 |
| 5 | 主动断开禁止重连、1008 登出、指数退避加抖动、心跳超时重连、公开连接状态 | 实际 WebSocket 长时间运行验收 | L2；自动测试覆盖主动断开及心跳等场景 |
| 6 | WebSocket `eventSequence` 自增；App/执行页监听序号，不监听固定长度 50 的数组 | 无代码缺口 | 满额事件流仍触发刷新，测试覆盖 |
| 7 | 全站共享逐秒 `useNow` 和 `FreshnessStamp`；快照直接使用后端 stale/age；删去 alignment 重复本地过期推断 | 后端时钟偏差联调 | L2；公开投影不双重累加年龄，未知年龄不冒充新鲜 |
| 8 | 路由三态区分未知/未初始化/已初始化；未知进入连接失败重试页；补 404；所有视图懒加载 | 真实后端断连重试流程 | L2；三态守卫自动测试通过 |
| 9 | 缓存请求序号只接受最新结果；登出作废在途；独立带单详情按 ID/序号防止迟到覆盖；危险操作迟到结果不污染新会话 | 无代码缺口 | 并发及退出回归通过 |
| 10 | 账户和带单员启停、名称/代理编辑、删除确认，409 原因翻译 | 全部敞口限制的前置推断 | L3；后端仍是最终拒绝依据 |
| 11 | 新执行记录页：绑定/状态筛选、分页、details/订单号/滑点抽屉；待处理订单页签及导航角标；旧 `/intents` 兼容跳转 | 大规模执行历史性能验收 | L6；按服务端精确状态分组查询，筛选不限于当前页 |
| 12 | 白名单、黑名单、映射与冲突校验；范围集中 `constants/validation.ts`，绑定/设置共用；空或非法数字不能提交 | 活动保护单的完整空仓前置校验 | L3；当前 schema 已只读核对，未知敞口保守锁定 |
| 13 | 设置删除重复账户/代理页签；代理页用 `url`/`region`；共用账户表单；补钉钉、四个 notify 开关、连续失败阈值；只读安全区没有通用 Save | 独立单渠道通知测试、未保存凭据纯测试 | L4、L5；界面已明确测试范围及保存副作用 |
| 14 | 创建账户只提供 OKX、币安 | 无代码缺口 | 没有启用后端默认禁用的交易所 |
| 15 | `BaseModal` 对话框语义、焦点陷阱/恢复、Esc/遮罩关闭及 90vh 滚动；统一确认/徽标/toast；删除原生 alert/confirm；图标 aria-label、浏览器缩放、focus-visible | 完整辅助技术和真实键盘走查 | L1；组件自动测试通过 |
| 16 | 视图 `any` 改类型；设置拆五页签组件；深度分析拆四组件；SVG 渐变唯一 ID、业务 key、逐秒时间更新 | 浏览器中全部图表布局验收 | L1；拆分和未知字段回归通过 |
| 17 | format null 为“—”、小量有效数字、USDC 后缀；计算器缺价不以 1 替代；文案 i18n；不完整时禁用汇总/复制计划 | 测算与交易所真实强平价校准 | 不把测算宣称为实际强平价；界面已说明边界 |
| 18 | 页面文案双语，清理旧 Dashboard/导航死 key 和无效类；退出无论请求结果均清状态并跳转；TOTP 启/停各自分步及 one-time-code | 真实 TOTP、密码修改、通知外发 | L2；没有执行账户安全变更 |
| 19 | Vite dev `/mq/api`、`/mq/ws` 去前缀代理；host 默认 localhost；TS Bundler 解析 | 实际部署反向代理验收 | 不修改根部署文件；L2 |
| 20 | 引入 Vitest、Vue Test Utils、jsdom；覆盖设置急停、Socket、路由三态、危险确认、方向归一化、样例看板；原有 3 个测试文件继续参与 `npm test` | 真浏览器端到端测试 | L1；最终 15 + 147 项全部通过 |

## 第二阶段：统一骨架

| 项目 | 已做 | 未做 | 原因或限制 |
| --- | --- | --- | --- |
| 页面骨架 | `PageHeader` 一句话汇总和主操作；`DataTable`、Empty/Error/Loading 三态；通用数字输入、更多菜单 | 真实截图逐页验收 | L1；新增骨架组件自动测试 |
| 列表层级 | 管理页紧凑行/表格，异常优先或行内原因，正常行安静；手机卡片；常用操作控制在两个内，其余放更多 | 375 宽真实触控/长文本验收 | L1 |
| 表单统一 | 分组、必填、行内错误、单位后缀、风险说明、默认差异标记；长策略折叠分区 | 每个表单在真实后端保存及 409/422 走查 | L2、L3 |
| 样例模式 | 全部页面共用 `src/mocks/` 中本地样例，包含连接错误、过期、未知及失败状态 | 模拟保存后状态持久化 | 只读保护是有意边界，不把本地假成功当真实功能 |

## 第二阶段：逐页清单

| 编号 / 页面 | 已做 | 未做 | 原因或限制 |
| --- | --- | --- | --- |
| 1 带单员 | 名称头像、开关、持仓数、今日开/平/盈亏、最近动作、快照年龄、跟随账户数；异常优先；独立 `/leads/:id`，上部复用对照/动态，下部收益曲线/绩效/历史仓位/原始订单/测算；简洁添加表单和 Portfolio ID 来源 | 数据缺失时补出最近动作品种/方向；真实图表截图 | 数据来源全部缺失时显示未知，不虚构；L1、L2 |
| 2 账户 | 交易所/环境、权益/可用/今日/未实现/高水位回撤/同步/状态/开关；异常和历史风险基线说明；展开当前仓位；共用新建/编辑/表单内测试结果 | 无副作用的未保存凭据测试 | L5；未执行真实交易所测试，L1、L2 |
| 3 绑定 | 带单员到账户、环境、开关、比例、熔断/停用/急停/待处理状态、持仓与欠单、今日成功/失败；急停横幅；基础/风控/止盈止损/品种过滤分区、默认差异、有仓不可改字段说明 | 全部活动保护单前置锁定；大量今日执行统计性能及一致性验收 | L3、L6；有状态汇总和危险操作测试 |
| 4 执行记录 | 五类计数分段筛选、绑定筛选、倒序分页、失败原因行内高亮、详情抽屉；待处理页签和导航角标 | 跨状态查询的数据库一致快照 | L6；分类查询合并排序及分页测试通过 |
| 5 代理 | 脱敏地址、区域、启用、健康/延迟/检测时间/错误、配置使用方；全部检测；统一新增编辑与危险删除确认 | 自动选路后的真实使用方展示 | L7；没有宣称配置引用等于实际出口 |
| 6 设置 | 通知/默认风控/韧性/安全/公开看板五子组件；独立保存、未保存提示与离开确认；通知折叠渠道及内联测试结果；密码和 TOTP 独立流程 | 真正单渠道投递测试 | L4；没有操作真实安全配置，L2 |
| 7 公开看板 | 只读复用持仓组件、不含我的列/账户/残留仓/执行详情；挂屏字号、连接/过期横幅；AudioContext 复用并卸载关闭 | 完整今日聚合计数及真实音频挂屏验收 | 公开接口只有截断快照，无法推断完整今日总数；显示未知；L1、L2 |
| 8 认证和错误页 | Login/Setup/连接失败/404 共用居中 AuthLayout；表单内错误、锁定倒计时、验证码 img；样例预览登录与初始化流程 | 真实认证挑战/恢复链路 | L2；样例状态和路由回归通过 |
| 9 App 外壳 | 桌面监控/配置/系统分组、子路由正确高亮；手机看板/执行/带单员/绑定四入口加更多；登录后共用状态条 | 1440/375 导航及信息层级截图 | L1；所有入口均可达，样例角标使用真实样例 intents 数量 |

## 未完成的验收与接口边界

| 编号 | 未做 | 原因 | 当前处理 |
| --- | --- | --- | --- |
| L1 | 每页 1440 宽、375 宽截图；真实无横滚/重叠、首屏易读、辅助技术验收 | 当前浏览器工具不可用或操作未获批准；本机 headless Chrome 的 mojo/crashpad 命名管道拒绝访问 | 没有绕过权限，没有成功截图；仅完成响应式源码、长文本/数字约束、组件测试。失败日志在本地忽略目录 `output/playwright/chrome-error.log` |
| L2 | 真实 HTTP/WS、LIVE 跟单、交易所测试、通知外发、密码/TOTP 变更、部署联调 | 本次只做前端和本地只读样例；不启动交易、不修改并行后端或生产配置 | 明确保留联调验收；mock 不证明真实 side effect |
| L3 | 前端提前识别全部活动保护单和非终态订单 | 固定 board/intents 契约不暴露完整交易敞口 | 已对已知持仓、UNKNOWN/SUBMITTING 和未知数据锁定相关字段；剩余由后端 409 最终拦截并翻译 |
| L4 | 每个渠道真正独立测试 | 现有通知测试端点没有渠道参数，只能发送到全部已保存且启用渠道 | 每个渠道卡片展示自己的返回结果，发送前明确提示全渠道范围；不伪造单渠道接口 |
| L5 | 未保存账户凭据的无副作用测试 | 现有测试端点按账户 ID 调用 | 新账户先确认保存为停用再测试；表单内显示保存/测试各自结果，不自动启用 |
| L6 | 大量执行记录性能、多个状态/分页统计的数据库级一致快照 | 现有 executions 只支持精确状态和分页，没有分类/今日聚合接口 | 精确状态分组后合并倒序分页；今日统计固定截止时间、分页至当天起点并去重；不宣称数据库事务快照 |
| L7 | 代理自动路由后的实际使用方 | 后端代理/账户/带单员响应只有配置引用，不暴露运行时实际出口 | 显示配置 `proxyId` 关联，明确是配置引用 |

另：韧性 `flip_close_first` 是后端兼容字段，执行依真实事件顺序。未提供一个暗示能改变实际顺序的伪开关。

## 已执行的验证

- 最后源码修正后，无 mock 配置的普通 `npm run build` 成功：类型检查及正式 Vue/Vite 编译通过，1703 个模块。
- `npm test` 成功：原有 3 个 Node 测试文件共 15 项；17 个 Vitest 文件共 147 项；共 162 项，失败/跳过均为 0。
- 覆盖设置安全、会话/缓存竞态、危险确认、Socket 断开与心跳、路由三态、方向/未知值、看板五状态、账号/安全流程、骨架、绑定汇总、执行筛选/分页、i18n key 与占位符对齐及实际调用缺 key 扫描。
- `git diff --check -- frontend` 通过。已有 Git LF/CRLF 提示不是校验错误。
- 语义色静态对比度核算已做：检查组合的最低文字对比度 5.13:1，LIVE/急停白字 6.79:1；这不是整站 WCAG 审计认证。
- 本机样例版也使用普通正式构建生成，没有替换生产编译器。仅临时加入 `.env.production.local` 的 `VITE_MOCK=1`，构建后立即删除。
- 当前预览 HTML、主脚本、CSS 均已检查 HTTP 200；主脚本包含 `MOCK_READONLY` 写入保护。

## 本机预览

地址：<http://localhost:5181/mq/>。当前后台预览只监听本机，使用只读样例版 `dist/`。

| 页面 | 路径 |
| --- | --- |
| 跟单看板 | `/mq/dashboard` |
| 带单员 / 详情 | `/mq/leads`、`/mq/leads/1` |
| 账户 | `/mq/accounts` |
| 绑定 | `/mq/bindings` |
| 执行记录 / 待处理 | `/mq/executions`、`/mq/executions?tab=intents` |
| 代理 | `/mq/proxies` |
| 设置 | `/mq/settings` |
| 公开监控 | `/mq/public` |
| 登录样例 / 初始化样例 | `/mq/login?preview=1`、`/mq/setup?preview=1` |
| 连接失败 / 404 | `/mq/connection-error?preview=1`、`/mq/not-a-page` |

常规开发的样例入口是 `npm run dev -- --mode mock`，对应保留的 `.env.mock`。普通 `npm run build` 默认不启用 mock；再次普通构建会替换当前样例 `dist/`。`dist/`、依赖和本机验收日志未加入源码列表。

## 完整改动文件列表

共 112 个前端文件：32 个已跟踪文件的修改，80 个新增文件。该列表包含接续前已存在的未提交改动，不代表这些文件全部由本次收尾新写。

```text
frontend/.env.mock
frontend/.gitignore
frontend/REFACTOR_CHECKLIST.md
frontend/index.html
frontend/package-lock.json
frontend/package.json
frontend/src/App.vue
frontend/src/api/client.ts
frontend/src/api/mode.ts
frontend/src/components/AccountFormModal.vue
frontend/src/components/AuthLayout.vue
frontend/src/components/BaseModal.vue
frontend/src/components/ConfirmDialog.vue
frontend/src/components/DataTable.vue
frontend/src/components/EmptyState.vue
frontend/src/components/EnvBadge.vue
frontend/src/components/ErrorState.vue
frontend/src/components/FreshnessStamp.vue
frontend/src/components/IntentList.vue
frontend/src/components/LangToggle.vue
frontend/src/components/LeadTelemetryPanel.vue
frontend/src/components/LoadingSkeleton.vue
frontend/src/components/MoreMenu.vue
frontend/src/components/NumberField.vue
frontend/src/components/PageHeader.vue
frontend/src/components/PositionCalculatorCard.vue
frontend/src/components/RiskPolicyFields.vue
frontend/src/components/StatusBadge.vue
frontend/src/components/ToastHost.vue
frontend/src/components/board/AccountSummary.vue
frontend/src/components/board/FollowStatus.vue
frontend/src/components/board/GlobalStatusBar.vue
frontend/src/components/board/LeadComparison.vue
frontend/src/components/board/LeadTimeline.vue
frontend/src/components/board/MinePosition.vue
frontend/src/components/settings/NotificationSettingsPanel.vue
frontend/src/components/settings/PublicSettingsPanel.vue
frontend/src/components/settings/ResilienceSettingsPanel.vue
frontend/src/components/settings/RiskSettingsPanel.vue
frontend/src/components/settings/SecuritySettingsPanel.vue
frontend/src/components/telemetry/OrderHistory.vue
frontend/src/components/telemetry/PerformancePanel.vue
frontend/src/components/telemetry/PositionAnalysis.vue
frontend/src/components/telemetry/RoiChart.vue
frontend/src/composables/useAsyncAction.ts
frontend/src/composables/useNow.ts
frontend/src/constants/bindingStatus.ts
frontend/src/constants/telemetry.ts
frontend/src/constants/validation.ts
frontend/src/i18n/auth-extra.ts
frontend/src/i18n/bindings-extra.ts
frontend/src/i18n/executions-extra.ts
frontend/src/i18n/index.ts
frontend/src/i18n/messages.ts
frontend/src/i18n/settings-extra.ts
frontend/src/main.ts
frontend/src/mocks/board.ts
frontend/src/router/guard.ts
frontend/src/router/index.ts
frontend/src/stores/auth.ts
frontend/src/stores/board.ts
frontend/src/stores/dataCache.ts
frontend/src/stores/socket.ts
frontend/src/stores/ui.ts
frontend/src/style.css
frontend/src/types/api.ts
frontend/src/types/ui.ts
frontend/src/utils/alignment.ts
frontend/src/utils/errors.ts
frontend/src/utils/executions.ts
frontend/src/utils/format.ts
frontend/src/utils/publicBoard.ts
frontend/src/utils/settings.ts
frontend/src/utils/sizing.ts
frontend/src/utils/status.ts
frontend/src/views/AccountsView.vue
frontend/src/views/BindingsView.vue
frontend/src/views/ConnectionErrorView.vue
frontend/src/views/DashboardView.vue
frontend/src/views/ExecutionsView.vue
frontend/src/views/LeadDetailView.vue
frontend/src/views/LeadsView.vue
frontend/src/views/LoginView.vue
frontend/src/views/NotFoundView.vue
frontend/src/views/ProxiesView.vue
frontend/src/views/PublicDashboardView.vue
frontend/src/views/SettingsView.vue
frontend/src/views/SetupWizard.vue
frontend/tailwind.config.js
frontend/tests/account-safety.spec.ts
frontend/tests/alignment.test.mjs
frontend/tests/auth-preview.spec.ts
frontend/tests/bindings-safety.spec.ts
frontend/tests/bindings-summary.spec.ts
frontend/tests/board.spec.ts
frontend/tests/calculator-safety.spec.ts
frontend/tests/executions.spec.ts
frontend/tests/format.test.mjs
frontend/tests/i18n.spec.ts
frontend/tests/lead-detail.spec.ts
frontend/tests/management-pages.spec.ts
frontend/tests/page-scaffold.spec.ts
frontend/tests/security-flows.spec.ts
frontend/tests/settings-safety.spec.ts
frontend/tests/settings.test.mjs
frontend/tests/telemetry.spec.ts
frontend/tests/transport-auth-pages.spec.ts
frontend/tests/transport.spec.ts
frontend/tests/ui.spec.ts
frontend/tsconfig.json
frontend/vite.config.ts
frontend/vitest.config.mjs
```
