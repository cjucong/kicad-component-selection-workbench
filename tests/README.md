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
| `test_test_artifacts.py` | 临时产物清理 |
| `run_*_smoke.py` | 按需真实窗口、安装包或联网转换冒烟 |

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

查看 `python -B tests/run_kicad_plugin_smoke.py --help` 和 `python -B tests/run_easykiconverter_smoke.py --help` 后按需执行，只使用自动清理的隔离工程。桌面冒烟另需 `tools/kicad_plugin/desktop-requirements.txt` 与 `jsonschema`，部分步骤会联网。可复用的依赖运行时允许保留在外部缓存，并与一次性测试结果区分。

单元测试、打包和模拟 loader 检查不能证明实际 KiCad 按钮、窗口交互、网络转换或跨机器兼容已通过；本说明不声明这些真实冒烟已执行。
