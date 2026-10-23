# 插件回归测试

本目录保留可复用测试和稳定夹具，不含生产 CAD、BOM、人工选料记录或 Git 数据。电路连通性、FPGA、工程目录检查和 U46 审核测试由原工程维护。

更新回归包含 `test_tool_windows.py` 的网络阻塞并发场景（工具可打开，退出和重复更新受保护），以及 `test_plugin_updates.py` 的 TLS 中断重试、证书拒绝、超时提示。真实 WebView2 的 `run_native_tool_windows_probe.py` 还通过实际桥接挂起更新检查并打开设置窗口；网络延迟为受控测试，不代表真实下载验证。

## 运行

从独立插件仓库根目录执行：

```powershell
python -m pip install -r tools/requirements.txt pytest
$env:PYTHONDONTWRITEBYTECODE = '1'
python -B -m pytest tests -p no:cacheprovider
```

Node.js 用于部分前端行为检查；涉及 KiCad CLI/pcbnew、WebView2 或转换器的检查需对应运行环境。报告实际失败、跳过及缺失依赖，不把跳过当成验证通过。仅 unittest discovery 不覆盖所有 pytest 测试。

| 测试类别 | 范围 |
| --- | --- |
| `test_workbench_undo.py`、`run_workbench_undo_smoke.py` | 未同步编辑链快照、重开保持、旧记录回退、同步后失效、过期预览及缺位号保护；Chromium 取消/确认、自动保存队列、选料和确认状态恢复、桌面/窄屏边界。仅用一次性工程，不回写生产 CAD |
| `test_supply_column.py`、`run_supply_column_smoke.py` | 合并主料/供货列、旧列布局及默认配置迁移；Chromium 编码自动保存/清空/重开、库存刷新保持输入焦点、订货渠道链接及桌面/窄屏控件边界。使用合成工程与本地库存响应，不联网或写生产 CAD |
| `test_row_footprint.py`、`run_inventory_footprint_smoke.py` | 独立行封装预览/应用、无编码改名、确认状态、过期/取消/转换失败隔离、同名冲突及事务回滚；合并库存的未知与零值、同编码请求合并、跨行并行和迟到结果保护。使用可清理合成工程，不写生产 CAD |
| `test_part_selection_specs.py`、`test_spec_check_integration.py`、`run_spec_checks_smoke.py` | 阻容感值等价、限制信息、可靠尺寸识别、缺失信息及只读 API 与原理图基线；Chromium 警告弹窗、迟到响应、字段编辑和实时库存反馈 |
| `test_row_filters.py`、`run_row_filters_smoke.py` | 主表字段/页面多选组合、条件保存和清除；筛选不减少共享库存总需求；Chromium 桌面/窄屏、隐藏列、存储失败及候选窗口隔离 |
| `test_part_selection_inventory.py`、`run_local_inventory_smoke.py` | 汇总列优先、去重/冲突、待匹配采购编码、确认令牌与变更检测、原子失败保护、共享库存/NC/套数、路径迁移、HTTP 与真实 Chromium 导入/持久化/列设置；只用合成表格及一次性工程 |
| `test_auto_column_widths.py`、`test_workbench_toolbar.py` | 自动列宽按钮位置、列宽计算与持久化，以及整页生成 JavaScript 的语法校验（包括 Python 字符串转义） |
| `test_workbench_interactions.py` | 初始化 HTTP/JSON/空来源失败及重试、未加载/切换来源时按钮保护、打包模块独立进程导入（防止测试顺序掩盖依赖问题） |
| `test_candidate_*`、`test_progressive_search.py`、`test_part_stock_lookup.py` | 搜索、筛选、库存、订货与请求竞态 |
| `test_product_details.py`、`test_candidate_parameter_completion.py`、`run_candidate_parameter_completion_smoke.py` | 商品详情页完整属性、身份/来源隔离、固定域名、大小/缓存/并发边界；搜索和库存刷新保留同料号已知参数；Chromium 后台补取、迟到结果、失败重试、温度系数/工作电压筛选及保存。默认合成来源，不修改生产 CAD/记录 |
| `test_*selection*`、`test_selected_schematic_sync.py` | 配置、主料一致性、记录及同步范围 |
| `test_path_settings.py`、`test_part_selection_overrides.py` | 更换型号网页搜索偏好、确认时间戳持久化及路径设置 |
| `test_field_mapping.py` | 中英文自动识别、标准字段优先、手动映射持久化、冲突拒绝、缺失属性隐藏创建、单行/所选同步与内置 Value/Footprint 镜像校验；仅使用合成工程 |
| `test_chinese_data_paths.py` | 中文与空格路径下的保存/加载、中文导出、隔离工程同步、旧标准配置迁移、外部备份、冲突及失败回滚；不操作生产工程 |
| `test_footprint_choices.py`、`test_part_selection_library.py` | 封装选择、仅改名按铜皮焊盘布局匹配并提示丝印差异、同名暂停保存、显式另存/令牌授权覆盖、目标变化与备份、覆盖失败回滚 |
| `test_footprint_geometry.py`、`test_footprint_alignment_integration.py` | 编号焊盘刚性配准、中心对齐及间距差异提示、圆/方焊盘差异保留、PCB 实例基准/正反面/旋转还原、原理图已换而 PCB 未换、转换与仅改名的实际焊盘坐标不变、显式旧库恢复、真实 pcbnew 几何变换、图层渲染、库发布及 Chromium SVG 像素检查；使用合成 PCB，不写生产 CAD |
| `test_footprint_overlay_ui.py`、`run_footprint_overlay_smoke.py` | Chromium 同比例叠加/并排、独立图层与不透明度、原点/1号焊盘、迟到响应、仅改名快捷路径、同名覆盖警告/另存/取消及桌面和窄屏边界；候选保存重试链另见 test_candidate_selection.py |
| `test_kicad_*`、`test_start_part_selection_tool.py` | 桌面/安装入口、工程归属和忙碌保护 |
| `test_desktop_lifecycle.py` | 退出授权、并发保护、真实宿主进程退出监视及前端保存失败/重试 |
| `test_plugin_updates.py` | 正式发布清单、匿名额度规避与检查缓存、403/429 等待持久化/过期/损坏缓存、已校验包保留、版本/哈希/ZIP 安全、转换器保留、安装回滚及多宿主保护 |
| `test_test_artifacts.py` | 临时产物清理 |
| `run_*_smoke.py` | 按需真实窗口、安装包或联网转换冒烟 |

电感回归还覆盖 `RMS Current` / `Sat Current`（以及旧版 `Rated Current` / `Saturation Current`）的数字和 `not specified` 标签：只解析感值，不把电流当成另一个感值；错误电流单位和将电流标签用于电容不判一致。候选搜索回归覆盖前后端普通电感的简洁搜索及特例型号搜索。

位号输入专项（Windows PowerShell 5.1）：

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
python -B -m pytest tests/test_kicad_search_text.py tests/test_kicad_busy_guard.py tests/test_kicad_manager_open.py tests/test_kicad_save_fallback.py -p no:cacheprovider
```

`test_kicad_search_text.py` 仅加载搜索脚本函数，模拟 Win32/UIA 边界并注入 `C103 → C03`、读写超时、非目标焦点和不支持的控件等故障；另用一次性隐藏原生 Edit 控件验证实际消息写入、完整值读取与文本变化通知。位号写入不经过 SendKeys 或剪贴板，搜索面板不再重复键入已校验的值；无法读回完整值时停止跳转。该专项不启动 KiCad、不保存或关闭真实原理图，也不能替代实际 KiCad/wx 控件、输入法环境和搜索结果刷新的人工冒烟。

## 隔离与清理

导航专项：

```powershell
python -B -m pytest tests/test_kicad_navigation_preferences.py tests/test_path_settings.py -p no:cacheprovider
```

上述测试模拟 Win32 边界，确认仅最小化时还原、默认恢复鼠标及留在原理图模式、设置校验与即时生效，不操作用户编辑器。行编辑 Chromium 冒烟另覆盖自动保存差异、连续修改与回改、Enter 反馈一致及确认料不重建当前行；设置冒烟覆盖鼠标选项取消、保存和重开保持。

```python
from pathlib import Path
from _scratch import temporary_project

with temporary_project(prefix="example_") as directory:
    project = Path(directory)
    # Only disposable fixtures belong here.
```

pytest 可使用 `conftest.py` 的 `scratch_project`；unittest 在 setUp 中立即注册 `self.addCleanup(tmp.cleanup)`。临时工程及它生成的同级运行缓存均在外部容器，正常结束、断言失败或异常展开时清理。其他 TemporaryDirectory/tmp_path 也须满足相同边界。强杀或文件占用可能留下残余，后续核对绝对路径再清理。

临时 ZIP、解压文件、截图、报告、下载、字节码和测试缓存不得写入源码或正式工程，不得提交。稳定夹具应最小化并说明用途，禁止为测试复制真实工程或其 `.git`。

## 真实冒烟

原生流程夹具使用同盘短路径的一次性工程，避免 KiCad 管理器将长路径省略后无法核实工程归属。PCB 实例配准回归同时覆盖更新后附带的 `sheetname` / `sheetfile` 元数据，不把这些字段误判为不支持的几何。

`run_native_footprint_workflow.py` 为真实工作台和 KiCad 更新流程创建可清理的双器件工程：联网调用已安装转换器，兼容平铺及分单元符号引脚，先用 KiCad CLI 导出网表验证每个引脚网络再建立 PCB 基线。给原封装加入非零局部原点和 90 度旋转，PCB 包含正面 37 度电阻、背面 90 度芯片及各焊盘走线。只用于 Windows、KiCad 10 和已安装插件；报告目录必须在仓库和正式工程外。它不是自动通过的 pytest，也不模拟 KiCad 的更新操作。

```powershell
python -B tests/run_native_footprint_workflow.py --report-dir C:/external/footprint-workflow
# 保留该进程。另一个终端启动同一临时工程的工程管理器及编辑器：
python -B tests/run_native_footprint_workflow.py --launch-session C:/external/footprint-workflow/session.json
# 在真实工作台中选择“仅改名”，同步原理图，再用 KiCad“从原理图更新 PCB”并保存。
python -B tests/run_native_footprint_workflow.py --verify-session C:/external/footprint-workflow/session.json --stage rename
# 再实际选择“替换立创封装”，同步、更新并保存 PCB 后：
python -B tests/run_native_footprint_workflow.py --verify-session C:/external/footprint-workflow/session.json --stage converted
```

可通过 Playwright CDP 操作实际 WebView2 工作台，通过 KiCad IPC 触发更新和保存；原生更新确认不由该脚本替代。校验会拒绝未真正更换封装的 PCB，检查 PCB 库标识与原理图一致、器件原点/角度/正反面/UUID 路径不变、逐编号焊盘世界坐标偏差不超过 5 nm、网络及所有原有走线不变。工程管理器启动使用外部隔离配置和独立 IPC，保留用户全局设置。验证完成先正常关闭所属工作台和测试 KiCad 窗口，再在保留的进程输入 `finish` 清理；不要对生产编辑器发退出命令。只有两阶段均实际更新并通过校验，才可描述为端到端通过。

`python -B tests/run_footprint_overlay_smoke.py` 使用合成比较接口，在真实 Chromium 中检查预览图层、叠加/并排切换、缓存和过期响应、关闭清理及 1440/390 像素布局，截图在工程外运行缓存。`navigation` 场景还覆盖资料未返回时先显示窗口、同窗前进后退、搜索和改名保持、转换失败不阻塞改名、关闭后迟到响应不复活、读取失败重试。`test_footprint_choices.py` 覆盖库对比按原坐标渲染、双源缓存及源文件变更校验。Node.js 需能解析 Playwright，可用 `NODE_PATH` 指向已安装依赖。完整 pytest 在依赖缺失时明确跳过对应浏览器检查；显式冒烟缺少依赖会失败。`test_footprint_geometry.py` 与 `test_footprint_alignment_integration.py` 自动寻找 KiCad CLI 相邻或 Windows KiCad 安装目录的 Python，存在时执行真实 pcbnew 变换和渲染，不存在时明确跳过。集成测试仅模拟联网转换输入，使用真实几何配准/库发布/Chromium SVG 像素校验；不声明真实联网转换或生产 PCB 更新已验证。

`python -B tests/run_plugin_updates_smoke.py` 用真实 Chromium 驱动实际更新面板脚本，原生桥接为合成模拟：检查模式保存失败回退、已校验包不重复下载、独立下载后稍后重启、下载失败/重试、忙碌时关闭重开、源码模式不误报最新、自动下载不强制退出、未保存工程设置阻止重启、更新内容不执行 HTML。无后端、GitHub 请求或安装操作，不调用真实 KiCad；可设置工程外绝对目录 `PLUGIN_UPDATE_SCREENSHOTS` 留存桌面/390 像素宽截图。运行环境同其他 Node Playwright 脚本。

`python -B tests/run_workbench_interactions_smoke.py` 使用 Chromium 与自动清理的合成工程，验证菜单方向键/Home/End/Enter/空格/Tab/Esc、执行后焦点恢复、BOM 原生下拉框、启动 503 失败后重新读取、空筛选提示与清除、隐藏行不计入所选同步且取消筛选后勾选保留。可设置工程外已存在的 `WORKBENCH_AUDIT_SCREENSHOTS` 目录保存桌面和 800 像素宽截图；Node 需要能解析 playwright（可设置 NODE_PATH）。不访问商城、不调用真实同步、不打开用户 KiCad；localhost 仅为隔离测试传输。

`python -B tests/run_directed_row_merge_smoke.py` 在 Chromium 中分别以自动保存和 Enter 将 `4.7nF` 并入已有 `4.7nF 50V` 目标行，验证目标 C 编码、参数、备选与确认状态保留、待同步差异正确及重开持久化。仅使用 `test_directed_row_merge.py` 的一次性合成工程，不写真实工程、不执行 KiCad 同步；单元回归另覆盖 Comment/封装、反向位号、订货链接、目标不唯一和无目标场景。

`python -B tests/run_candidate_search_smoke.py` 使用真实 Chromium 和一次性合成工程，模拟上游分阶段响应，验证 600 ms 自动搜索、输入法组合输入、Enter 立即搜索、清空失效，以及查询中不误报无结果、部分返回、补充来源、最终空结果与失败、筛选交互和 1475 / 800 / 390 像素宽度的状态栏布局。可设置工程外目录 `CANDIDATE_SCREENSHOTS` 保存截图。此测试不访问真实商城、不选择主料、不改生产记录，不代表真实网络时延验证。

`python -B tests/run_candidate_parameter_completion_smoke.py` 使用真实 Chromium 与合成列表/详情响应，验证首屏不等待详情、不误报厂商缺参、旧查询迟到参数不串料、完整参数筛选、失败重试、选用后保存及 CAD 保持不变。截图存到工程外运行缓存，Node 需能解析 playwright（可设置 NODE_PATH）。真实商城读取需另行只读验证，不能把此离线测试当作联网验证。

生命周期冒烟还注入退出脚本缺失、列表未加载的场景，验证空白窗口可正常关闭；失败时输出页面地址、加载状态、行数及桥接可用性。测试子进程固定 UTF-8，避免 Windows 输出解码错误掩盖实际故障。

安装桌面依赖后运行 `python -B tests/run_desktop_lifecycle_smoke.py`，或通过 `--python "C:/path/to/desktop-runtime/Scripts/python.exe"` 指定已准备好依赖的解释器。它在外部临时合成工程打开隐藏的真实 WebView2 窗口，验证原生叉号触发行编辑保存、所属后台端口释放、重开数据保持、菜单重启及退出；正常清理测试宿主和工程，不触碰生产服务或 CAD。它不验证实际 KiCad 插件按钮、网络转换或真实工程同步。

`python -B tests/run_row_editing_smoke.py` 使用 Node Playwright 启动无头 Chromium，在自动清理的合成工程验证页面多选合行、字段/备注/C 编码自动保存、无 Enter 的自动分组/合行、输入法事件、失败重试和请求期间回改。Node 应能解析已安装的 playwright 包（可设置 NODE_PATH）；不对生产工程写入，也不执行真实 KiCad 保存关闭。

`test_path_settings.py` 持续回归路径默认值、转换器检测、原子保存、版本冲突、复制保留、目标冲突、失败回滚及启动来源；`python -B tests/run_path_settings_smoke.py` 用真实 Chromium 和临时工程验证设置菜单、默认折叠、取消、浏览按钮、保存前迁移确认、显式单次 BOM 导入、原理图启动默认值及桌面/窄屏布局。可设置工程外绝对目录 `PATH_SETTINGS_SCREENSHOTS` 留存截图。浏览选择接口在浏览器测试中模拟，不会弹出真实 Windows 对话框，不代表已验证 WebView2 或 KiCad 原生窗口。

查看 `python -B tests/run_kicad_plugin_smoke.py --help` 和 `python -B tests/run_easykiconverter_smoke.py --help` 后按需执行，只使用自动清理的隔离工程。桌面冒烟另需 `tools/kicad_plugin/desktop-requirements.txt` 与 `jsonschema`，部分步骤会联网。可复用的依赖运行时允许保留在外部缓存，并与一次性测试结果区分。

单元测试、打包和模拟 loader 检查不能证明实际 KiCad 按钮、窗口交互、网络转换或跨机器兼容已通过；本说明不声明这些真实冒烟已执行。

`test_value_parameters.py` 回归阻容感参数选择、单位标签、缺失/冲突、同类替换和特殊限制保留；`test_part_selection_specs.py` 包含电阻 ppm 温漂尾缀的保守解析。`python -B tests/run_value_parameters_smoke.py` 用真实 Chromium 和自动清理的合成工程测试箭头、勾选预览、取消/应用、自动保存及重开、过期拒绝、确认状态和 C 编码保留，以及 1440/768/390 像素布局。截图在工程外缓存；不访问商城、不同步或修改生产 CAD，不代表真实 KiCad 更新验证。

`test_candidate_native_window.py` 回归独立候选窗口的复用、地址/参数限制、桥接最小权限、保存忙碌拒绝退出、退出失败恢复和双窗清理。`python -B tests/run_candidate_window_smoke.py` 用 Chromium/合成商城响应验证候选独立页、父页桥接、收起后搜索筛选保持、不重复查询、保存回传、失败恢复、空白点击及拖选越界。可用工程外绝对路径 `CANDIDATE_WINDOW_SCREENSHOT` 保存截图。

封装管理的 `pane-radios` 与 `rename-retry` 浏览器回归分别检查缓存页单选组隔离、原封装资料失败后的就地重试且保留改名草稿。`run_inventory_footprint_smoke.py` 在一次性工程通过真实 KiCad 渲染核验改名、现有库选用、转换封装三条独立保存路径；上游转换输入为合成夹具，不将它表述为联网或 PCB 更新验证。

`run_desktop_lifecycle_smoke.py` 还验证真实 WebView2 独立窗口移到父窗外、收起恢复、主料保存后刷新父页、忙碌拒绝关闭和退出销毁。`--only-candidate` 可只运行此阶段；商城响应在子页模拟、正式保存仅写一次性工程，不打开真实商城或修改生产 CAD。这验证原生窗口程序移动，不等同于人工跨屏拖动或真实 KiCad 按钮测试。

## 标准库原位更新 PCB

`test_design_sync.py`、`test_design_source.py`、`test_footprint_change_types.py` 覆盖分阶段目标/范围、源版本、仅改名证据、字段更新、失败及 PCB 单独重试；`run_design_sync_smoke.py` 检查 B 菜单及确认流程。`run_tool_windows_smoke.py` 检查固定工具页、草稿保护及父表局部刷新；`run_native_tool_windows_probe.py --report-dir <工程外目录>` 用真实 WebView2 检查工具窗口和受限桥接，不是 KiCad 写入测试。

`run_native_design_sync_probe.py --report-dir <工程外目录>` 在真实 KiCad 合成工程验证字段/改名完整实例、单步撤销、原生阻塞弹窗后的分阶段重试，再运行正背面库替换、中心/严格对齐、网络/走线及保存重开。使用桌面 Python/kipy 和 Node Playwright，报告工程外保存；不会接受生产工程路径。失败即记录，不得把部分通过当作整套通过。

2026-10-23 验证记录：1,169 项 pytest 通过；B 菜单 Chromium 检查覆盖 1440/768/390 像素，工具窗口检查覆盖草稿保护及未改行 DOM/焦点保持。真实 WebView2 覆盖 10 类窗口、移出主窗口及受限桥接。真实 KiCad `10.0.0-19-g65df3ab11c` 完整探测通过字段、仅改名、原生弹窗受阻后的 PCB 单独重试、正背面严格/中心对齐、重复预检不重复换封装、网络/走线、单步撤销、自动打开、保存重开，以及进程和目录清理。该记录不代表真实联网转换、生产工程更新或其他 KiCad 版本验证。

`test_pcb_native_types.py` 的对象顺序回归覆盖字段、焊盘、图形混排，以及字段值/几何/网络/UUID/位置/元数据/重复对象数量变化。只允许顶层封装对象顺序变化，不能以此放宽完整内容校验。真实探测夹具包含 `LCSC` 和 `Comment` 自定义 PCB 字段，覆盖原生暂存类型重排和回读。

中心对齐另由 `test_pcb_in_place_geometry.py` 覆盖 L9 同型间距差（3.6068 / 3.048 mm）、中心不动、逐焊盘移动 0.2794 mm、正背面及编号拒绝；`run_pcb_updates_smoke.py` 覆盖默认中心模式、严格模式失败、切换清除确认和迟到响应隔离。真实探测在严格流程后运行不同间距标准库的正背面中心更新，独立核验中心、实际移动量、目标几何、网络、走线不动、单步撤销与保存重开。两种模式的“位置保持”含义不同，不能把中心对齐写成每颗焊盘原位。

`test_pcb_session.py` 另覆盖从原理图入口按需连接 PCB、PCB 晚于工作台打开、多工程/同工程重复编辑器、独立服务端令牌及本机接口发现。真实探测脚本的工作台阶段不传入 PCB 启动接口，由正式连接逻辑发现并核验一次性工程的实时 PCB。

`test_pcb_schematic_identity.py` 覆盖复制工程遗留根 UUID、PCB 省略根 UUID、实际 Sheetfile 层级（含子目录）、无关页面/错误页面链/其他工程实例拒绝、缺页和循环诊断。真实 PCB 探测夹具采用三级页面、历史根实例记录和省略根 UUID 的 PCB 路径，避免只测手工拼出的平面根路径。

`test_pcb_in_place_geometry.py`、`test_pcb_session.py`、`test_pcb_transaction.py`、`test_pcb_update.py` 与 `test_pcb_updates_ui.py` 覆盖严格编号配准、工程身份、批次边界、备份失败、暂存/回读失败、未知结果不重放、源变化及确认状态隔离。`run_pcb_updates_smoke.py` 为可复用离线 Chromium 交互检查；设置 `PCB_SCREENSHOT` 为工程外绝对文件名前缀可保留截图。

`run_native_pcb_in_place_probe.py --report-dir <工程外绝对目录>` 是明确选择运行的真实 KiCad 验证，使用已安装桌面 Python 环境的 `kipy`、KiCad 捆绑 Python 和 Node Playwright（需配置 `NODE_PATH`）。创建并自动清理合成工程，不接受生产工程路径；验证正面任意角度、背面、偏移原点、真实标准库几何替换、网络与走线不变、此前未保存修改、一个撤销步骤，以及显式保存重开。包含真实工作台选库、原理图保存关闭/恢复、分阶段同步及 PCB 更新流程。报告和截图在指定工程外目录，不能把离线浏览器模拟或接口存在误称为真实流程通过。
