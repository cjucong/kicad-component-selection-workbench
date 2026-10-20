# 选料插件工具

命令从独立插件仓库根目录执行，目标工程单独指定。这里不包含 FPGA、PCB 3D 或生产 BOM 导出工具。

## 启动

```powershell
python -m pip install -r tools/kicad_plugin/desktop-requirements.txt
python -B tools/kicad_plugin/desktop.py --project "E:/path/to/project/project.kicad_pro"
python -B tools/kicad_plugin/desktop.py --config "E:/path/to/project/BOM/part_selection_config.json"
```

上面两条启动命令择一。中文入口 `tools/启动器件选料助手.bat` 支持相同参数，无参数则打开工程选择器。只支持独立工作台，不提供浏览器模式或自动回退。关闭窗口会保存行编辑并退出所属后台；“文件”菜单提供重启与退出，任务忙碌时暂不关闭。

## 代码与说明

| 位置 | 用途 |
| --- | --- |
| `part_selection_server.py` | 本地工作台、API、同步协调 |
| `part_selection_helper.py`、`part_selection_overrides.py` | 原理图/BOM 读取、配置、记录与字段处理 |
| `part_selection_paths.py` | 共享显式工程/配置 CLI 路径解析，随安装包分发 |
| `start_part_selection_tool.vbs`、`launch_part_selection.py` | 隐藏启动桥，保留 --project/--config；缺目标时选择工程 |
| `kicad_sexpr.py` | 独立 KiCad S-expression 解析器 |
| `part_stock_lookup.py`、`part_selection_search.py` | 库存与候选查询 |
| `part_selection_library.py`、`part_selection_footprints.py`、`web/` | 库事务、封装预览与网页资源 |
| `part_selection_runtime.py` | 工程外临时缓存定位 |
| `part_selection_lifecycle.py`、`web/desktop-lifecycle.js` | 保存后关闭、持有令牌的后台退出及宿主监视 |
| `kicad_plugin/` | 桌面入口、IPC 清单、安装包构建 |
| [使用教程](docs/part-selection.md) | 工作流、配置路径及记录迁移 |
| [插件安装](docs/kicad-plugin.md) | ZIP、直接安装和桌面运行 |
| [转换器](docs/easykiconverter.md) | 封装/符号转换和完整版 |
| [开发接手](docs/ai-handoff.md) | 数据流、安全边界及验证 |
| [故障排查](docs/troubleshooting.md) | 工程、日志、依赖与同步问题 |
| [测试](../tests/README.md) | 回归及隔离冒烟 |

## 数据边界

程序在本仓库或用户插件目录；配置、人工记录、库存缓存和导入库属于目标工程。已有工程配置继续使用。顶部“文件 → 工程与路径配置…”可编辑数据与运行路径，正式数据默认在工程 BOM 下，支持自定义目录并复制已有数据，保留原文件；数据路径立即应用，运行环境路径通过“文件 → 重启工作台”生效。

转换、预览、安装备份等使用工程外同盘缓存；正式库不能移进临时缓存。目标库选择在现有封装对比 UI 中完成。先读 [AGENTS.md](../AGENTS.md)，不要对生产 CAD/BOM 做测试写入。
