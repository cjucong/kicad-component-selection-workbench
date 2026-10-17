# 器件选型助手使用教程

[工具首页](../README.md) | [使用教程](part-selection.md) | [AI 接手](ai-handoff.md) | [故障排查](troubleshooting.md)

独立仓库首先按 [仓库首页](../../README.md) 使用 `desktop.py --project` 指定自己的工程。下文 `.bat` 与省略 `--config` 的示例仅适用于把工具放在目标工程 `tools/` 下的兼容用法；独立仓库运行服务时必须传入目标工程的 `--config`。

## 运行环境要求

- **操作系统**：Windows 10/11；一键启动器是 Windows 批处理脚本，并使用系统自带的 `cmd.exe` 和 Windows PowerShell。
- **Python**：Python 3.10 或更高版本，解释器必须能正常执行并带有 `pip`。支持 python.org、Conda/Miniconda、`PATH` 中的解释器，也可通过 `PART_SELECTION_PYTHON` 指定完整路径；Windows Store 的无效执行别名不会被采用。
- **Python 依赖**：`tools/requirements.txt` 当前要求 `openpyxl>=3.1`。缺少依赖时启动器会尝试通过 `pip` 安装，因此首次启动可能需要网络以及对当前 Python 环境的安装权限。
- **浏览器**：查看本地工作台需要现代浏览器；库存查询的网页回退功能需要 Microsoft Edge 或 Google Chrome。没有 Chromium 浏览器时，基础页面和不依赖网页回退的功能仍可使用。
- **端口**：本机 TCP 端口 `8765` 必须空闲，或已经运行本工具的健康实例。服务仅绑定 `127.0.0.1`，默认不会向局域网开放。
- **文件权限**：运行账户需要读取整个工程，并对 `BOM/` 中的选型状态、缓存和同步日志具有写权限；启动日志写入 `%TEMP%\part_selection_tool_startup.log`。
- **KiCad**：基础网页浏览和 BOM 复核不要求启动 KiCad；位号跳转、原理图字段同步及相关 QA 流程按本工程的 KiCad 10 环境设计。

开始前可用下面的命令确认启动器找到了哪个 Python；命令只检查环境，不启动服务：

```bat
call tools\start_part_selection_tool.bat --find-python
```

## 快速启动

最简单的方式是双击 `tools/start_part_selection_tool.bat`。它会自动检查 Python 依赖，然后在隐藏的后台进程中启动网页服务；启动脚本本身会立即退出，关闭命令行窗口不会再终止服务。

服务启动后访问：

```text
http://127.0.0.1:8765/
```

隐藏启动不会弹出服务命令行。排查启动失败时查看：

```text
%TEMP%\part_selection_tool_startup.log
```

启动器会验证 Python 3.10 或更高版本，并依次检查 `PART_SELECTION_PYTHON`、常见 Python 安装目录、`PATH` 中的真实解释器以及常见磁盘根目录下的 Conda 安装。它不会把 Windows Store 的 `python.exe` 执行别名当成可用解释器。需要手动指定时，可在终端运行：

```bat
set "PART_SELECTION_PYTHON=F:\anaconda\python.exe"
call tools\start_part_selection_tool.bat
```

启动器会先检查 `8765` 上是否已有健康实例；重复双击时直接退出，不会叠加多个后台 `pythonw` 服务。

如需在终端中观察实时输出，仍可使用下面的手动启动命令。

在工程根目录运行：

```powershell
cd C:\KiCadProjects\MyBoard
python -m pip install -r tools\requirements.txt
python tools\part_selection_server.py --host 127.0.0.1 --port 8765 --open
```

库存查询的网页回退需要 Chromium 浏览器。工具会按顺序查找 Chrome 或 Edge；如果设备没有浏览器，可安装任一浏览器：

```powershell
winget install --id Microsoft.Edge --exact
# 或
winget install --id Google.Chrome --exact
```

网页地址：

```text
http://127.0.0.1:8765/
```

如果已有服务占用 `8765`，先找进程：

```powershell
Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue |
  Select-Object LocalAddress,LocalPort,OwningProcess
```

## 配置文件

默认配置在：

```text
BOM/part_selection_config.json
```

常用字段：

| 字段 | 说明 |
| --- | --- |
| `project_name` | 网页标题 |
| `project_root` | 工程根目录，通常是 `..` |
| `schematic_dir` | KiCad 原理图分页面目录，当前为 `DOC` |
| `source_bom` | 工程源 BOM |
| `lcsc_bom_pattern` | LCSC BOM 下拉列表匹配规则 |
| `input_refs` | 手工位号输入文件 |
| `output_overrides` | 人工选择、字段编辑、同步摘要等状态文件 |
| `stock_cache` | 立创库存/价格/参数缓存 |
| `kicad_protocol` | 浏览器跳 KiCad 的自定义协议名 |

当前主要数据文件：

```text
BOM/MyBoard_LCSC_BOM.csv
BOM/part_selection_overrides.json
BOM/lcsc_stock_cache.json
BOM/part_selection_schematic_sync_log.csv
```

“网页端状态”不是浏览器 DOM 临时内容，而是后端合并结果：

```text
LCSC BOM CSV + part_selection_overrides.json + lcsc_stock_cache.json
```

## Web UI 常用流程

1. 打开 `http://127.0.0.1:8765/`。
2. 在左上角选择 LCSC BOM，例如 `MyBoard_LCSC_BOM.csv`。
3. 切换 BOM 后表格会自动加载；顶部状态栏会显示分组数和位号数。
4. 点击单行“查询库存”，或点击“查询未查库存”只查还没有缓存的器件。批量查询时，工具栏会显示独立进度条，包含完成数量、百分比、当前 C 编码、表格行号和位号；同一 C 编码只查询一次。
5. 如需换 C 编码，在替换框输入 `Cxxxx` 后按 `Enter`，会自动识别、保存、合并，不需要额外点保存按钮。
6. 点击“更换型号”或备选料列的“添加备选”，可把候选型号设为主料或加入备选料。
7. 编辑 `Comment`、`Value`、`Footprint` 后按 `Enter` 自动保存；`Shift+Enter` 在文本框内换行。
8. 单行“同步原理图”会把主料、备选料和已选择的订货料字段一起写回 KiCad。
9. 表头的全局同步按钮/CLI 批量同步会把当前后端合并状态写回原理图。
10. 更换型号中输入精确 `Cxxxx` 时，只展示该编码的精确结果，不自动混入参数替代料；国内页面、WMSC 回退和缓存的具体顺序由 `part_stock_lookup.py` 维护。首次查询耗时受网络、浏览器状态和站点响应影响。
11. 合并行的位号列出现“多选拆行”时，点击后勾选多个位号并点“拆出所选”；未选位号留在原行，选中位号各自成为临时独立行。临时行只有在字段修改并按 Enter 保存后才会写入覆盖记录。
12. 更换型号默认用 Chrome `--app` 快速打开立创官网，同时在弹窗中自动执行结构化查询。结构化结果优先读取与 Chrome 相同的嘉立创商城页面数据；商城页面不可用时才回退 WMSC 接口。也可修改搜索词后手动重新查询。
13. 结构化候选料会在主料候选下方单独显示“订货渠道”。精确 C 编码搜索会从立创搜索页的海外渠道卡片读取 `Digikey`、`Mouser`、`TTi-US`、`E` 等供应商，并显示供应商型号/编号、库存、交期、起订量、增量、最小包装、价格梯度和 `item.szlcsc.com/details/...` 实际下单链接。
14. 自营候选使用“选用主料”或“加入备选”；订货渠道使用“选用订货料”。选择订货料只记录报价链接和供应商信息，不会改动当前主料 C 编码。

网页表格支持：

- 列显示/隐藏。
- 主表列顺序拖拽调整。
- 列宽拖拽调整。
- 候选型号弹窗列顺序和列宽调整。
- 每行最多 5 个有优先级的备选料，可上移、下移、删除。
- 库存状态按颜色区分：现货、低库存、待查询、异常。

列宽和列顺序保存在浏览器 `localStorage`，不是后端状态。

## 库存与物料参数缓存

库存查询入口：

```text
GET /api/stock?code=C5182461
```

查询结果写入：

```text
BOM/lcsc_stock_cache.json
```

查询会使用当前行的有效或手动输入 C 编码，并将结果写入库存缓存。批量查询不会改写 `part_selection_overrides.json`，避免库存刷新与人工选型保存互相干扰。

“查询未查库存”和“查询全部库存”的区别：

- 查询未查库存：跳过已经有缓存/已查询状态的主料和备选料 C 编码。
- 查询全部库存：重新查所有当前主料和备选料 C 编码，适合强制刷新价格和库存。

所有 C 编码会先全局去重，同一个编码只查询一次。

## C 编码选择优先级

最终生效 C 编码由 `part_selection_overrides.choose_effective_c_code()` 决定。大致优先级：

1. 人工替换并接受的 C 编码。
2. 工程/已接受 C 编码。
3. 立创匹配 C 编码。
4. 如果被标记为拒绝，则最终 C 编码为空。

`LCSC_URL` 只对真正的 `C数字` 生成，例如：

```text
https://so.szlcsc.com/global.html?k=C2179278
```

`NC`、空值、非 C 编码不会生成有效立创链接。

## 备选料数据模型

备选料保存在每个位号的 override 中，数组顺序就是优先级：

```json
{
  "alternate_parts": [
    {
      "c_code": "C22399484",
      "model": "HVLR2408F500KK9",
      "package": "Through Hole,D8xL24mm",
      "param": "RES 500kΩ ±1% 2W"
    }
  ]
}
```

约束：

- 每行最多 5 个备选料。
- 不允许重复编码，也不允许把当前主料加入备选料。
- 主料后来改成某个备选编码时，后端会自动从备选列表移除该编码。
- 型号、封装和参数是稳定信息；库存继续只保存在 `lcsc_stock_cache.json`。

备选料接口：

```text
POST /api/alternate_parts
```

`action` 支持 `add`、`remove`、`move`、`adopt_schematic`。所有操作以整行位号为目标，同一分组中的位号会保存相同的有序备选列表。

## 同步到 KiCad 原理图

支持字段：

```text
Comment
Value
Footprint
LCSC
LCSC_URL
Description
LCSC_ALT
LCSC_ALT_INFO
PROCUREMENT_URL
PROCUREMENT_INFO
```

代码里的字段映射在 `part_selection_helper.py`：

```python
SCHEMATIC_FIELD_PROPERTIES = {
    "comment": "Comment",
    "value": "Value",
    "footprint": "Footprint",
    "lcsc_c_code": "LCSC",
    "lcsc_url": "LCSC_URL",
    "description": "Description",
    "lcsc_alt": "LCSC_ALT",
    "lcsc_alt_info": "LCSC_ALT_INFO",
    "procurement_url": "PROCUREMENT_URL",
    "procurement_info": "PROCUREMENT_INFO",
}
```

备选料在 KiCad 中使用两个属性：

- `LCSC_ALT`：权威的有序 C 编码列表，例如 `C1001,C1002`。
- `LCSC_ALT_INFO`：一行一个备选料的型号、封装和参数摘要，不包含库存。

网页没有备选料 override 时，会从原理图初始化备选列表。两端不一致时网页显示冲突，必须明确选择“采用原理图”或“同步到原理图”，不会静默覆盖。

## 订货料数据模型

合作库存/海外订货渠道不是立创自营物料，不使用 C 编码作为身份。网页在“更换型号”的“订货渠道”区域点击“选用订货料”后，以精确下单 URL 保存一条独立记录：

```json
{
  "procurement_choice": {
    "id": "order_<URL 哈希>",
    "source": "lcsc_overseas",
    "supplier": "Mouser",
    "supplier_sku": "72-P2TC0805D5002LNTA",
    "manufacturer_part_number": "P2TC0805D5002LNTA",
    "package": "0805",
    "batch": "26+",
    "order_url": "https://item.szlcsc.com/details/...",
    "related_lcsc_code": "C4283613"
  }
}
```

- `order_url` 是订货料的唯一业务身份；`id` 由 URL 稳定生成。
- `related_lcsc_code` 只用于界面提示关联商品，不会替换主料 C 编码。
- 库存、价格和交期属于动态信息，只显示在候选报价中，不写入 override 或原理图。
- 网页可清除订货料；两端冲突时可“采用原理图”或“同步到原理图”。
- KiCad 使用 `PROCUREMENT_URL` 保存下单链接，`PROCUREMENT_INFO` 保存渠道、供应商型号、制造商型号、封装、批次和关联 C 编码摘要。

### 订货料操作教程

1. 在目标 BOM 行点击“更换型号”或“添加备选/订货料”。
2. 等待结构化查询完成，在自营候选下方找到“订货渠道”；精确 C 编码会显示官网对应的海外供应商分组。
3. 对比供应商、库存、交期、起订量和价格；“立创订货”用于打开该条报价的实际下单页。
4. 点击目标报价的“选用订货料”。网页会把选择保存到 `BOM/part_selection_overrides.json`，刷新或切换 BOM 后仍保留。
5. 回到 BOM 表确认“订货料”区块中的型号、封装、渠道和订货链接。此时主料 C 编码保持不变，该行显示待同步状态。
6. 点击本行“同步原理图”，或者使用顶部“同步全部原理图”。同步成功后，KiCad 器件属性中会出现 `PROCUREMENT_URL` 和 `PROCUREMENT_INFO`。
7. 不再使用该订货料时点击“清除”，随后再次同步原理图，两个属性会被清空。

网页和原理图同时存在但内容不一致时，界面显示冲突：

- “采用原理图”：用 KiCad 中的订货料覆盖网页状态。
- “同步到原理图”：用网页当前选择覆盖 KiCad 属性。

订货料接口：

```text
POST /api/procurement_choice
```

`action` 支持 `select`、`clear`、`adopt_schematic`。`select` 必须提供有效的 HTTP/HTTPS `order_url`；同一 BOM 分组中的所有位号会保存相同选择。

### 订货料常见问题

- **为什么 C 编码列显示“无（关联 Cxxxx）”？** 海外订货报价本身没有立创自营 C 编码；括号内编码只是搜索结果关联信息。
- **为什么价格和库存没有同步进原理图？** 这些数据会变化，只保留在实时查询结果中；原理图只保存稳定的下单链接和物料摘要。
- **刷新后选择消失怎么办？** 检查 `part_selection_overrides.json` 对应位号是否存在 `procurement_choice`，并查看服务终端/同步运行日志中的错误。
- **同步后仍显示待同步怎么办？** 先查看该行冲突提示，再检查 KiCad 是否成功关闭并重新打开；同步流程会对磁盘字段做二次校验。

## 同步事务与日志

同步日志：

```text
BOM/part_selection_schematic_sync_log.csv
```

日志会记录：

```text
SyncedAt, Designator, Page, Field, OldValue, NewValue, SchematicPath
```

人工覆盖文件也会写入每个位号的最近同步摘要：

```text
schematic_synced_at
schematic_sync_summary
```

Comment、Value、Footprint 还会保存上次成功同步到 KiCad 的字段快照：

```text
schematic_field_snapshot
```

网页刷新时会比较“网页当前值 / 上次同步快照 / KiCad 当前值”：KiCad 单边修改会自动采用原理图值并清理过期网页覆盖；网页尚有未同步修改时继续保留网页值；两边都改过时在表格中标记冲突，可通过“采用原理图”明确处理。旧版没有快照时，会根据 `field_updated_at` 和 `schematic_synced_at` 兼容判断，并同时兼容 ISO 和旧斜杠日期格式。

同步运行日志：

```text
BOM/part_selection_schematic_sync_runtime.jsonl
```

网页每次点击单行“同步原理图”都会生成一个 `attempt_id`。运行日志按 JSON Lines 记录请求字段、KiCad 保存/关闭结果、文件写入、磁盘校验、通知 KiCad、自动恢复、总耗时和失败原因。同步失败时网页错误信息会附带日志编号，可直接按 `attempt_id` 查完整过程。

同步调用的 PowerShell 子进程使用 Windows 无窗口模式，不会反复弹出命令行。写文件前会保存并确认关闭当前 KiCad 原理图窗口；IPC 关闭未生效时会调用窗口关闭并再次确认，仍无法关闭则中止同步，不会冒险写入。写入并校验成功后，工具重新打开顶层原理图，并用搜索面板定位到本行第一个位号。

如果 KiCad 在第一次通知后把旧内容保存回磁盘，后端会自动执行一次完整恢复事务：保存并关闭当前 KiCad 页面、重写字段、磁盘校验、重新打开并再次校验。最多恢复两轮，仍不一致时才向网页报告失败。

### 单行同步

网页点击“同步原理图”会调用：

```text
POST /api/sync_schematic
```

后端入口：

```python
PartSelectionApp.sync_schematic()
```

写入函数：

```python
update_schematic_symbol_fields(doc_dir, refs, fields)
```

### 批量同步

批量同步会复用 Web 后端合并逻辑，不要手写 CSV 解析：

```powershell
python tools\part_selection_server.py --bulk-sync-bom BOM\MyBoard_LCSC_BOM.csv --bulk-sync-dry-run --bulk-sync-no-notify
```

确认 dry-run 后正式写入：

```powershell
python tools\part_selection_server.py --bulk-sync-bom BOM\MyBoard_LCSC_BOM.csv
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--bulk-sync-dry-run` | 只报告统计和缺失位号，不写原理图 |
| `--bulk-sync-no-notify` | 写完后不通知/激活 KiCad |
| `--bulk-sync-no-prepare` | 跳过写入前的编辑器保存/关闭保护，仅用于受控调试；不是只读模式 |

批量同步入口：

```python
PartSelectionApp.bulk_sync_bom_state_to_schematic()
```

批量写入函数：

```python
update_schematic_symbol_field_map(doc_dir, ref_fields)
```

这个函数会按 `ref -> fields` 先构建完整映射，每个 `.kicad_sch` 文件只读写一次，比逐行调用更稳也更快。

### 位号同步范围

独立发布版没有默认位号重映射，按当前 BOM 位号寻找原理图符号。不同工程不得套用原项目历史重编号规则。如确需迁移位号，调用方应显式提供经过核对的映射，并先 dry-run 检查目标。

## KiCad 位号跳转

注册自定义协议：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\install_kicad_schematic_search_protocol.ps1
```

网页位号链接形式：

```text
kicad-sch-search://R1169?page=PA
```

调试跳转：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\kicad_schematic_search.ps1 "kicad-sch-search://U16?page=DAC" -ShowErrors
```

日志位置：

```text
%TEMP%\kicad_schematic_search.log
```

当前跳转策略：

- KiCad 未打开时，先打开 `.kicad_pro` 工程，再打开根目录顶层 `.kicad_sch`，不是直接打开子页。
- KiCad 已打开时，优先使用搜索面板输入位号，再点击搜索结果定位。
- 点击搜索结果不要依赖固定屏幕坐标，应尽量基于窗口/控件状态定位；修改脚本后必须实机验证。
- 两次 UI 点击之间需要留出短延时，避免 KiCad 搜索结果尚未刷新就点击到旧结果。

## 静态 HTML/XLSX 导出

不启动网页服务，只生成复核文件：

```powershell
python tools\part_selection_helper.py
```

可选参数：

```powershell
python tools\part_selection_helper.py --refs R1,R2,C3
python tools\part_selection_helper.py --input BOM\part_selection_input.txt
python tools\part_selection_helper.py --bom BOM\source.csv
python tools\part_selection_helper.py --lcsc-bom BOM\MyBoard_LCSC_BOM.csv
```

输出：

```text
BOM/part_selection_review_*.html
BOM/part_selection_review_*.xlsx
```

这些导出文件是工具产物，不应该作为 Web UI 的可选 BOM 输入。
