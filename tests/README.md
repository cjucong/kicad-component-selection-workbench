# 可复用测试

这里保留回归测试源码、`fixtures/` 固定夹具和冒烟测试入口，不是运行后可以整目录删除的一次性数据。

## 测试范围

| 文件类别 | 用途 |
| --- | --- |
| `test_candidate_*`、`test_progressive_search.py`、`test_part_stock_lookup.py` | 候选搜索、筛选、库存、订货和请求竞态 |
| `test_*selection*`、`test_selected_schematic_sync.py` | 选料状态、分组、字段覆盖和同步 |
| `test_footprint_choices.py`、`test_part_selection_library.py` | 封装选择、转换、库事务及回滚 |
| `test_kicad_*`、`test_start_part_selection_tool.py` | 插件入口、隐藏启动、窗口归属和忙碌保护 |
| 其余 `test_*.py` | 表格布局、工程解析、电路专项及临时产物清理回归 |
| `run_*_smoke.py` | 按需运行的真实安装包/转换器/桌面窗口冒烟测试 |

常规验证（从工程根目录执行）：

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
python -B -m unittest discover -s tests
python -B -m pytest tests -p no:cacheprovider
```

第二条测试命令还覆盖使用 pytest 函数与 `scratch_project` 的测试，需要安装 pytest；普通单元测试不能代替真实 KiCad 窗口、网络、WebView2 和 EasyKiConverter 冒烟测试。U46 审核机制的独立回归入口是 `python -B tools/test_xc7a75t_io_audit.py`。

## 临时产物

新建临时工程统一使用 `_scratch.py` 的上下文管理器：

```python
from pathlib import Path
from _scratch import temporary_project

with temporary_project(prefix="example_") as directory:
    project = Path(directory)
    # Create disposable fixtures and reports here, never in the real project.
```

- 路径由 `tools/part_selection_runtime.py` 计算，在工程外同盘缓存的 `tests/` 下，不硬编码本机路径哈希。pytest 测试可使用 `conftest.py` 的 `scratch_project` 夹具，自动清理旁边缓存；既有系统 `TemporaryDirectory()` 和 pytest `tmp_path` 同样不应指向工程内部。
- 正常结束、断言失败或可展开的异常都会清理临时工程及其同级运行缓存；`setUp` 中使用时立即注册 `self.addCleanup(tmp.cleanup)`。强杀进程、断电或文件占用可能留下残余，不能保证这种情况下即时清理。
- 生成的临时工程、ZIP、截图、SVG、报告、解压目录和字节码不能提交；需要长期保留的稳定夹具须说明用途并人工审核。
- `run_kicad_plugin_smoke.py --prepare-runtime` 的依赖环境是工程外可复用缓存，刻意保留，不属于一次性输出。
- 不删除 `fixtures/` 或测试源码来代替清理产物。正式 CAD、BOM、库文件不能用于破坏性测试。
