# 选料助手工具目录

运行与安装步骤见 [仓库首页](../README.md)。仓库可以独立于 KiCad 工程克隆，优先通过 `kicad_plugin/desktop.py --project` 指定真实工程。

| 位置 | 职责 |
| --- | --- |
| `part_selection_server.py` | HTTP API、合并状态、同步事务与内联网页 |
| `part_selection_helper.py` | 原理图字段读写、BOM 读取与复核导出 |
| `part_selection_overrides.py` | 配置、人工覆盖与备选料/订货状态 |
| `part_stock_lookup.py` | 商城候选料、库存、参数与订货渠道解析 |
| `fpga_pin_assignment.py` | 保留被选料模块依赖的 S-expression 解析器，不作为独立 FPGA 功能发布 |
| `kicad_plugin/` | IPC 插件清单、桌面窗口与白名单打包 |
| `kicad_schematic_search.ps1` / `kicad_ipc_action.py` | Windows 原理图跳转与同步自动化 |
| `docs/` | 使用教程、安装、排错与 AI 接手 |

`.bat` 是原工程内使用方式的兼容入口，不会自动寻找任意外部工程。不要将工作台目录误当作电路工程。
