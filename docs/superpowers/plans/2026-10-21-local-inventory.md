# 本地库存与多套用量实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 导入已购库存并按可配置套数显示器件需求、库存与差额。

**Architecture:** 新增独立 `part_selection_inventory.py`，负责 XLSX/XLSM 导入解析、C 编码去重/冲突标记、库存 JSON 原子持久化和行级计算。`PartSelectionApp` 暴露导入预览/确认及套数 API；前端在数据菜单提供导入对话框，在列表显示独立“本地库存”列，套数保存在 UI 设置。

**Tech Stack:** Python 3.10+, openpyxl, 标准库 JSON/HTTP server, HTML/CSS/JavaScript, pytest。

**Spec:** `docs/superpowers/specs/2026-10-21-local-inventory-design.md`

## Global Constraints

- 来源 Excel 只读；生产工程及其原理图、BOM 和选料数据不作为测试写入目标。
- 库存独立保存于 `BOM/选料工作台/本地库存.json`；不更改商城库存缓存或选料记录 schema。
- 有效库存取“同型号已购合计”；绝不将订单行数量/发货数量重复累计。
- 只有有效主料 C 编码精确匹配库存；型号仅用于辅助提示。
- 单套需求使用行 `quantity`，总需求等于 `quantity × 套数`；套数默认 3 并持久化。
- 修改前遵循 `AGENTS.md` 做非破坏式基点/stash；测试后安装插件、校验安装副本、提交并推送本轮改动到插件 `origin`。
- 测试命令：`$env:PYTHONDONTWRITEBYTECODE='1'; python -B -m pytest tests -p no:cacheprovider`。

## Review Focus

1. 同编码重复行且“同型号已购合计”相同/冲突：只保留一个相同数量；冲突禁止入账并说明来源行。
2. 空编码、非法数量、公式缓存缺失、不同工作表和列名别名：有可理解的预览错误，不能写入有效库存。
3. 已导入编码在当前选择变化、主料空缺或同一行编码冲突时：不误匹配，显示待确认/未匹配。
4. 套数为 0、负数、小数、空值及超大值：前端和服务端共同校验，不破坏此前设置。
5. 本地库存列隐藏、重排、刷新/重新打开后：用户列布局与套数保持；商城库存显示/筛选不受影响。

---

### Task 1: 库存记录与工作簿解析

**Files:**
- Create: `tools/part_selection_inventory.py`
- Create: `tests/test_part_selection_inventory.py`
- Modify: `tests/README.md`

**Interfaces:**
- Produces `parse_inventory_workbook(path: Path) -> dict[str, object]`，返回工作表信息和按编码整理的预览条目，不写文件。
- Produces `merge_inventory_records(existing: dict, preview: dict) -> dict`，保留来源文件未出现的编码；重复输入相同汇总值只保留一个；汇总冲突标为 unresolved。
- Produces `load_inventory(path: Path) -> dict` 和 `save_inventory(path: Path, inventory: dict) -> None`；写入使用同目录临时文件、fsync 和 `os.replace`。
- Produces `calculate_inventory_status(record, per_set_quantity: int, set_count: int) -> dict`。

- [ ] **Step 1: 添加失败测试**：合成 xlsx 覆盖中文表头别名、多个工作表、同编码重复一致/冲突、空编码/非法数量/公式空缓存、保留既有编码及原子写入可读性；需求 2 件/套、库存 5、3 套得需求 6、缺口 1。
- [ ] **Step 2: 运行专项测试确认失败**：`python -B -m pytest tests/test_part_selection_inventory.py -q`。
- [ ] **Step 3: 实现解析/合并/持久化/计算**：仅读取商品编号、厂家型号、品牌、封装、同型号已购合计、订单编号、来源行；不载入收货人或地址字段。
- [ ] **Step 4: 运行专项测试通过**：`python -B -m pytest tests/test_part_selection_inventory.py -q`。

### Task 2: 工程数据路径与服务接口

**Files:**
- Modify: `tools/part_selection_overrides.py`
- Modify: `tools/part_selection_server.py`
- Test: `tests/test_part_selection_inventory.py`
- Test: `tests/test_path_settings.py`

**Interfaces:**
- 配置加载新增 `local_inventory` 路径，默认为工程 BOM 数据目录中的 `本地库存.json`，沿用自定义 `bom_dir` 语义。
- `PartSelectionApp.preview_local_inventory(payload) -> dict` 接收工作簿路径及可选工作表名，返回预览，不写库存。
- `PartSelectionApp.import_local_inventory(payload) -> dict` 只接受确认后的预览/来源令牌并保存，返回新增、更新、重复、待核实计数。
- `PartSelectionApp.set_inventory_set_count(payload) -> dict` 服务端校验正整数并持久化在 UI 配置中。
- `PartSelectionApp.rows()` 在每行新增 `local_inventory` 视图数据，不改变 `stock` 商城字段。

- [ ] **Step 1: 写失败测试**：默认与自定义 BOM 路径、预览无副作用、未确认不可写、确认导入/重复导入不累加、API 错误响应、主料 C 编码匹配，以及行编码冲突待确认。
- [ ] **Step 2: 运行专项测试确认失败**：`python -B -m pytest tests/test_part_selection_inventory.py tests/test_path_settings.py -q`。
- [ ] **Step 3: 实现配置路径与 API**：新增 POST `/api/local_inventory/preview`、`/api/local_inventory/import`、`/api/local_inventory/set_count`；请求路径必须为用户明确选择的文件，不读取工作目录猜测路径。
- [ ] **Step 4: 运行专项测试通过**。

### Task 3: 菜单导入预览与工作台展示

**Files:**
- Modify: `tools/part_selection_server.py`
- Modify: `tools/web/path-settings.js` only if shared directory picker primitives are reused
- Modify: `tests/test_workbench_toolbar.py`
- Modify: `tests/test_part_selection_overrides.py`
- Create: `tests/run_local_inventory_smoke.py` if browser smoke conventions support a reusable run

**Interfaces:**
- 前端预览调用 `POST /api/local_inventory/preview`，只有用户按“确认导入”后调用 `POST /api/local_inventory/import`。
- 套数输入调用 `POST /api/local_inventory/set_count`；当前 UI 状态保存 `localInventorySetCount`，初始值 3。
- 表格新增 `data-col="local_inventory"` 并纳入现有列可见性、顺序、宽度及持久化机制。
- 本地库存状态在格内显示现存/需求/差额与“充足/不足/待核实/未匹配”；必要时展示同型号但编码不同的提示，不自动套用。

- [ ] **Step 1: 添加失败测试**：工具栏数据菜单含导入入口；预览/确认操作文本和 endpoint 存在；本地库存列可配置；商城库存旧列及查询仍存在；渲染行显示计算结果。
- [ ] **Step 2: 执行失败测试**：`python -B -m pytest tests/test_workbench_toolbar.py tests/test_part_selection_overrides.py -q`。
- [ ] **Step 3: 实现紧凑导入对话框、工作表选择、预览摘要/逐项错误及确认写入；实现套数输入和库存列。**
- [ ] **Step 4: 执行测试并修复**。

### Task 4: 文档、全套验证和发布

**Files:**
- Modify: `tools/docs/part-selection.md`
- Modify: `tools/docs/kicad-plugin.md`
- Modify: `tools/docs/ai-handoff.md`
- Modify: `tests/README.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: 更新教程**：说明 Excel 表头要求、套数计算、匹配优先级、冲突处理、文件路径与隐私边界。
- [ ] **Step 2: 跑完整套测试**：按 Global Constraints 执行 pytest，专项浏览器 smoke 使用一次性合成工程和合成 workbook。
- [ ] **Step 3: 检查变更**：确认真实采购 Excel、个人信息、目标工程数据均未进入 diff；核对编码冲突和导入重复流程。
- [ ] **Step 4: 安装验证**：运行 `python -B tools/kicad_plugin/package_plugin.py --install`，验证源码与安装副本一致并保留转换器。
- [ ] **Step 5: 提交推送**：提交审查后的插件源码/测试/文档，推送 `origin main`；不得将本地生产库存文件提交。

## 自检

## 实施记录

- Task 1–3 已完成。套数沿用既有项目 UI localStorage（`localInventorySetCount`），未另设重复的服务端套数 API；Python 计算层和前端均限制正整数，前端变化即时重算。原计划列出的 `set_count` endpoint 由此替代。
- 真实来源只读检查发现非 C 采购编号，补充待匹配库存持久化与预览显式映射 C 编码；重复型号汇总不叠加。不自动根据型号、历史位号或供应商编号替换工程主料。
- 同编码跨分组按共享库存池核对，NC 不计需求；所有已加载行参与计算，筛选/隐藏不会降低需求。
- 预览令牌十分钟有效且有界，导入确认校验源文件哈希和库存基线。数据目录迁移对隐式库存路径同样执行复制确认、冲突检查和备份。
- 测试先行观察过缺模块/缺接口/缺列失败；代码审查发现的数量表头优先级和隐式路径迁移问题均先加入失败测试再修复。
- D 编号复核问题按 RED→GREEN 修复：持久化来源分组键到唯一 C 编码绑定，重复导入沿用、改绑拒绝；无效 D 数量行保留到型号组，整组待核实，避免跳过后错误入账。
- 验证：完整 pytest 548 项通过；新增库存真实 Chromium 冒烟、既有路径设置及行编辑冒烟通过，无页面脚本错误。桌面/窄屏截图已检查；测试只用自动清理的合成工程。28 条既有模板转义警告仍存在。未执行真实 KiCad 同步或生产工程导入。

- 规格覆盖：数据身份/解析与合并在 Task 1；路径与服务/API 在 Task 2；列表与交互在 Task 3；用户文档/测试/分发在 Task 4。
- 风险聚焦的 5 类输入均由 Task 1–3 测试覆盖。
- 跨任务接口名称和参数保持一致；UI 设置键 `localInventorySetCount` 只表示套数，不存实际库存。
- 只创建一个专用库存模块，不扩展现有商城库存结构。
