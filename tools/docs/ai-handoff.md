# AI 与开发者接手说明

[工具首页](../README.md) | [使用教程](part-selection.md) | [故障排查](troubleshooting.md)

## 先读这些

1. 仓库根目录 `AGENTS.md` 和 `README.md`。
2. 目标工程 `BOM/part_selection_config.json`，确认绝对工程根目录与分页目录。
3. 本文、使用教程、安装说明；不要把原工程专用路径或映射带入新工程。

## 结构与数据流

`part_selection_server.py` 仍包含 API、应用服务及内联 HTML/CSS/JS，尚未拆成前后端目录。`PartSelectionApp.rows()` 合并原理图、源 BOM、overrides 与 stock cache，然后计算字段差异。人工确认同步后，统一字段写入函数修改原理图，随后磁盘校验并记录日志。

`part_selection_helper.py` 通过 `fpga_pin_assignment.py` 的 `parse_sexpr` 读取层级页名，因此该文件不能从安装包移除。它的其他项目专用功能不是本仓库对外接口，不要执行其主函数。

目标工程数据仍写入目标工程 `BOM/`：

- `part_selection_config.json`：目录与输入配置。
- `part_selection_overrides.json`：字段覆盖、确认料、备选料、订货选择。
- `lcsc_stock_cache.json`：库存、价格与物料参数缓存。
- `part_selection_schematic_sync_log.csv`：字段旧值/新值审计。
- `part_selection_schematic_sync_runtime.jsonl`：以 `attempt_id` 关联的事务阶段。

浏览器列布局在 localStorage，不在 overrides。普通浏览器与 WebView2 的存储互不共享。

## 插件

`kicad_plugin/desktop.py` 处理 IPC 工程识别、WebView2、窗口复用及服务身份校验。清单包含 `pcb` 与 `schematic` scope。`/api/config` 的 `desktop_launcher_version=2` 用于排除旧服务。

`package_plugin.py` 按白名单打包，不含工程数据。KiCad 管理的依赖只包含 wheel；WebView 的源码依赖在桌面专用 venv 安装，不能把 `pywebview` 塞回托管 `requirements.txt`。

## API

读取：`GET /api/rows`、`/api/boms`、`/api/stock`、`/api/lcsc_search`、`/api/config`。

保存：`POST /api/override`、`/api/field_override`、`/api/select_candidate`、`/api/adopt_schematic_fields`、`/api/alternate_parts`、`/api/procurement_choice`。

同步：`POST /api/sync_schematic`、`/api/bulk_sync_schematic`、`/api/kicad_jump`。

其他：`POST /api/export_changes`、`/api/export_pump_table`、`/api/open_lcsc_search`、`/api/open_project_path`、`/api/refs`。

写入流程复用 `update_schematic_symbol_field_map()`，批量按文件写入，不逐行反复读写同一页面。只增改属性，不替换符号/引脚/连线。不得跳过关闭编辑器保护后直接写正式工程。

## 发布分支差异

首次独立发布基于工具快照 `78bedc7`，没有带入其 Git 历史。默认 source 为 `__schematic__`，LCSC 文件匹配 `*LCSC*.csv`；移除原工程专用的六组默认位号映射及默认位号列表。测试中的旧工程名称仅用于合成回归样例，并不包含真实设计。

## 尚未解决

- PCB 编辑器入口点击无响应：用户点击后 desktop.log 没增加，尚未定位并修复。
- 结构化查询较慢：订货与商城请求目前串行，尚未做性能修复。
- 行勾选批量同步：用户已提出，尚未实现。现有批量接口会处理整个所选 BOM，不要用筛选后的 UI 行数冒充实际写入范围。
- 部分自动化依赖本机定制 `kicad-mcp`，其他机器需验证，不应宣称标准 KiCad 全兼容。

## 验证

```powershell
python -B -m unittest discover -s tests
python -B tools/kicad_plugin/package_plugin.py
```

测试使用合成工程。真实 UI 冒烟为 `tests/run_kicad_plugin_smoke.py`，需要 Windows、WebView2、`jsonschema` 及联网验证 schema，不纳入普通单测。正式写入前先在临时工程验证并使用 KiCad CLI 解析输出，不能拿用户工程做破坏测试。
