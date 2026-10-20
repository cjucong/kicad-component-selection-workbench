# AI 与开发者接手说明

[工具首页](../README.md) | [使用教程](part-selection.md) | [AI 接手](ai-handoff.md) | [故障排查](troubleshooting.md)

文中命令均从工程根目录执行。

## 接手顺序与边界

1. 先读根目录 [AGENTS.md](../../AGENTS.md)，再读本文件和 [使用教程](part-selection.md)。
2. 日常网页服务、KiCad 跳转、FPGA 工具仍在 `tools/` 根目录，不要在整理专项脚本时顺带移动它们。
3. `pcb_3d/` 只保留可复用 QA 板生成器，不加入网页启动依赖；旧一次性 3D/封装修复脚本已删除。详见 [3D 清单](../pcb_3d/README.md) 与 [测试说明](../../tests/README.md)。
4. `part_selection_library.py` 已集成 EasyKiConverter 独立 CLI；`part_selection_footprints.py` 提供隔离转换、真实 CLI SVG 对比及目标库事务。正式网页封装替换必须先 `phase=preview`，再提交绑定的 `footprint_choice`，不能绕过用户确认直接导入。默认合并 DSMU，同名不同几何另存 C 编码后缀；已有脚本无 phase 的兼容接口仍保留。预览绑定候选、位号、覆盖版本与原理图封装，过期或并发变更须重做预览。默认保留每个位号自己的 Footprint，确认后也仅更新待同步字段。`update_schematic_symbol_fields()` 仍只更新属性，不替换符号几何、引脚或 PCB。安全边界及完整版构建见 [转换说明](easykiconverter.md)。
5. 主料选择 payload 的 `replace_value`、`replace_comment` 为独立严格布尔值，缺省均 false；省略的字段不新建覆盖，也不清掉既有 pending overrides。按每个位号分别推导：R/C/L 的 Comment 为 resistor/capacitor/inductor、Value 为明确电气数值；其他器件两项均为完整型号。Description 始终保留“型号: MPN”。搜索框后紧邻三个 select，不放到底部；Value/Comment 的字符串值显式转换为 JSON 布尔值。封装 keep/library/convert 分别为不替换、库选择、立创转换，新开重置、最小化恢复保留。library 必须 phase=preview，prepare_library 不转换/不渲染，library_detail 懒读编号与尺寸，POST /api/library_footprint 仅 render=true 才生成 SVG；chooseLibraryFootprint 使用请求序号和 AbortController 隔离旧请求。确认要求编号匹配、明确勾选及原文件/行版本未变，不修改库或符号。回归测试在 `tests/test_candidate_selection.py`、`tests/test_footprint_choices.py`，测试不要写正式工程。
6. 选料窗口不再提供“结构化查询”“自动列宽”按钮及刷新图标；保留自动搜索、Enter 查询、缓存到期后台更新和 force API。主表/候选列宽默认自动适配，columnWidthMode/candidateColumnWidthMode=auto 跟随新内容与窗口尺寸；手动拖动/双击设 manual，旧保存宽度按手动处理，避免刷新覆盖用户布局。
7. 独立窗口 create_window 必须传 text_select=True；loaded 回调在 UI 线程启用 WebView2 的 AreBrowserAcceleratorKeysEnabled 和 AreDefaultContextMenusEnabled，不开启开发者工具。页面普通内容允许选择，位号有文字选区时保留原生复制菜单；窗口标题文字不触发移动，空白处仍可拖动。不要通过全局 preventDefault 拦截复制粘贴，也不要把只读表格改成 contenteditable。宿主设置需重开助手窗口后生效，无需重启 KiCad。

安装版入口见 [KiCad 插件说明](kicad-plugin.md)：`kicad_plugin/desktop.py` 负责 IPC 工程识别、窗口和服务身份校验，`package_plugin.py` 按白名单打包核心模块。清单声明 PCB 和 schematic scope；不携带工程状态。`/api/config` 的 `desktop_launcher_version=2` 用于拒绝复用旧版服务。

0.1.2 额外提供 `kicad_plugin/__init__.py` PCB ActionPlugin 兼容入口，绕过跨编辑器共享 IPC 按钮注册表；保留原 IPC。安装目录改为 `com_smu_part-selection`，旧 dotted 目录备份迁移；更换安装后须重启 KiCad。真实点击尚待验证，不把模拟 loader 测试视为已实测。

KiCad 的依赖安装只接受 wheel，故 `kicad_plugin/requirements.txt` 不包含 WebView 宿主；`desktop-requirements.txt` 在独立的桌面 venv 安装。不要重新把 `pywebview` 放进前者（其 `proxy_tools` 只有源码包）。原理图窗口 PID 缓存按工程隔离，使用前重新核实绝对路径；无法识别所属工程的窗口必须阻止同步，不得回退到“第一个窗口”。

## 数据流与依赖

候选表本地筛选：candidateResultEntries() 将商城项及去重订货报价展开，candidateEntryFields() 提取品牌/封装及具名参数，不提供型号 facet；订货自身参数覆盖相同名称的商城字段。filterCandidateEntries() 同字段 OR/跨字段 AND，独立按报价库存筛现货，buildCandidateFacets() 按其他条件统计可选数并保留已选零计数项。筛选只改显示及可选映射，原始候选不改；新搜索清空、最小化保留，后续部分结果沿用条件。现货控件位于渠道表头，防止拖动表头吞掉筛选点击。回归见 tests/test_candidate_filters.py，测试用临时工程，不写正式 BOM/CAD。

```text
BOM CSV + overrides + stock cache + 当前原理图
  -> PartSelectionApp.rows()
  -> 网页分组、字段对比与待同步状态
  -> 手工确认同步 -> KiCad 属性写入 -> 磁盘校验 -> 日志
```

- `part_selection_server.py` 仍同时包含 HTTP 路由、应用服务及内联 HTML/CSS/JS；本轮没有拆后端或前端，也没有引入构建工具。
- `part_selection_helper.py` 读取页面名时依赖 `fpga_pin_assignment.py` 的 `parse_sexpr`。这不是无用脚本，分发时不能遗漏；后者当前仅依赖标准库。
- 保存状态在 `BOM/part_selection_overrides.json`，动态库存/参数缓存在 `BOM/lcsc_stock_cache.json`；浏览器列宽、列顺序和显示配置在 localStorage，不在 overrides 中。
- 同步审计为 `BOM/part_selection_schematic_sync_log.csv`，运行过程为 `BOM/part_selection_schematic_sync_runtime.jsonl`。修改同步逻辑前先按 `attempt_id` 看失败阶段。
- 跨电脑使用建议带上完整 `tools/` 和所需配置，不要只复制 `part_selection_*.py`，否则会漏掉库存模块和解析器。

## 关键文件

| 文件 | 作用 |
| --- | --- |
| `part_selection_server.py` | 本地 Web UI、API、网页渲染、批量同步入口 |
| `part_selection_helper.py` | KiCad 原理图解析/写入、BOM 合并、静态 HTML/XLSX 导出 |
| `part_selection_overrides.py` | 配置、人工覆盖记录、最终 C 编码选择规则 |
| `part_stock_lookup.py` | 立创库存/价格/物料参数查询与缓存 |
| `part_selection_search.py` | 分批搜索任务、短缓存、旧请求取消与会话序号 |
| `part_selection_library.py` | 转换器发现、隐藏有界 CLI、暂存校验、项目相对库表、哈希复用及异步选料任务 |
| `part_selection_footprints.py` / `web/footprint-compare.js` / `.css` | 临时转换、真实封装预览、焊盘编号拦截、复用/目标库选择、事务保存与取消；回归测试 `test_footprint_choices.py` |
| `kicad_plugin/prepare_converter_bundle.py` | 准备固定版本转换器源码、依赖许可及来源清单，不携带工程数据 |
| `kicad_schematic_search.ps1` | 从网页位号链接跳转 KiCad 原理图搜索结果 |
| `kicad_ipc_action.py` | KiCad IPC 调用辅助 |
| `install_kicad_schematic_search_protocol.ps1` | 注册浏览器自定义协议 |
| `kicad_schematic_search_launcher.vbs` | 隐藏 PowerShell 窗口启动跳转脚本 |
| `requirements.txt` | Python 依赖，目前只有 `openpyxl` |
| `fpga_pin_assignment.py` / `.html` | FPGA 分配工具；Python 内还包含被选料模块复用的 S-expression 解析器 |
| `update_xc7a75t_io_audit.py` / `test_xc7a75t_io_audit.py` | U46 审核与独立机制回归测试 |
| `pcb_3d/` | 只读输入 PCB 的 QA 板生成器，需独立检查 `pcbnew` 环境 |

## API 速查

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/` | Web UI |
| `GET` | `/api/rows?refs=&bom=&stock=0/1` | 读取当前合并行 |
| `GET` | `/api/boms` | BOM 下拉列表 |
| `GET` | `/api/stock?code=Cxxxx` | 查询/读取库存缓存 |
| `GET` | `/api/lcsc_search?keyword=...` | 内嵌候选型号搜索 |
| `GET` | `/api/lcsc_search?progressive=1&keyword=...&client=...&sequence=N&force=0/1` | 启动分批查询，立即返回快照和 job_id；保留同步旧接口 |
| `GET` | `/api/lcsc_search?progressive=1&job=...` | 获取当前 items、sources、done、revision、checked_at、cached/stale、elapsed_ms |
| `POST` | `/api/lcsc_search_cancel` | client + 单调递增 sequence，取消旧排队任务和后续结果发布 |
| `GET` | `/api/config` | 当前配置摘要 |
| `POST` | `/api/override` | 保存 C 编码/状态覆盖 |
| `POST` | `/api/field_override` | 保存 Comment/Value/Footprint 覆盖 |
| `POST` | `/api/select_candidate` | 保存完整主料候选及绑定参数，更新整行所有位号 |
| `POST` | `/api/start_selection` | 异步选料；footprint_mode=keep/library/convert，默认保留现有封装 |
| `POST` | `/api/library_footprint` | 库选择的懒加载编号/尺寸；只有 render=true 才生成预览 |
| `GET` | `/api/selection_job?id=...` | 转换阶段、完成/失败及保存结果；不得把排队响应当成功 |
| `POST` | `/api/adopt_schematic_fields` | 明确采用 KiCad 当前 Comment/Value/Footprint |
| `POST` | `/api/alternate_parts` | 添加、删除、排序备选料或采用原理图备选料 |
| `POST` | `/api/procurement_choice` | 选择、清除订货料或采用原理图订货信息 |
| `POST` | `/api/sync_schematic` | 单行同步到 KiCad 原理图 |
| `POST` | `/api/kicad_jump` | 使用当前服务的工程路径跳转；不依赖系统协议的旧注册路径 |
| `POST` | `/api/bulk_sync_schematic` | 批量同步；`selected_refs` 非空数组限定源位号，省略表示整张 BOM；空/无效/过期选择拒绝 |
| `POST` | `/api/export_changes` | 导出改动表 |
| `POST` | `/api/export_pump_table` | 按当前导出条件生成采购 BOM |
| `POST` | `/api/open_project_path` | 打开工程目录 |
| `POST` | `/api/open_lcsc_search` | 打开官网快速搜索窗口 |
| `POST` | `/api/refs` | 保存位号输入文件 |

### 批量范围与缓存回归

- 勾选同步只发送当前可见且已选位号；后端在映射前再次筛选，不能按相交分组整行扩写。`selected_refs` 路径不套用历史 U 位号迁移表，以当前勾选位号为准；旧全量/CLI 路径保留原兼容行为。
- 没有任何可写目标时只返回缺失清单，不关闭编辑器、不调用空字段写入。`tests/test_selected_schematic_sync.py` 包含实际临时文件写入、HTTP 范围转发及 Node 前端半选/隐藏状态回归。
- `part_stock_lookup._FetchCache` 使用单调时钟、60 秒 TTL、32 项 LRU、每来源最多 4 个不同的在途请求；同 key 共享 Future，结果深拷贝，空结果/异常可立即重试。Chromium 启动仍串行，网络合作库存抓取与商城抓取并行。不要恢复无期限页面缓存或对精确 C 编码调用宽泛替代搜索。

## AI 接手原则

1. 先读 `BOM/part_selection_config.json`，确认工程根目录、原理图目录、BOM 和缓存路径。
2. 获取网页状态时用 `PartSelectionApp.rows()`，不要自己拼 CSV、overrides、stock cache。
3. 写原理图前先 dry-run 或只读统计位号，尤其要输出缺失位号清单。
4. 修改 `.kicad_sch` 前先备份目标页，尤其是 KiCad 正打开时。
5. 批量写原理图用 `update_schematic_symbol_field_map()`，不要循环 200 多行逐次写同一批文件。
6. 写完必须用 KiCad CLI 解析全部 `DOC/*.kicad_sch`，避免原理图打不开。
7. 不要把 `NC` 当成 LCSC 链接目标。
8. 不要直接删除或重排原理图符号块；字段同步只应该改/增 `property`。
9. 如果用户提供历史 `.kicad_sch` 救回器件，先按 UUID、器件与网络对比历史和当前改动；不能整页覆盖丢掉当前其他修改。保留实例 UUID，恢复后导出原生网表逐引脚检查。
10. 每次功能改动都跑测试，并把关键命令和结果告诉用户。

自动改动前创建带时间戳的非破坏式 `git stash create` / `git stash store`，不清空用户工作区；临时输出统一放在工程外 `runtime_directory(project_root)`，测试使用自动清理的临时子目录。子进程使用 `CREATE_NO_WINDOW` 或隐藏启动，不弹命令行。只提交本次改动，不夹带原理图、PCB、库存或选型状态的既有修改。

## 验证命令

单元测试：

```powershell
python -B -m unittest discover -s tests
python -B tools/test_xc7a75t_io_audit.py
```

Python 语法检查（只在内存中编译，不生成源码目录缓存）：

```powershell
python -B -c "from pathlib import Path; files=list(Path('tools').rglob('*.py')); [compile(p.read_bytes(),str(p),'exec') for p in files]; print(f'{len(files)} Python files parsed')"
```

抽取网页内嵌 JavaScript 做语法检查：

```powershell
$tmp = Join-Path (Resolve-Path Temp) "part_selection_ui_check.js"
$env:PART_SELECTION_UI_CHECK = $tmp
@'
from pathlib import Path
import os, sys
root = Path.cwd()
sys.path.insert(0, str(root / "tools"))
from part_selection_server import PartSelectionApp, render_index
html = render_index(PartSelectionApp(root / "BOM" / "part_selection_config.json"))
start = html.index("<script>") + len("<script>")
end = html.index("</script>", start)
Path(os.environ["PART_SELECTION_UI_CHECK"]).write_text(html[start:end], encoding="utf-8")
print(os.environ["PART_SELECTION_UI_CHECK"])
'@ | python -B -
node --check $tmp
```

KiCad CLI 解析全部原理图：

```powershell
$failed = @()
Get-ChildItem DOC -Filter *.kicad_sch | ForEach-Object {
  $out = Join-Path (Resolve-Path Temp) ($_.BaseName + "_python_bom.xml")
  & "C:\Program Files\KiCad\10.0\bin\kicad-cli.exe" sch export python-bom --output $out $_.FullName *> $null
  if ($LASTEXITCODE -ne 0) { $failed += $_.Name }
}
if ($failed.Count) { "FAILED: " + ($failed -join ", ") } else { "all_schematic_pages_parse_ok" }
```

目录整理回归：`python -B -m unittest discover -s tests -p test_tools_layout.py`。覆盖日常入口保留、专项脚本唯一位置、默认根目录、PowerShell 解析、文档链接，并在可用的 KiCad Python 下从其他工作目录执行 `--help`。若本机 `import pcbnew` 已失败，这项会明确跳过；不能据此宣称模型执行流程已通过。
