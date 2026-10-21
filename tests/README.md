# 插件回归测试

本目录保留可复用测试和稳定夹具，不含生产 CAD、BOM、人工选料记录或 Git 数据。电路连通性、FPGA、工程目录检查和 U46 审核测试由原工程维护。

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
| `test_part_selection_specs.py`、`test_spec_check_integration.py`、`run_spec_checks_smoke.py` | 阻容感值等价、限制信息、可靠尺寸识别、缺失信息及只读 API 与原理图基线；Chromium 警告弹窗、迟到响应、字段编辑和实时库存反馈 |
| `test_row_filters.py`、`run_row_filters_smoke.py` | 主表字段/页面多选组合、条件保存和清除；筛选不减少共享库存总需求；Chromium 桌面/窄屏、隐藏列、存储失败及候选窗口隔离 |
| `test_part_selection_inventory.py`、`run_local_inventory_smoke.py` | 汇总列优先、去重/冲突、待匹配采购编码、确认令牌与变更检测、原子失败保护、共享库存/NC/套数、路径迁移、HTTP 与真实 Chromium 导入/持久化/列设置；只用合成表格及一次性工程 |
| `test_auto_column_widths.py`、`test_workbench_toolbar.py` | 自动列宽按钮位置、列宽计算与持久化，以及整页生成 JavaScript 的语法校验（包括 Python 字符串转义） |
| `test_workbench_interactions.py` | 初始化 HTTP/JSON/空来源失败及重试、未加载/切换来源时按钮保护、打包模块独立进程导入（防止测试顺序掩盖依赖问题） |
| `test_candidate_*`、`test_progressive_search.py`、`test_part_stock_lookup.py` | 搜索、筛选、库存、订货与请求竞态 |
| `test_*selection*`、`test_selected_schematic_sync.py` | 配置、主料一致性、记录及同步范围 |
| `test_path_settings.py`、`test_part_selection_overrides.py` | 更换型号网页搜索偏好、确认时间戳持久化及路径设置 |
| `test_field_mapping.py` | 中英文自动识别、标准字段优先、手动映射持久化、冲突拒绝、缺失属性隐藏创建、单行/所选同步与内置 Value/Footprint 镜像校验；仅使用合成工程 |
| `test_chinese_data_paths.py` | 中文与空格路径下的保存/加载、中文导出、隔离工程同步、旧标准配置迁移、外部备份、冲突及失败回滚；不操作生产工程 |
| `test_footprint_choices.py`、`test_part_selection_library.py` | 封装对比、库事务与回滚 |
| `test_kicad_*`、`test_start_part_selection_tool.py` | 桌面/安装入口、工程归属和忙碌保护 |
| `test_desktop_lifecycle.py` | 退出授权、并发保护、真实宿主进程退出监视及前端保存失败/重试 |
| `test_plugin_updates.py` | 正式发布清单、匿名额度规避与检查缓存、403/429 等待持久化/过期/损坏缓存、已校验包保留、版本/哈希/ZIP 安全、转换器保留、安装回滚及多宿主保护 |
| `test_test_artifacts.py` | 临时产物清理 |
| `run_*_smoke.py` | 按需真实窗口、安装包或联网转换冒烟 |

电感回归还覆盖 `Rated Current` / `Saturation Current` 的数字和 `not specified` 标签：只解析感值，不把电流当成另一个感值；错误电流单位和将电流标签用于电容不判一致。候选搜索回归覆盖前后端普通电感的简洁搜索及特例型号搜索。

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

`python -B tests/run_plugin_updates_smoke.py` 用真实 Chromium 驱动实际更新面板脚本，原生桥接为合成模拟：检查模式保存失败回退、已校验包不重复下载、独立下载后稍后重启、下载失败/重试、忙碌时关闭重开、源码模式不误报最新、自动下载不强制退出、未保存工程设置阻止重启、更新内容不执行 HTML。无后端、GitHub 请求或安装操作，不调用真实 KiCad；可设置工程外绝对目录 `PLUGIN_UPDATE_SCREENSHOTS` 留存桌面/390 像素宽截图。运行环境同其他 Node Playwright 脚本。

`python -B tests/run_workbench_interactions_smoke.py` 使用 Chromium 与自动清理的合成工程，验证菜单方向键/Home/End/Enter/空格/Tab/Esc、执行后焦点恢复、BOM 原生下拉框、启动 503 失败后重新读取、空筛选提示与清除、隐藏行不计入所选同步且取消筛选后勾选保留。可设置工程外已存在的 `WORKBENCH_AUDIT_SCREENSHOTS` 目录保存桌面和 800 像素宽截图；Node 需要能解析 playwright（可设置 NODE_PATH）。不访问商城、不调用真实同步、不打开用户 KiCad；localhost 仅为隔离测试传输。

`python -B tests/run_directed_row_merge_smoke.py` 在 Chromium 中分别以自动保存和 Enter 将 `4.7nF` 并入已有 `4.7nF 50V` 目标行，验证目标 C 编码、参数、备选与确认状态保留、待同步差异正确及重开持久化。仅使用 `test_directed_row_merge.py` 的一次性合成工程，不写真实工程、不执行 KiCad 同步；单元回归另覆盖 Comment/封装、反向位号、订货链接、目标不唯一和无目标场景。

`python -B tests/run_candidate_search_smoke.py` 使用真实 Chromium 和一次性合成工程，模拟上游分阶段响应，验证查询中不误报无结果、部分返回、补充来源、最终空结果与失败、筛选交互，以及 1475 / 800 / 390 像素宽度的状态栏布局。可设置工程外目录 `CANDIDATE_SCREENSHOTS` 保存截图。此测试不访问真实商城、不选择主料、不改生产记录，不代表真实网络时延验证。

生命周期冒烟还注入退出脚本缺失、列表未加载的场景，验证空白窗口可正常关闭；失败时输出页面地址、加载状态、行数及桥接可用性。测试子进程固定 UTF-8，避免 Windows 输出解码错误掩盖实际故障。

安装桌面依赖后运行 `python -B tests/run_desktop_lifecycle_smoke.py`，或通过 `--python "C:/path/to/desktop-runtime/Scripts/python.exe"` 指定已准备好依赖的解释器。它在外部临时合成工程打开隐藏的真实 WebView2 窗口，验证原生叉号触发行编辑保存、所属后台端口释放、重开数据保持、菜单重启及退出；正常清理测试宿主和工程，不触碰生产服务或 CAD。它不验证实际 KiCad 插件按钮、网络转换或真实工程同步。

`python -B tests/run_row_editing_smoke.py` 使用 Node Playwright 启动无头 Chromium，在自动清理的合成工程验证页面多选合行、字段/备注/C 编码自动保存、无 Enter 的自动分组/合行、输入法事件、失败重试和请求期间回改。Node 应能解析已安装的 playwright 包（可设置 NODE_PATH）；不对生产工程写入，也不执行真实 KiCad 保存关闭。

`test_path_settings.py` 持续回归路径默认值、转换器检测、原子保存、版本冲突、复制保留、目标冲突、失败回滚及启动来源；`python -B tests/run_path_settings_smoke.py` 用真实 Chromium 和临时工程验证设置菜单、默认折叠、取消、浏览按钮、保存前迁移确认、显式单次 BOM 导入、原理图启动默认值及桌面/窄屏布局。可设置工程外绝对目录 `PATH_SETTINGS_SCREENSHOTS` 留存截图。浏览选择接口在浏览器测试中模拟，不会弹出真实 Windows 对话框，不代表已验证 WebView2 或 KiCad 原生窗口。

查看 `python -B tests/run_kicad_plugin_smoke.py --help` 和 `python -B tests/run_easykiconverter_smoke.py --help` 后按需执行，只使用自动清理的隔离工程。桌面冒烟另需 `tools/kicad_plugin/desktop-requirements.txt` 与 `jsonschema`，部分步骤会联网。可复用的依赖运行时允许保留在外部缓存，并与一次性测试结果区分。

单元测试、打包和模拟 loader 检查不能证明实际 KiCad 按钮、窗口交互、网络转换或跨机器兼容已通过；本说明不声明这些真实冒烟已执行。

`test_candidate_native_window.py` 回归独立候选窗口的复用、地址/参数限制、桥接最小权限、保存忙碌拒绝退出、退出失败恢复和双窗清理。`python -B tests/run_candidate_window_smoke.py` 用 Chromium/合成商城响应验证候选独立页、父页桥接、收起后搜索筛选保持、不重复查询、保存回传、失败恢复、空白点击及拖选越界。可用工程外绝对路径 `CANDIDATE_WINDOW_SCREENSHOT` 保存截图。

`run_desktop_lifecycle_smoke.py` 还验证真实 WebView2 独立窗口移到父窗外、收起恢复、主料保存后刷新父页、忙碌拒绝关闭和退出销毁。`--only-candidate` 可只运行此阶段；商城响应在子页模拟、正式保存仅写一次性工程，不打开真实商城或修改生产 CAD。这验证原生窗口程序移动，不等同于人工跨屏拖动或真实 KiCad 按钮测试。
