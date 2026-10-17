# 选料与 KiCad 故障排查

[工具首页](../README.md) | [使用教程](part-selection.md) | [AI 接手](ai-handoff.md) | [故障排查](troubleshooting.md)

独立仓库用法见 [仓库首页](../../README.md)。下文命令按工程内 `tools/` 布局编写；独立克隆时使用 `desktop.py --project`，或为服务命令补上目标工程 `--config`，不要误读工具仓库为电路工程。

## 网页打不开或服务停止

- 双击 `tools/start_part_selection_tool.bat` 重新隐藏启动；不需要保留终端窗口。
- 查看 `%TEMP%\part_selection_tool_startup.log`。这是现有启动器的系统临时日志位置，与同步日志不同。
- 检查 `8765` 端口是否被其他程序占用；不要按端口号直接结束未经核实身份的进程。
- `tools/start_part_selection_tool.bat --find-python` 可诊断 Python 选择；依赖与配置要求见 [使用教程](part-selection.md)。

## 同步失败如何查后台

先取网页错误中的 `attempt_id`，在 `BOM/part_selection_schematic_sync_runtime.jsonl` 中查同一次事务；再核对 `BOM/part_selection_schematic_sync_log.csv` 的字段旧值与新值。区分“窗口没有保存关闭”“写入失败”“磁盘校验失败”“重开定位失败”，不要靠重复点击覆盖问题。

## 常见问题

### 刷新后库存或物料参数丢失

检查：

- `BOM/lcsc_stock_cache.json` 是否有对应 C 编码。
- `BOM/part_selection_overrides.json` 是否保存了该行 `stock_c_code` 或最终接受的 C 编码。
- 浏览器请求 `/api/rows?...&stock=0` 时只会读缓存，不会联网刷新；点击库存按钮才会查询。

### 批量同步反复报大量变更

先确认是不是 KiCad 打开的旧页面在同步前保存并覆盖磁盘。先只读检查同步范围：

```powershell
python -B tools\part_selection_server.py --bulk-sync-bom BOM\MyBoard_LCSC_BOM.csv --bulk-sync-dry-run --bulk-sync-no-notify
```

不要将 `--bulk-sync-no-notify --bulk-sync-no-prepare` 当作只读诊断：没有 `--bulk-sync-dry-run` 仍会写原理图，并跳过编辑器保护。反复出现大量变更时，重点检查：

- KiCad 字符串转义/反转义。
- 是否对同一位号多次生成不同字段。
- 是否有重复位号或跨页同名符号。

### KiCad 点击位号只填了搜索框但没定位

检查：

- `%TEMP%\kicad_schematic_search.log`
- 搜索结果是否还是旧位号。
- 两次点击之间是否有足够延时。
- 搜索面板控件是否因 KiCad 版本、语言、缩放、显示器变化而变化。

### 同步后 KiCad 看不到变化

KiCad 可能仍显示内存里的旧页。当前流程会保存并关闭原理图、写入和校验磁盘、重开顶层页并定位；确认运行日志中这些步骤实际成功。原理图内存没关闭成功时应中止，不要绕过保护继续写。同步只更新属性，不会替换符号引脚或几何。

### 原理图页面缺符号

先比较当前页和历史页的目标位号、实例 UUID、引脚与导线，保留当前其他器件的修改；不要用历史整页覆盖，也不要盲补孤立符号块。恢复后导出原生网表检查受影响引脚并用 KiCad CLI 验证。
