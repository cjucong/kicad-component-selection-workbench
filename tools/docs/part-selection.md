# 使用与路径配置

[工具首页](../README.md) | [插件安装](kicad-plugin.md) | [开发接手](ai-handoff.md) | [故障排查](troubleshooting.md)

命令从独立插件仓库根目录执行，工程位于仓库之外。Windows 10/11、Python 3.10+、openpyxl 为基础环境；独立窗口另需 WebView2 Runtime，库存网页回退需 Edge 或 Chrome。

## 显式启动工程

```powershell
python -m pip install -r tools/kicad_plugin/desktop-requirements.txt
python -B tools/kicad_plugin/desktop.py --project "E:/work/Board/Board.kicad_pro"
```

`--project` 接受绝对工程目录或工程文件。中文启动器 `tools/启动器件选料助手.bat` 转发 `--project` / `--config`；无参数时弹工程选择器，不绑定源码父目录。插件按钮优先识别调用它的 KiCad 工程，不能唯一识别时要求选择。

已有配置直接指定：

```powershell
python -B tools/kicad_plugin/desktop.py --config "E:/work/Board/BOM/part_selection_config.json"
```

只需浏览器时安装 `tools/requirements.txt` 并运行：

```powershell
python -B tools/part_selection_server.py --config "E:/work/Board/BOM/part_selection_config.json" --host 127.0.0.1 --port 8765 --open
```

后端也接受 `--project`；必须明确提供工程或配置，不从源码目录猜测。两参数同时给出时，配置的 project_root 必须与 --project 匹配。已有 `BOM/part_selection_config.json` 原样保留；新工程使用前核对配置和数据位置。

## JSON 路径

示例是供新工程参考的配置，不要用它覆盖已有工程配置：

```json
{
  "project_name": "Board",
  "project_root": "..",
  "schematic_dir": "DOC",
  "bom_dir": "BOM",
  "source_bom": "__schematic__",
  "lcsc_bom_pattern": "BOM/*LCSC*.csv",
  "input_refs": "BOM/part_selection_input.txt",
  "output_overrides": "BOM/part_selection_overrides.json",
  "stock_cache": "BOM/lcsc_stock_cache.json"
}
```

| 键 | 路径基准与用途 |
| --- | --- |
| `project_root` | 相对配置文件所在目录；配置放 BOM 下时推荐显式写 `..`。旧 BOM 配置的 `.` 根目录兼容规则保留 |
| `schematic_dir` | 相对工程根目录，通常 `DOC`；原理图位于根目录时写 `.` |
| `bom_dir` | 相对工程根目录，保存导出和同步日志；默认 `BOM` |
| `source_bom` | 默认 `__schematic__` 表示原理图当前数据；或指定相对工程根目录的 CSV |
| `lcsc_bom_pattern` | 相对工程根目录的 BOM 匹配规则，默认 `BOM/*LCSC*.csv` |
| `input_refs` | 相对工程根目录的位号输入文件 |
| `output_overrides` | 相对工程根目录的人工主料、确认、备注、备选与订货记录 |
| `stock_cache` | 相对工程根目录的库存/参数缓存 |
| `converter_executable` | 可选，转换器可执行文件，相对工程根目录或绝对路径 |

上述路径项也支持绝对路径及环境变量、`~` 展开。`source_bom` 的特殊值不是实际文件；开发代码保留其 Path 兼容表示。设置 bom_dir 不会自动重写 output_overrides、stock_cache 等其他显式路径，调整时逐项核对。

“文件 → 路径配置文件…”通过 POST /api/open_path_config 用记事本打开当前 JSON。**当前没有 UI 路径编辑器，也不实时应用修改。** 保存选料后停止该工程后端，备份 JSON 并编辑，再使用上面的 `--config` 命令重启；只刷新网页或关闭独立窗口不足以确保旧后端退出。不要按端口号终止未核实的服务。

## 迁移已有记录

**更改输出路径不会自动搬迁文件。** 先备份原记录和配置，停止对应后端，将 overrides、库存缓存及需保留的同步日志复制到新位置，再修改各路径、重启并核对主料、备注、确认、备选和订货信息。保留原副本直到核对完成。若新路径为空，应先查明是否漏复制，未经确认不得新建空记录替代旧数据。

`bom_dir` 下的同步日志通常为 `part_selection_schematic_sync_log.csv`、`part_selection_schematic_sync_runtime.jsonl`；已有工程仍沿用原 BOM 路径。浏览器列布局保存在 localStorage，普通浏览器与独立窗口不共享。

临时缓存默认在工程同级的 `../.kicad-part-selection-cache/<工程名>-<路径哈希>/`。可在启动前设置：

```powershell
$env:KICAD_PART_SELECTION_CACHE = 'E:/KiCadCaches'
$env:KICAD_PART_SELECTION_HOME = 'E:/KiCadDesktop'
python -B tools/kicad_plugin/desktop.py --config "E:/work/Board/BOM/part_selection_config.json"
```

CACHE 是绝对、工程外同盘的缓存父目录，程序追加工程路径哈希子目录；不能用跨盘目录或链接回工程。HOME 是绝对、工程外的桌面日志/环境基目录，默认 `%LOCALAPPDATA%/KiCadPartSelection`。两者不负责移动正式记录或库文件。

## 选料与确认

工作台按 Comment、Value、Footprint 完全一致分组，一行一个主料 C 编码和确认状态。同组只有一个非空编码时补齐空白并设待确认；存在不同非空编码时清空整行并待确认；确认状态不一致也设待确认。读取原理图当前数据时会将统一结果保存到记录，整理前在外部缓存保存审计备份；历史 BOM 不写回当前选料记录。备注、备选、订货和来源保留。

点击“更换型号”搜索候选，可选主料、备选或订货；缓存不代表实时库存，采购前核对官网。候选按品牌、封装及具名参数筛选，同字段多选取并集、跨字段取交集；仅现货按每个渠道报价分别判断。精确 C 编码不扩大为替代搜索。

Value、Comment、封装默认不替换。选择替换 Value/Comment 时，阻容感采用可靠电气数值及 resistor/capacitor/inductor，其他器件采用完整型号；没有可靠数值则保留当前值。完整型号仍保留在物料参数。保存选料是保存待同步记录，后续仍须确认同步。

## 封装与工程库

历史 BOM 的位号映射由工程 JSON 的 `bulk_sync_ref_map` 显式配置，例如 `{"U56": "U28"}`；独立插件没有预置某个工程的映射。此配置只用于旧 BOM 的全表同步；“原理图当前数据”和明确勾选的同步始终按当前位号处理，不套用历史映射。使用旧 BOM 前须核对其位号版本。

封装支持保留、选择工程已注册本地库、立创转换。库选择只读编号/尺寸，点预览才渲染；立创转换先在外部缓存生成并比较。焊盘编号不符或原封装不可读时禁止替换；编号相同仍须人工核对尺寸与引脚功能。

目标库已经可在对比界面选择：合并现有库、新建工程内库或独立转换库。示例中的 DSMU 只是原工程库名，其他工程使用自己的库。同名不同几何另存，不覆盖共享旧封装。

导入后的符号、封装与 sym-lib-table/fp-lib-table 是工程设计依赖，仍随工程管理。移动程序或缓存不应移除它们；选择外部记录位置也不代表库会搬迁。详见 [转换说明](easykiconverter.md)。

## 同步与导出

“重新读取”从磁盘原理图读取，不读取编辑器未保存内容；会切回原理图当前数据并清空当前筛选和勾选。“同步所选行”只写当前可见且勾选的位号，隐藏选择不参与；“同步全部”不受筛选限制。执行前确认范围、保存编辑器并备份，窗口归属不明或忙碌时停止。

同步仅更新属性，不替换已放置符号几何、引脚或 PCB。Footprint 字段变化后仍需人工核对并从原理图更新 PCB。历史工程专用位号映射不能用于新的明确勾选同步。

BOM 导出菜单可按筛选、库存、确认和待同步状态选择范围，导出排除 NC；导出改动表用于复核。工程自身的生产 BOM 导出器由工程维护，不随插件分发。单元测试不能替代实际 KiCad 窗口、网络或转换器冒烟。
