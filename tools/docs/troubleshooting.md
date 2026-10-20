# 故障排查

[工具首页](../README.md) | [使用与配置](part-selection.md) | [插件安装](kicad-plugin.md)

## 工程不对或配置未生效

从独立仓库运行时必须指定目标，例如：

```powershell
python -B tools/kicad_plugin/desktop.py --config "E:/work/Board/BOM/part_selection_config.json"
python -B tools/part_selection_server.py --config "E:/work/Board/BOM/part_selection_config.json" --open
```

两条分别用于独立窗口和浏览器，择一运行。--project 可替代 --config；同时给出时，配置解析根目录必须匹配。不要把插件仓库当成工程，不要为排错复制整套工程工具。

中文启动器无参数弹工程选择器是预期行为。KiCad 按钮无法唯一识别工程时也会要求手动选择，这不是 BOM 导入窗口。

“文件 → 路径配置文件…”用记事本打开当前 JSON，不是 UI 路径编辑器。配置更改需要重启对应后端，刷新网页或关闭窗口可能仍复用旧服务。检查服务报告的工程和配置路径后再重启；不要按端口号盲目结束进程，也不要切换别人的工程。

## 记录、库存或日志消失

先核对 JSON 的 output_overrides、stock_cache、bom_dir 和 project_root。更改路径不会自动搬迁旧文件；不要把空白页面当成旧记录已清除，更不要未经确认保存空记录。停止对应服务，备份原文件并复制到新位置后再启动核对。

已有 BOM/part_selection_config.json 应继续使用原路径；bom_dir 变化不会自动改其他显式路径。列宽/列顺序在 localStorage，浏览器和独立窗口不共享；这与人工主料记录不同。

库存缓存不是实时结果，查询失败和无货应分开看。核对所选 C 编码、缓存对应项与来源时间，采购前检查官网。

## 服务或桌面窗口打不开

确认 Python 3.10+、pip 和依赖可用，桌面环境使用 tools/kicad_plugin/desktop-requirements.txt，浏览器后端使用 tools/requirements.txt。独立窗口还需 WebView2 Runtime，网页回退需 Edge/Chrome。离线机器须提前准备依赖。

查看桌面基目录的 desktop.log；默认 %LOCALAPPDATA%/KiCadPartSelection，KICAD_PART_SELECTION_HOME 设置后使用该位置。工程哈希目录中的 server.log、runtime-install.log、window.log 分别用于后端、私有环境与窗口诊断。PCB 兼容入口另查 pcb-launch.log。旧辅助跳转脚本如报告日志路径，以实际错误输出为准，不把系统临时目录当成正式记录或新的 QA 输出位置。

KiCad IPC 入口需启用 API。直接安装与 PCM 副本重复时核对安装来源，升级入口后重启 KiCad；不能把图标出现视为全部业务已通过。

## 缓存路径报错

KICAD_PART_SELECTION_CACHE 必须是绝对、工程外同盘的父目录，不能指向工程内部、跨盘或通过链接回到工程。KICAD_PART_SELECTION_HOME 也须绝对且在工程外。在启动前设置环境变量，重启目标后端后再检查。

默认外部缓存为工程同级 .kicad-part-selection-cache 下按工程路径哈希隔离的目录。查询实际位置：

```powershell
python -B -c "import sys; from pathlib import Path; sys.path.insert(0, 'tools'); from part_selection_runtime import runtime_directory; print(runtime_directory(Path('E:/work/Board')))"
```

正式库不属于缓存；不能为清缓存删除工程 Libraries 或库表。不要把旧临时资料复制回工程 Temp。插件安装不会自动更改 KiCad 全局备份设置。

## 同步失败或 KiCad 卡住

按错误 attempt_id 查 bom_dir 下的 part_selection_schematic_sync_runtime.jsonl，再核对 part_selection_schematic_sync_log.csv。先区分工程识别、保存/关闭、写入、磁盘校验和重开定位阶段。

KICAD_BUSY 或窗口归属不明时应停止本次同步，等待 KiCad 响应并处理对话框，不连续点击、不强杀有未保存内容的进程。同一进程可能托管 PCB 和原理图，不能以终止进程解决窗口问题。

确认 KiCad 已保存当前页面；原理图同步只改属性。磁盘校验成功但界面仍旧时检查重开步骤是否完成；description_c_code 是内部元数据，不应作为 KiCad 属性制造假待同步。不要清空记录掩盖真实字段差异。

位号跳转需核对实际窗口、搜索结果刷新和版本/语言/缩放环境。缺符号时先按 UUID、引脚和网络比较历史数据，不用历史整页覆盖当前其他修改。

位号文本由目标控件直接写入并完整回读验证，避免 SendKeys 受输入法或焦点变化影响；C103 等丢字符时停止跳转并记录 expected/current，不继续点击不确定的结果。真实 KiCad wx 控件仍需在实际版本中核验。

CLI 的 `--bulk-sync-no-notify --bulk-sync-no-prepare` 不是只读模式：没有 `--bulk-sync-dry-run` 仍会写原理图。仅评估可使用 `python -B tools/part_selection_server.py --config "E:/work/Board/BOM/part_selection_config.json" --bulk-sync-bom __schematic__ --bulk-sync-dry-run --bulk-sync-no-notify --bulk-sync-no-prepare`，仅对已保存数据计算差异。

## 转换失败

轻量 ZIP 不含转换器，可使用完整版或设置 converter_executable / PART_SELECTION_CONVERTER。轻量直接更新保留已有 third_party 转换器。检查错误中的外部日志绝对路径，区分网络/CAD 数据不足、180 秒超时、本地权限和库冲突。

预览过期、原文件/行版本变化或焊盘编号不符时重新核查，不删库绕过保护。“库已保存但记录未保存”是分阶段保存故障，先处理文件占用/权限，再重新预览复用。配置代理不保证转换成功，插件不修改系统代理；详见 [转换说明](easykiconverter.md)。

本页不声明真实 UI、网络或转换器冒烟已执行；验证范围见 [测试说明](../../tests/README.md)。
