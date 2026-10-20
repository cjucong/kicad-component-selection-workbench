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
| `test_candidate_*`、`test_progressive_search.py`、`test_part_stock_lookup.py` | 搜索、筛选、库存、订货与请求竞态 |
| `test_*selection*`、`test_selected_schematic_sync.py` | 配置、主料一致性、记录及同步范围 |
| `test_footprint_choices.py`、`test_part_selection_library.py` | 封装对比、库事务与回滚 |
| `test_kicad_*`、`test_start_part_selection_tool.py` | 桌面/安装入口、工程归属和忙碌保护 |
| `test_desktop_lifecycle.py` | 退出授权、并发保护、真实宿主进程退出监视及前端保存失败/重试 |
| `test_plugin_updates.py` | 正式发布清单、匿名额度规避与检查缓存、版本/哈希/ZIP 安全、转换器保留、安装回滚及多宿主保护 |
| `test_test_artifacts.py` | 临时产物清理 |
| `run_*_smoke.py` | 按需真实窗口、安装包或联网转换冒烟 |

位号输入专项（Windows PowerShell 5.1）：

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
python -B -m pytest tests/test_kicad_search_text.py tests/test_kicad_busy_guard.py tests/test_kicad_manager_open.py tests/test_kicad_save_fallback.py -p no:cacheprovider
```

`test_kicad_search_text.py` 仅加载搜索脚本函数，模拟 Win32/UIA 边界并注入 `C103 → C03`、读写超时、非目标焦点和不支持的控件等故障；另用一次性隐藏原生 Edit 控件验证实际消息写入、完整值读取与文本变化通知。位号写入不经过 SendKeys 或剪贴板，搜索面板不再重复键入已校验的值；无法读回完整值时停止跳转。该专项不启动 KiCad、不保存或关闭真实原理图，也不能替代实际 KiCad/wx 控件、输入法环境和搜索结果刷新的人工冒烟。

## 隔离与清理

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

安装桌面依赖后运行 `python -B tests/run_desktop_lifecycle_smoke.py`，或通过 `--python "C:/path/to/desktop-runtime/Scripts/python.exe"` 指定已准备好依赖的解释器。它在外部临时合成工程打开隐藏的真实 WebView2 窗口，验证原生叉号触发行编辑保存、所属后台端口释放、重开数据保持、菜单重启及退出；正常清理测试宿主和工程，不触碰生产服务或 CAD。它不验证实际 KiCad 插件按钮、网络转换或真实工程同步。

`python -B tests/run_row_editing_smoke.py` 使用 Node Playwright 启动无头 Chromium，在自动清理的合成工程验证页面多选合行、字段/备注/C 编码自动保存、输入法事件、失败重试和请求期间回改。Node 应能解析已安装的 playwright 包（可设置 NODE_PATH）；不对生产工程写入，也不执行真实 KiCad 保存关闭。

`test_path_settings.py` 持续回归路径默认值、转换器检测、原子保存、版本冲突、复制保留、目标冲突、失败回滚及启动来源；`python -B tests/run_path_settings_smoke.py` 用真实 Chromium 和临时工程验证设置菜单、默认折叠、取消、浏览按钮、保存前迁移确认、显式单次 BOM 导入、原理图启动默认值及桌面/窄屏布局。可设置工程外绝对目录 `PATH_SETTINGS_SCREENSHOTS` 留存截图。浏览选择接口在浏览器测试中模拟，不会弹出真实 Windows 对话框，不代表已验证 WebView2 或 KiCad 原生窗口。

查看 `python -B tests/run_kicad_plugin_smoke.py --help` 和 `python -B tests/run_easykiconverter_smoke.py --help` 后按需执行，只使用自动清理的隔离工程。桌面冒烟另需 `tools/kicad_plugin/desktop-requirements.txt` 与 `jsonschema`，部分步骤会联网。可复用的依赖运行时允许保留在外部缓存，并与一次性测试结果区分。

单元测试、打包和模拟 loader 检查不能证明实际 KiCad 按钮、窗口交互、网络转换或跨机器兼容已通过；本说明不声明这些真实冒烟已执行。
