# 选料与 KiCad 故障排查

[工具首页](../README.md) | [使用教程](part-selection.md) | [AI 接手](ai-handoff.md) | [故障排查](troubleshooting.md)

文中命令均从工程根目录执行。

## 网页打不开或服务停止

- 双击 `tools/start_part_selection_tool.bat` 重新隐藏启动；不需要保留终端窗口。
- 查看 `%TEMP%\part_selection_tool_startup.log`。这是现有启动器的系统临时日志位置，与同步日志不同。
- 检查 `8765` 端口是否被其他程序占用；不要按端口号直接结束未经核实身份的进程。
- `tools/start_part_selection_tool.bat --find-python` 可诊断 Python 选择；依赖与配置要求见 [使用教程](part-selection.md)。

## 同步失败如何查后台

先取网页错误中的 `attempt_id`，在 `BOM/part_selection_schematic_sync_runtime.jsonl` 中查同一次事务；再核对 `BOM/part_selection_schematic_sync_log.csv` 的字段旧值与新值。区分“窗口没有保存关闭”“写入失败”“磁盘校验失败”“重开定位失败”，不要靠重复点击覆盖问题。

### 保存或关闭编辑器一直卡住

先等待 KiCad 恢复响应，检查任务栏是否有保存错误或未处理的对话框；不要连续点击同步，也不要强制终止仍有未保存改动的 KiCad。原理图、PCB 和工程管理器可能共用一个进程，终止进程会影响其他编辑器。

工具现在在工程归属检查、保存和关闭前使用最长 750ms 的只读响应检测；工程管理器的 UIAutomation 路径读取在独立隐藏子进程中执行，最长 2.5 秒。无响应或读取超时会报告 `KICAD_BUSY` 并停止本次同步，不继续排队保存/关闭、不写入原理图。超时仅终止工具自己的只读探测子进程，不终止 KiCad。日志中的 `phase` 区分卡在窗口归属、工程路径读取、保存前或关闭前。

这项保护避免窗口信息读取拖住同步，但不代表已修复 KiCad 内部的保存/备份卡顿。2026-10-20 本机查出工程 `Temp/` 约 17.9GB，5 个不完整的项目备份 ZIP 包含大量临时文件。按用户要求，旧临时目录已移至工程外缓存的 `legacy-project-temp-20261020/`；损坏 ZIP 隔离到 `maintenance_20261020/corrupt-backups/`，保留检查清单和原偏好设置。

本机 KiCad 10 用户配置 `kicad_common.json` 的 `auto_backup.enabled` 已关闭；这会影响本用户所有 KiCad 10 工程的自动项目备份，独立的定时自动保存/恢复设置未修改，`.history/` 也未删除。需要恢复自动备份时，在 KiCad 首选项中重新启用；先确保临时文件在工程外，核对备份内容和保存耗时，不要在重新启用前把历史缓存复制回工程。此处不是插件安装时自动关闭备份的全局策略。

运行时缓存默认位于 `../.kicad-part-selection-cache/<工程名>-<路径哈希>/`，可运行 `python -B -c "import sys; sys.path.insert(0, 'tools'); from part_selection_runtime import runtime_directory; print(runtime_directory('.'))"` 获取。转换、预览、库事务和选料整行统一审计备份均在此处；`BOM/` 的选料记录、库存缓存与同步日志仍保留。原子保存只在目标文件旁创建短暂的 `.overrides-*.tmp`，成功或异常后立即清理。不得用链接将外部缓存接回工程。

## 转换封装失败

先确认“选用主料时”选择了保留还是转换。轻量包不含转换器，需安装完整版或配置 `PART_SELECTION_CONVERTER` / `converter_executable`。看错误提示的工程外缓存日志绝对路径；转换最长 180 秒，失败时原主料不变。库表同名冲突、已有导入文件被改过、生成格式不完整会停止，不要反复点选用或删掉用户库来绕过。导入、焊盘兼容确认及原理图同步是不同阶段，见 [转换说明](easykiconverter.md)。

## 插件入口弹出选择工程文件

这是自动识别当前工程失败后的兜底，不是 BOM 导入窗口。查看 `%LOCALAPPDATA%\KiCadPartSelection\desktop.log` 中的 `IPC PCB project path`、`IPC schematic document count` 和 `IPC schematic project path`。

入口会先校验 PCB 返回的工程路径；空路径、相对路径或失效目录都继续尝试原理图文档。同一工程的目录与 `.kicad_pro` 路径先归一化再比较。只有不能确定唯一有效工程时才要求手动选择，不能用插件工作目录或上次工程猜测当前工程，避免写错项目。

部分 KiCad 版本的原理图接口只返回顶层文件名，`project.path` 为空。入口此时读取 KiCad 启动插件时传入的 `KIPRJMOD`，并检查当前顶层原理图和同名 `.kicad_pro` 确实位于该目录；缺失、相对路径、文件不匹配或多工程仍进入手动选择。日志中的 `IPC schematic environment` 可查看这一回退，不记录令牌。2026-10-17 已在当前 KiCad 打开的原理图中点击验证：直接打开助手，不再弹选文件框。

## 常见问题

### 刷新后库存或物料参数丢失

检查：

- `BOM/lcsc_stock_cache.json` 是否有对应 C 编码。
- `BOM/part_selection_overrides.json` 是否保存了该行 `stock_c_code` 或最终接受的 C 编码。
- 浏览器请求 `/api/rows?...&stock=0` 时只会读缓存，不会联网刷新；点击库存按钮才会查询。

### 批量同步反复报大量变更

先确认是不是 KiCad 打开的旧页面在同步前保存并覆盖磁盘。先只读检查同步范围：

```powershell
python -B tools\part_selection_server.py --bulk-sync-bom BOM\PXIE_4137_LCSC_BOM_20260813_101540.csv --bulk-sync-dry-run --bulk-sync-no-notify
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

KiCad 可能仍显示内存里的旧页。当前流程先回读磁盘字段；已经一致的行不会保存、关闭或重开 KiCad。确实需要写入时，已保存的编辑器才会自动关闭，随后写入、校验磁盘并重开顶层页定位。若原理图有未保存修改且 KiCad IPC 保存接口不可用，工具会停止并提示先在 KiCad 按 `Ctrl+S` 保存，不再发送可能造成卡顿的菜单保存指令。确认运行日志中这些步骤实际成功；原理图内存没关闭成功时应中止，不要绕过保护继续写。同步只更新属性，不会替换符号引脚或几何。

### 已经同步却仍显示未同步

先刷新工作台当前数据，再检查逐位号字段差异。`description_c_code` 是工具内部的参数来源元数据，不是 KiCad 属性，不应出现在待同步字段中；旧版误比较会造成假待同步。真实的 C 编码、物料参数、Value、Comment、封装、备选料及订货字段差异仍需要核对，不应直接清空状态掩盖问题。

### 原理图页面缺符号

先比较当前页和历史页的目标位号、实例 UUID、引脚与导线，保留当前其他器件的修改；不要用历史整页覆盖，也不要盲补孤立符号块。恢复后导出原生网表检查受影响引脚并用 KiCad CLI 验证。
