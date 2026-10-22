# Standard Library PCB In-Place Replacement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 保留标准库封装，通过调整所选 PCB 实例的位置和角度保持原焊盘实际位置；实时更新但不自动保存整板。

**Architecture:** 将实时 KiCad 会话、纯几何求解和更新任务分开。现有选料保存与原理图同步不附带 PCB 写入；新增明确的所选器件 PCB 预检、确认、可撤销执行与回读验证。先做本机真实接口能力验证，未证明可以安全替换定义及保留关联前不开放写入按钮。

**Tech Stack:** Python、KiCad 10 原生接口 / 已安装的 IPC Python 绑定、现有 S-expression 解析与几何模块、原生 WebView2、Node Playwright、pytest。

**Spec:** `docs/superpowers/specs/2026-10-23-pcb-library-in-place-design.md`

## Global Constraints

- 不新增适配封装库，不生成偏移或旋转的库副本，不修改标准库文件。
- 只更新用户勾选且预检通过的器件；不执行整板原理图导入，不新增删除其他器件，不修改原有走线或网络定义。
- 只连接可核验属于当前工程的打开 PCB；不使用默认 IPC 地址或同名位号猜目标板。
- 采用实时 PCB 数据，包括未保存修改；锁定器件、源变化或不支持的对象阻止本批次执行。
- 数值计算容差为 0.001 mm；等效端到端夹具逐焊盘误差不超过 5 nm，不自动交换编号、缩放或额外镜像。
- 明确执行后保留为未保存修改；不调用整板 `save()`，一次更新对应一个 KiCad 撤销步骤。
- 回读失败撤回本批次；无法确认恢复时保留恢复材料并明确报错，不覆盖整板。
- 临时工程、截图、日志、预览与恢复材料在工程外；生产 CAD / BOM 不作为写入测试对象。
- 所有手工源码编辑使用 apply_patch；先测试失败、再实现；完整回归与独立审查后才提交、推送和重新安装。

## Review Focus

1. 多个 KiCad 工程同时打开，甚至都有 L9：必须按真实文件身份和会话选择目标，测试归 Task 2。
2. 预览后用户移动、旋转或更换器件：旧令牌必须失效，不能覆盖用户后续编辑，测试归 Task 4。
3. 编辑器中存在其他未保存修改：更新、失败回滚和撤销均不得保存或丢弃这些修改，测试归 Task 3 / 6。
4. 同一工作台行有正背面、不同角度的多个实例：逐 UUID 计算，不共用一个补偿值，测试归 Task 2 / 3。
5. 网络返回失败发生在写入之后：不得重复执行或错误显示成功，使用任务 ID 和实时回读识别结果，测试归 Task 4。

## Task 0: 隔离并退出暂停的方案 A

**Files:** `tools/part_selection_footprints.py`、`tools/part_selection_server.py`、`tools/web/footprint-compare.js` / `.css` 及当前未提交测试改动。

**Interfaces:** 此任务只建立干净的方案 B 行为基线；不新增 PCB 接口。

- [ ] 核对当前工作区及非破坏式备份。已记录发布基点 `805338b3302acbee3cb0d48a0396fa280a2bcefb`，方案 A 跟踪文件备份 `9c0f588178cf9f09523f1f62c4b3946d1411d0f4`，未跟踪文件另有工程外备份。
- [ ] 对照基点逐块撤去本任务的“发布适配副本”行为，保留用户其他改动和已批准的同窗导航；不使用清空工作区或整树还原。
- [ ] 保留可复用夹具；将 A 中“库副本变换、实例 X/Y 不变”的断言改为 B 的验收目标，不把旧断言当作 B 的需求。
- [ ] 运行 `python -B -m pytest tests -p no:cacheprovider -q`，确认基线无未知失败；记录后再进行能力验证。

## Task 1: 真实接口能力验证

**Files:** 创建 `tests/run_native_pcb_in_place_probe.py`，复用 `tests/_scratch.py` 及 `tests/run_native_footprint_workflow.py` 的隔离工程构造。

**Interfaces:** 脚本输出工程外 `capabilities.json`，包含 KiCad / 绑定版本、实际采用的接口、各验收项及失败原因；不由此创建生产入口。

- [ ] 编写一次性双器件夹具：标准目标库原点为零，旧实例几何带原点偏移和 90 度局部旋转，PCB 分别正面 37 度、背面 90 度，包含网络、走线和原理图关联。
- [ ] 增加另一个未选中器件及一项未保存编辑；保存磁盘基线并记录实时基线。
- [ ] 通过已打开的目标 PCB 原生接口，验证在同一实例 UUID 上替换标准库几何并补偿放置；不先删除再创建、不通过整板 F8 更新替代。
- [ ] 回读验证器件 / 焊盘身份与网络、原理图路径、正反面、文本放置和未选中内容；磁盘 PCB 内容必须不变。
- [ ] 实际执行一次撤销，确认只撤回本插件批次，并保留此前未保存编辑。
- [ ] 在测试工程正常保存并重新打开，验证补偿、标准库绑定与网络仍正确。
- [ ] 若本机接口不能可靠完成任何一项，停止后续写入功能并报告具体限制；不得把仅移动位置的成功当作封装替换能力通过。

## Task 2: 几何求解与实时只读会话

**Files:** 创建 `tools/part_selection_pcb_geometry.py`、`tools/part_selection_pcb_session.py`、`tests/test_pcb_in_place_geometry.py`、`tests/test_pcb_session.py`。

**Interfaces:**
- `solve_placement(source: dict, target: dict, tolerance_mm: float = 0.001) -> dict`：返回 `ok`、`position_mm`、`rotation_deg`、`back`、`max_error_mm`、逐焊盘偏差及尺寸差异；失败返回明确 `reason`，无 I/O。
- `PcbSession.snapshot(project_root: Path, refs: list[str]) -> dict`：返回经身份核验的 `session_id`、`document_path`、`revision`、完整板恢复快照与逐实例数据。
- `PcbSession.read_library(lib_id: str) -> dict`：用 Task 1 已验证的原生读取路径返回目标标准定义、源路径及内容哈希，不放置临时器件到用户板。

- [ ] 先写几何失败测试：未知 / 重复编号、间距不同、方向歧义；必须 `ok=False`。
- [ ] 先写成功测试：局部偏移、90 / 180 / 任意角度、正背面、圆方焊盘、同一行不同实例；对应焊盘世界坐标误差不超过容差。
- [ ] 复用 `part_selection_footprint_geometry.py` 的编号和配准规则；不要另写只适合 1206 的映射。
- [ ] 对会话添加多工程同位号、文件路径不符、锁定、接口不可用、磁盘落后于实时数据的失败测试。
- [ ] 运行新增两个测试文件；确认只读路径不创建库、不改记录、不改 PCB。

## Task 3: 可撤销 PCB 事务与回读

**Files:** 修改 `tools/part_selection_pcb_session.py`，创建 `tests/test_pcb_transaction.py`。

**Interfaces:**
- `PcbSession.apply(snapshot: dict, replacements: list[dict], backup_dir: Path) -> dict`：批次预检、先备份、一个原生提交、回读；返回更新后的逐实例数据和恢复凭据，不调用保存。
- `PcbSession.verify(before: dict, after: dict, replacements: list[dict]) -> dict`：校验目标绑定、补偿、UUID / 关联、网络、正反面、未选中器件和走线；输出具体差异。

- [ ] 写失败测试：备份失败时零写入、一个实例不支持则整批零写入、原生写入中途失败、回读不符、关联或网络丢失。
- [ ] 写未保存编辑保护测试：成功、失败、撤销三条路径都不调用整板保存或载入磁盘基线。
- [ ] 基于 Task 1 验证的接口实现先预检后提交，保留实例和可一对一映射的焊盘身份；执行后校验 API 实际返回，而不相信请求对象。
- [ ] 写回读失败的本批次撤销测试；恢复无法确认时返回失败与恢复目录，不能宣称成功。
- [ ] 运行几何、会话与事务测试；再次执行 Task 1 真实脚本，验证一个撤销步骤和磁盘未保存语义。

## Task 4: 所选器件更新任务、令牌与状态

**Files:** 创建 `tools/part_selection_pcb.py`、`tests/test_pcb_update.py`，修改 `tools/part_selection_server.py`、`tools/part_selection_overrides.py`。

**Interfaces:**
- `PcbUpdates.preview(refs: list[str], bom: str) -> dict`：核对工作台目标与已保存原理图，生成含逐位号新旧参数的预览令牌；不一致则提示先同步，不自动同步。
- `PcbUpdates.apply(preview_id: str, acknowledged: bool) -> dict`：核对实时源、目标库哈希及令牌，提交事务并记录结果；重复请求不重复移动。
- `POST /api/pcb_update_preview`、`POST /api/start_pcb_update`、`GET /api/pcb_update_job?id=...`：复用现有任务队列机制，但与原理图同步状态分开。

- [ ] 写待同步原理图阻止、目标已在 PCB 生效、无选中项、NC / PCB 缺失位号、令牌过期及预览后移动器件的测试。
- [ ] 写已错位但缺少可信旧快照的测试：明确拒绝自动恢复，不把当前错位实例当作历史正确位置；有旧快照时仍校验工程与实例身份。
- [ ] 写网络丢失后重复执行 / 查询的测试；结果未知时保留任务 ID，并回读确认，不直接重做。
- [ ] 保存逐位号“PCB 已更新到编辑器、尚未保存”的结果证据。重新检查以实时数据为准；撤销或重新打开旧磁盘板后不得仍假定已更新。
- [ ] 写确认状态及其他字段不被误改的测试；同步原理图成功不能清除 PCB 待更新。
- [ ] 在每个文件中复用现有字段映射、记录锁、恢复目录和分组规则，避免向大服务器模块加入事务实现细节。
- [ ] 运行新增更新测试及现有记录、字段映射、原理图同步回归。

## Task 5: 工作台交互与封装预览

**Files:** 创建 `tools/web/pcb-updates.js` / `.css`、`tests/run_pcb_updates_smoke.py`、`tests/test_pcb_updates_ui.py`；修改 `tools/web/footprint-compare.js` / `.css`、`tools/web/desktop-lifecycle.js` 和服务器模板。

**Interfaces:** `window.previewSelectedPcbUpdate()` 打开预检清单并明确确认后启动任务；封装管理页只保存标准库绑定和待更新记录。

- [ ] 先写浏览器失败测试：没有勾选不能更新；预览只列所选位号；读取中不误报成功；取消无写入；一次点击只发一个应用请求。
- [ ] 写焊盘位置一致但尺寸、钻孔或层不同的提示测试：逐项展示差异并要求明确确认，不显示为电气或制造兼容结论。
- [ ] 移除 A 的适配副本目标库 / 名称项；预览显示标准库标识、原位补偿后的新位置角度、编号偏差与尺寸差异，保留同窗前进后退和输入状态。
- [ ] 添加独立“更新 PCB（所选器件）”入口；不更改“同步原理图”的写入边界。
- [ ] 成功文案明确“已更新到 PCB 编辑器，尚未保存”；失败列出位号和原因，不清空待更新提示。
- [ ] 加入保存 / 转换 / 原理图同步 / PCB 更新互斥及关闭忙碌保护；普通关闭不自动保存 PCB。
- [ ] 以 1440 / 800 / 390 像素截图核验按钮、预检表、失败详情和无横向溢出。
- [ ] 运行新增 UI 冒烟及现有封装、行编辑、桌面生命周期回归。

## Task 6: 真实端到端、打包与交付

**Files:** 修改 `tests/run_native_footprint_workflow.py`、`tests/test_native_workflow_fixture.py`、`tools/kicad_plugin/package_plugin.py`、`README.md`、`tools/docs/part-selection.md`、`tests/README.md`、`AGENTS.md`。

- [ ] 将 `library` 流程验收改为 B：标准库内容不变、实例 X/Y / 角度允许改变、逐焊盘世界坐标误差不超过 5 nm；原有 `rename` 和 `converted` 验收不被误改。
- [ ] 在真实工作台和原生 KiCad 运行“选标准库 -> 同步原理图 -> 更新所选 PCB”，覆盖正背面实例；检查关联、网络、走线和未选中器件。
- [ ] 核验更新后磁盘内容未被自动保存、原生撤销只撤当前批次；随后在测试工程保存、关闭并重开验证持久结果。
- [ ] 增加所有新模块及 Web 资源到 `package_plugin.CORE_FILES`，运行打包内容检查。
- [ ] 完整执行 `python -B -m pytest tests -p no:cacheprovider -q`，明确报告任何跳过项与环境限制。
- [ ] 独立审查 PCB 写入范围、回滚、未知结果重试、实时身份核验和撤销后状态；修复发现后重跑相应验证。
- [ ] 正常关闭测试宿主及测试 KiCad，清理一次性工程，保留工程外证据；不结束用户生产服务。
- [ ] 验证通过才提交并推送插件 origin，运行 `python -B tools/kicad_plugin/package_plugin.py --install`，逐文件比对安装内容并确认转换器保留。不把本机安装称为公开 Release。

## 测试环境与执行方式

所有 Python 测试设置 `PYTHONUTF8=1`、`PYTHONDONTWRITEBYTECODE=1`、`PYTHONPATH=tools`。Chromium 冒烟设置 `NODE_PATH` 为已安装的 Playwright 所在目录。真实 PCB 能力验证使用一次性工程和独立 KiCad 会话，生产 L9 不作为写入测试对象。

建议在当前会话由主执行者顺序实现，最后进行独立代码审查：接口能力验证是关键前置，几何、事务和状态彼此依赖，不宜多路同时猜接口。计划审核后再启动实现。

## 执行记录（2026-10-23）

- Task 0：完成。基点 `805338b`；非破坏式备份 `14c1c78222abe690e8fd66a383b5f733f6009451`，暂停方案 A 仅撤去本任务改动。重新运行的基线为 998 项通过。
- Task 1–3：完成。纯几何求解、显式 IPC 会话、原理图 UUID 路径核验、原生事务及单步撤销均已实现。目标库由独立隐藏只读会话加载，不向用户 PCB 临时放置器件。
- Task 4：完成实时预检、版本令牌、任务幂等及回执丢失核验。历史错位恢复不在本次范围：只以当前焊盘位置为基准，界面明确提示。持久主表 PCB 状态徽标暂缓，以每次实时预检和任务结果为准。
- Task 5：完成。PCB 预检、同窗原位封装对比、差异确认、忙碌保护，以及 1440 / 800 / 390 截图验证。选库页仍为库定义比较，原位对比放在 PCB 更新预检内。
- Task 6：真实工作台“选标准库 → 同步原理图 → 更新 PCB → 测试工程保存重开”通过。覆盖三个实例（含正背面、不同角度、同一目标库复用）、未保存编辑、一个原生撤销步骤、库不变、编号焊盘世界坐标误差 <=5 nm、网络/关联/走线保留。证据在工程外 `Temp/kicad-pcb-inplace-probe-20261023`；测试工程已自动清理。
- 独立审查完成，发现的 8 项安全问题均补充回归并修复；最终完整回归 1,046 项通过，无跳过，30 条已有转义警告。提交与安装结果另以 Git 记录及交付校验为准。上述原始清单保留为设计任务描述，本执行记录说明实际范围及偏离。

### 裁定与暂缓项

- KiCad 10 缺少 IPC 读取库接口，采用隔离只读编辑器；代价是首次预检可能较慢或失败，不影响用户板。
- 先校验暂存返回、发布后再回读实时板；真实接口在发布前返回旧板，不能误判为写入失败。
- 文件保存重开的文本容差为 5 nm，以允许原生 1 nm 量化；逐焊盘世界坐标仍要求 <=5 nm。
- 发布后无法核验时不盲目撤销：KiCad 10 无原子撤销栈归属查询，自动撤销可能误伤后续编辑；代价是极端断连时需要人工核验恢复材料。
- 暂缓：明确的启动拒绝仍复用断连任务查询，错误文案可能较笼统；PCB 待更新记录暂不提供主表实时状态徽标。
