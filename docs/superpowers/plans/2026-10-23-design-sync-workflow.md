# Design Sync Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按用户选择的 B 同步菜单，提供所选器件的字段/封装分类型同步、可靠 PCB 连接与独立工具窗口。

**Architecture:** 保留现有 CAD 事务，新增薄的设计同步协调器，分阶段调用原理图事务和 PCB 事务。可信操作意图与源快照共同确定封装改动类型；宿主统一管理工具窗口，前端消费真实任务事件，不另建一套 CAD 写入逻辑。

**Tech Stack:** Python、现有 S-expression 解析器、KiCad 10 原生 IPC/kipy、pywebview/WebView2、原生 JavaScript/CSS、pytest、Playwright。

**Spec:** `docs/superpowers/specs/2026-10-23-design-sync-workflow-design.md`，用户已选择 B。

**Prototype:** 本地独立分支 `prototype/design-sync-workflow-20261023`，提交 `44fb32b08347be760f043c194e8138fd2b29eafa`，源文件在 `prototype/design-sync/`；只是交互参考，不进入 main 或安装包。用户选择 B 单一同步菜单，范围独立于目的地。

## Global Constraints

- 全部同步指原理图与 PCB 两个目标，不代表全部行；范围单独选择。
- 只处理现有器件，不添加/删除器件，不改变网络或走线；PCB 不自动保存。
- 原理图或 PCB 入口能力相同。PCB 忙碌、IPC 状态不明或多会话时不重复打开编辑器。
- 仅改名只改完整库标识，保留实例几何、位姿及子对象；历史记录不凭名称猜改动类型。
- PCB 同步包含 Value、Comment 和现有 `field_mapping` 支持的物料属性，兼顾内置字段与自定义别名。
- 中心对齐不缩放、不镜像、不改编号；允许焊盘移动必须明确确认并提示连接/DRC 检查。
- 预览不写记录或库。发布结果不明先只读核验，不能盲目重放或全局撤销。
- 使用 `scratch_project` / `temporary_project()`；生产工程、真实订单及真实选料记录不作为写入测试对象。
- 临时日志、截图、备份及转换在工程外。每轮改动先非破坏式 stash；不覆盖其他人的修改。
- Windows 子进程不弹命令行；用户 CAD 编辑器保持正常可见。不结束用户进程。
- 全套回归和原生流程通过才提交/推送/重新安装并校验字节；不将源码推送称作正式 Release。

## Review Focus

1. PCB 中存在未保存的字段/位姿修改：预检后任何相关变化必须使旧令牌失效，不能覆盖用户刚改的内容（Tasks 3、5、8）。
2. 同名库文件内容已变或不同名但原几何相同：操作证据和版本决定路径，不能用名字判定或永久信任旧标记（Tasks 1、3、8）。
3. 映射别名、空值与重复大小写字段：保持内置值一致，缺失不等于清空，冲突拒绝（Tasks 3、5）。
4. 原理图已写完时编辑器断连或宿主轮询丢失：保留成功阶段，按任务查询，拒绝重做未确认写入（Tasks 5、6、8）。
5. 子窗输入未保存、异步结果迟到或主窗正在退出：不丢编辑、不复活关闭窗口、不让其他工程接收结果（Tasks 2、6、7）。

## File Map

- `tools/part_selection_footprint_changes.py`：可信封装操作证据、分类，不负责发布库或写 PCB。
- `tools/part_selection_design_source.py`：单次读取/解析的原理图、映射、关联与内容版本快照。
- `tools/part_selection_pcb_connection.py`：目标 PCB 解析、会话状态发现与只在确认未打开时启动。
- `tools/part_selection_design_sync.py`：预检令牌、阶段协调、继续剩余阶段与幂等状态。
- `tools/part_selection_tool_windows.py`：宿主所属原生工具窗注册表和最小桥接。
- `tools/web/design-sync.js/.css`：B 同步菜单、差异/确认/进度页面，消费阶段状态。
- `tools/web/tool-desktop.js`：主窗与工具页窄消息协议、状态恢复。
- 现有 `part_selection_pcb.py` / `part_selection_pcb_session.py`：继续负责可验证原生 PCB 事务。
- 现有 `part_selection_server.py`：注入上述组件、提供 API 和挂载入口，不把新流程大段塞入嵌入脚本。
- 现有 `part_selection_candidate_window.py`：向新注册表的兼容适配，保留旧候选调用。
- 每个任务同步登记 `package_plugin.py` 的新资源，保持源模式/安装模式可用。

## Verification Commands

仓库根目录 PowerShell，先设置：

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONPATH=(Join-Path (Get-Location) 'tools')
$env:NODE_PATH='C:\Users\admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules'
```

下文测试命令均使用 `python -B -m pytest ... -p no:cacheprovider`。新增测试的首次运行必须失败在缺失实现/行为断言，不能把导入环境错误当作业务失败。

## Task 1: Persist Footprint Intent Without Guessing

**Files:** Create `tools/part_selection_footprint_changes.py`；modify `tools/part_selection_footprints.py`、`tools/part_selection_server.py` 的 `row_footprint` / `select_candidate` / `save_field_override`、`tools/part_selection_overrides.py`；test `tests/test_footprint_change_types.py`、existing `tests/test_row_footprint.py`。

**Interfaces:**
- `make_footprint_change(*, operation: str, before: str, after: str, source: dict, target: dict) -> dict`，operation 仅 `rename` / `replace`。证据 schema=1，包含完整标识、来源 UUID/局部几何签名及文件内容哈希，按位号保存到记录的 `footprint_change`。
- `classify_footprint_change(record: dict, live: dict) -> dict`，返回 `kind` 为 `rename` / `replace` / `none` / `unknown`，另有 `reason`；只有匹配证据才返回 rename。same-ID replace 仍返回 replace。
- `save_field_override(..., footprint_change=None)` 新增仅关键字内部参数，不能接受未经服务端预览校验的客户端证据。

- [ ] 写失败测试 `test_explicit_rename_has_bound_evidence`、`test_same_name_replace_is_not_rename`、`test_old_name_difference_is_unknown`、`test_stale_geometry_rejects_rename`；断言旧记录不被自动改写，重新编辑/撤回恢复或清除对应意图，合行仍保留逐位号证据。
- [ ] 运行 `python -B -m pytest tests/test_footprint_change_types.py tests/test_row_footprint.py -p no:cacheprovider -q`，确认新增断言失败。
- [ ] 在现有库发布和记录保存的同一成功路径捕获服务端证据；失败连同记录/库一起回滚。手写 Footprint 改动清除旧意图，不能沿用先前 rename。
- [ ] 重跑上述测试及 `tests/test_candidate_selection.py tests/test_workbench_undo.py`，全部通过。
- [ ] 提交：`feat: record explicit footprint change intent per reference`。

## Task 2: Native Tool Window Registry

**Files:** Create `tools/part_selection_tool_windows.py`、`tools/web/tool-desktop.js`；modify `tools/part_selection_candidate_window.py`、`tools/part_selection_lifecycle.py`、`tools/kicad_plugin/desktop.py`、`tools/part_selection_server.py`；test `tests/test_tool_windows.py`、existing candidate/lifecycle tests。

**Interfaces:**
- `ToolWindows(lifecycle, create_window, on_loaded=None)` 提供 `main_action(action: str, value=None) -> dict`、`check_exit()`、`cancel_exit()`、`dispose()`。
- `open` 的 value 仅 `{kind, request_id, payload}`；kind 使用固定白名单，payload 各工具独立校验，目标 URL 固定为本宿主 `?view=tool&tool=<kind>`，不接收任意 URL。
- 子桥接 `tool_window(action, value=None)` 只允许本工具的 ready/state/changed/minimize/close 及白名单工具请求；无后台退出、任意文件或安装权限。
- 页面 `receiveNativeToolRequest(request)`、`prepareNativeToolExit()`、`cancelNativeToolExit()` 与主页 `nativeToolChanged(message)`。消息同时绑定 host/project/request，旧请求不得覆盖新请求。

- [ ] 写失败测试覆盖同类窗复用、立即 show 后再异步加载、独立原生标题栏、跨工程/任意 URL/超长 payload 拒绝、子窗退出权限拒绝，以及未保存/忙碌工具阻止主窗退出。
- [ ] 运行 `python -B -m pytest tests/test_tool_windows.py -p no:cacheprovider -q`，确认失败在新增接口。
- [ ] 实现注册表，保留 `candidate_window` 的兼容适配；移除在宿主生命周期锁内等待页面 ready 15 秒的路径，用请求代次与有界就绪消息异步交付。
- [ ] 跑新测试和候选窗口/生命周期已有测试；确认正常关闭不销毁用户 KiCad，工具缩小恢复保留输入。
- [ ] 提交：`feat: add owned native tool window registry`。

## Task 3: Shared Source Snapshot and PCB Field Operations

**Files:** Create `tools/part_selection_design_source.py`；modify `tools/part_selection_pcb.py`、`tools/part_selection_pcb_session.py`、`tools/part_selection_fields.py` 的复用接口；test `tests/test_design_source.py`、`tests/test_pcb_field_updates.py`、existing `tests/test_pcb_native_types.py`。

**Interfaces:**
- `DesignSource(app).capture(refs: list[str]) -> dict` 包含 `fields`、`records`、`identity`、`mapping`、`versions`；versions 为根页/层级页/库表/记录的内容哈希及相关文件集合。
- `DesignSource.validate(snapshot: dict) -> bool` 重读相关字节及文件集合，不重新解析全部页；有变化返回 false。
- `metadata_replacement(original: dict, *, footprint: str | None = None, fields: dict | None = None) -> str` 返回现有 base64 proto，除了明确标识/字段变更，完整原始对象保持不变。
- `PcbUpdates.preview(refs, bom, *, alignment='rigid', source=None)` 兼容旧调用；新增 item.kind、field_changes、needs_ack。只有 replace 调库几何读取和求解，rename/fields 不调库进程或渲染。

- [ ] 写失败测试断言 capture 每个层级文件仅解析一次；源字节改变但 mtime 相同也失效；孤立/旧 UUID 不通过。
- [ ] 写 metadata 正反测试：只改完整标识时完整 proto 除该字段外相等；更新 Value/别名/Comment/description，已有字段坐标显隐不变，新增属性隐藏且 UUID 唯一；重复大小写和目标冲突拒绝，缺失不清空而显式清空可执行。
- [ ] 跑 `python -B -m pytest tests/test_design_source.py tests/test_pcb_field_updates.py tests/test_pcb_native_types.py -p no:cacheprovider -q` 观察业务失败，然后实现以上接口与分类型预检。
- [ ] 重跑以上及 `tests/test_pcb_update.py tests/test_pcb_schematic_identity.py`，断言 rename/fields 的 library_reader 调用数为 0，批内相同 target 只加载一次，无变化不发布原生提交。
- [ ] 提交：`feat: preview typed PCB field and footprint updates from one source snapshot`。

## Task 4: Connect or Open the Correct PCB

**Files:** Create `tools/part_selection_pcb_connection.py`；modify `tools/part_selection_pcb_session.py`、`tools/part_selection_server.py` 配置注入；test `tests/test_pcb_connection.py`、existing `tests/test_pcb_session.py`。

**Interfaces:**
- `PcbConnection(root, *, discover=None, inspect_processes=None, launch=None, clock=None)` 的依赖可注入以测试，默认实现使用原生 IPC 及本机进程信息。
- `inspect() -> dict` 返回 state：`connected` / `closed` / `busy` / `ipc_disabled` / `ambiguous` / `unknown` / `disconnected`，附 verified project/file/session 或可操作原因，不把异常全部吞掉。
- `ensure(*, open_if_closed: bool, progress=None) -> dict` 只在 closed 且目标唯一时启动一次；等待最大 30 秒，消息反馈真实阶段，超时保留已打开窗口。unknown/busy/ambiguous 不启动。
- `PcbSession` 注入 connection，已核验连接可复用，但每次 snapshot/apply 重验身份。目标文件默认由当前明确工程唯一主文件确定，歧义交回 UI 选择，选择在写入前校验仍属于该工程。

- [ ] 写失败测试覆盖已开复用、未开只启动一次、忙碌不重复开、IPC disabled、不确定进程命令行、重用 PID 的启动时间变化、多工程、令牌隔离、同工程多会话与打开超时。
- [ ] 运行 `python -B -m pytest tests/test_pcb_connection.py tests/test_pcb_session.py -p no:cacheprovider -q`，观察新增行为失败。
- [ ] 实现保留类型化探测错误的发现与打开；PCB 正常可见但无命令行窗口，未确认缺席时保守提示；不更改运行中的 KiCad 全局配置。
- [ ] 重跑测试，确认来自原理图入口且无 inherited PCB socket 仍能发现正确 PCB；缓存失效后重新发现，不向错误板发写命令。
- [ ] 提交：`feat: distinguish PCB connection states and safely open missing editor`。

## Task 5: Staged Design Sync Coordinator

**Files:** Create `tools/part_selection_design_sync.py`；modify `tools/part_selection_server.py` 的任务派发/端点/原理图阶段事件，`tools/part_selection_library.py` 的 `SelectionJobs` 通用任务文案，`tools/part_selection_lifecycle.py` 的忙碌检查；test `tests/test_design_sync.py`。

**Interfaces:**
- `DesignSync(app).preview(payload: dict) -> dict` 只读预检，接受显式 refs、destination=`schematic|pcb|both`、alignment；返回 preview_id、refs、分组变更、前置条件和源版本，不隐式写原理图。
- `start(preview_id: str, acknowledgments: dict) -> dict` 返回稳定 job_id；同令牌重复调用仅返回同一任务。
- `status(job_id: str) -> dict` 返回 stage、message、phase_results、needs_confirmation、done、result；`continue_pcb(job_id: str, preview_id: str, acknowledgments: dict) -> dict` 仅继续未完成 PCB。
- 端点 POST `/api/design_sync_preview`、`/api/start_design_sync`、`/api/continue_design_sync`，GET `/api/design_sync_job?id=...`；所有请求保留现有所有权/工程校验。
- 原理图事件使用现有 `bulk_sync_bom_state_to_schematic` / `sync_schematic` 的阶段钩子，不复制 CAD 写入代码。协调阶段不持有全局写锁等网络/用户确认；实际写入沿用现有锁顺序。

- [ ] 写失败测试：both 严格先 schematic 再 PCB，原理图失败不写 PCB，成功后断连保留 phase_results，继续不重做原理图；PCB-only 有前置差异返回可改 both；同任务重复点击不重放。
- [ ] 写发布结果不明的测试：状态查询/只读对账而非重新 start；预览过期、编辑并发变化、同名库内容变化阻止旧令牌；新几何确认必须绑定同步后快照。
- [ ] 跑 `python -B -m pytest tests/test_design_sync.py -p no:cacheprovider -q` 观察失败，然后实现协调器，复用 Tasks 1/3/4 接口和现有 PCB apply。
- [ ] 重跑 Task 5 及 schematic/PCB 事务测试；验证全成功、部分完成、无需更新三种结果，正在执行的任务阻止宿主退出，等待用户确认不占写锁。
- [ ] 提交：`feat: coordinate scoped schematic and PCB sync with resumable stages`。

## Task 6: B Menu and Typed Diff UI

**Files:** Create `tools/web/design-sync.js/.css`；modify `tools/part_selection_server.py` 工具栏/行模板/脚本挂载，`tools/web/pcb-updates.js` 为旧 API 兼容入口，`tools/web/footprint-compare.js` 保存后导航，`tools/web/desktop-lifecycle.js`；test `tests/test_design_sync_ui.py`、`tests/run_design_sync_smoke.py`。

**Interfaces:**
- `window.openDesignSync({refs, destination, bom})` 先呈现独立工具窗壳，再请求 Task 5 端点；行内 refs 固定当前行，顶部由独立 scope 控件生成明确列表。
- 工具页只消费 Task 5 status 字段及 Task 2 消息；旧 `previewSelectedPcbUpdate()` 转调新入口，旧后端 PCB API 保持兼容。
- 变更显示 `封装仅改名` / `更换封装` / `字段更新` / `类型待核对`；按钮统一“同步原理图”“同步 PCB”“全部同步”，确认页把 both 展开为原理图 + PCB。

- [ ] 写离线 Chromium 失败测试：B 菜单与范围独立；行内不扩大范围；空选择阻止；待核对只阻止含该封装的 PCB 更新；改名不加载图，真换几何默认 center、切模式清空风险勾选。
- [ ] 写竞态和结果断言：打开立即可见、晚响应不能复活已关窗、部分完成仅继续 PCB、轮询断连不显示已失败/再提交、真实状态替代 `startSyncProgressTicker`，局部更新保持列宽/焦点/滚动。
- [ ] 跑 `python -B -m pytest tests/test_design_sync_ui.py -p no:cacheprovider -q` 观察失败，随后实现 B 界面及桥接；不把样例预览脚本带入产品。
- [ ] 跑上述测试和 `python -B tests/run_design_sync_smoke.py`，检查 1440/768/390 截图及无重叠、无页面横向溢出；既有候选、库存、行编辑专项继续通过。
- [ ] 提交：`feat: add compact sync menu and grouped change review`。

## Task 7: Move Remaining Utilities to Native Owned Windows

**Files:** Modify `tools/web/footprint-compare.js`、`path-settings.js`、`local-inventory.js`、`value-parameters.js`、`plugin-updates.js`、`tools/part_selection_server.py` 的列表视图/映射/撤回页挂载、`tools/part_selection_tool_windows.py` 工具白名单；test `tests/test_tool_window_routes.py`、`tests/run_tool_windows_smoke.py`。

**Interfaces:**
- Task 2 registry 的固定 kind：`candidate`、`footprint`、`design-sync`、`settings`、`inventory`、`value-parameters`、`undo`、`list-view`、`field-mapping`、`plugin-update`。工具确认/冲突放在当前工具页中，不衍生任意弹窗。
- 每工具提供 mount/unmount/prepareExit/cancelExit；payload 只允许现有相关请求字段；有未保存输入返回确认需求，任务进行时拒绝关闭；changed 消息仅携带合法 refs/结果版本。
- `plugin-update` 子页只能提议授权操作，由主宿主验证并执行已有更新流程；不能直接获得退出/安装权限。

- [ ] 写失败测试覆盖所有 kind 的白名单和最小 payload，source/install 路由一致，不允许 child 任意创建窗口/调用其他工具接口；settings/inventory 未保存输入阻止错误退出。
- [ ] 运行 `python -B -m pytest tests/test_tool_window_routes.py tests/test_tool_windows.py -p no:cacheprovider -q`，确认失败。
- [ ] 将各工具包装为独立挂载页，复用现有业务函数；多页操作通过同一工程后端和保存队列，通知主表局部刷新。不重写库存、转换、撤回或在线更新事务。
- [ ] 运行新测试和 `python -B tests/run_tool_windows_smoke.py`，实际 WebView2 检查拖出主窗、最大化/缩小/恢复、前进后退保留输入、全部所属工具的退出/重启协调。
- [ ] 提交：`feat: detach workbench utilities into owned native windows`。

## Task 8: Native End-to-End, Documentation and Installation

**Files:** Extend `tests/run_native_pcb_in_place_probe.py`；create `tests/run_native_design_sync_probe.py`；update `tests/README.md`、`README.md`、`tools/docs/part-selection.md`、`AGENTS.md`、`tools/kicad_plugin/package_plugin.py`。

**Interfaces:**
- Native probe 只用 `temporary_project()` 创建合成的多层级工程，含已走线的正背面器件、已有自定义字段和未保存编辑。
- 外部报告记录每阶段真实耗时、核验结果、源/安装版本；报告不包含生产 CAD。功能成功与清理成功分开报告，进程/目录清理失败不能写成整轮通过。

- [ ] 先扩展夹具断言：字段-only 不调库；rename 只变库标识；replace 保留标准库、按 center/rigid 规则核验逐焊盘偏移；未选器件、网络、走线、用户其他未保存编辑不变，一个原生撤销步骤；显式保存重开结果一致。
- [ ] 运行 `python -B tests/run_native_design_sync_probe.py`，实际覆盖从原理图入口发现已开 PCB、确实未开时自动打开、模态忙碌不重复开、both 原理图成功后 PCB 失败再继续，以及所有工具窗真实拖出主窗。不得以模拟测试替代原生流程；缺失能力明确报阻塞。
- [ ] 冷/热路径报告点击到窗口壳可见及各后端阶段耗时。断言没有假计时进度，rename/field-only 不启动几何读取；量化改善采用同一夹具实测，不承诺生产工程固定耗时。
- [ ] 更新使用文档、配置/数据兼容说明及可复用测试规则，运行 `python -B -m pytest tests -p no:cacheprovider -q`、全部新增浏览器/原生专项及 `git diff --check`；做一次独立整体审查并处理发现。
- [ ] 审查远端新增后提交/推送 origin main；运行 `python -B tools/kicad_plugin/package_plugin.py --install`，逐项对照 `package_files()` 的 plugins 文件字节，转换器 SHA-256 不变；最终确认工作区干净和远端提交一致，明确需正常重启旧工作台加载。

## Execution Handoff

建议在本会话逐项实现，最后做一次独立整体审查。理由：分类、源版本、协调器和工具窗协议彼此依赖，集中实现能减少接口漂移；CAD 安全仍由逐任务 TDD、原生夹具和最终独立审查保证。实施计划待用户审阅并确认执行方式后开始产品代码。
