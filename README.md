# KiCad 器件选料助手

面向 KiCad 工程的本地 BOM 选料工作台。支持读取原理图与 BOM、查询立创物料、选择主料及备选料、记录订货渠道，并将确认后的器件属性同步回原理图。

本仓库仅包含工具源码、测试和文档，不包含任何实际工程、原理图、PCB、选型记录、库存缓存或原工程 Git 历史。

## 快速开始

要求 Windows 10/11、Python 3.10+。插件入口面向 KiCad 10，独立窗口需要 Microsoft Edge WebView2 Runtime。商城页面解析需要 Chrome 或 Edge 及网络。

```powershell
git clone https://github.com/cjucong/kicad-.git
cd kicad-
python -m pip install -r tools/kicad_plugin/desktop-requirements.txt
python -B tools/kicad_plugin/desktop.py --project "C:/KiCadProjects/MyBoard/MyBoard.kicad_pro"
```

把示例路径替换为自己的已保存工程。程序只在目标工程首次使用时生成 `BOM/part_selection_config.json`；已有配置不会被覆盖。默认读取原理图当前数据，分页位于 `DOC/` 时自动选择该目录，否则读取根目录。其他布局需手动调整配置。

普通浏览器模式可加 `--browser`。也可安装基础依赖后指定目标工程配置启动服务：

```powershell
python -m pip install -r tools/requirements.txt
python -B tools/part_selection_server.py --config "C:/KiCadProjects/MyBoard/BOM/part_selection_config.json" --host 127.0.0.1 --port 8765 --open
```

配置格式见 [示例](examples/part_selection_config.example.json)。不要直接双击仓库中的 `.bat` 来识别另一个目录的工程；兼容启动器按所在工程目录运行，独立仓库优先使用上面的显式 `--project` 命令。

## 安装到 KiCad

```powershell
python -B tools/kicad_plugin/package_plugin.py
```

在 KiCad“插件与内容管理器”中从文件安装 `Temp/part-selection-0.1.1.zip`。安装包按白名单打包工具，不携带工程数据。开发安装可加 `--install`，详见 [安装说明](tools/docs/kicad-plugin.md)。

## 已有功能

- 原理图当前数据及 BOM CSV 合并、字段编辑、确认料与筛选。
- 立创 C 编码及型号搜索、库存缓存、主料及最多 5 个备选料、订货链接。
- 列宽、列顺序与显示配置，候选窗口移动、缩放和最小化。
- 单行及全表原理图属性同步、字段差异、进度与审计日志。
- 采购 BOM、改动表及静态复核表导出。

## 边界与已知问题

- 当前同步只更新属性，不替换符号几何、引脚或连线；商城封装名称不能保证是有效 KiCad 封装库标识。
- 原理图自动保存、关闭、重开与定位依赖 Windows 自动化；部分流程还使用定制 `kicad-mcp`，不是标准 KiCad 的通用保证。跨电脑必须先在测试工程验证。
- PCB 插件入口点击无反应及首次结构化搜索偏慢仍在排查，不能视为本次发布已修复。
- “勾选多行后仅同步所选行”尚未实现，本版仍是单行或全表同步。
- 不携带原工程专用的六组位号重映射；批量同步默认按当前位号写入。
- 库存、价格与订货交期来自第三方页面，可能过期或受反爬限制，采购前需到商城核实。
- 首次同步请备份工程并先使用 dry-run。服务仅监听本机，不适合直接暴露到公网。

## 文档与开发

- [使用教程](tools/docs/part-selection.md)
- [KiCad 插件安装](tools/docs/kicad-plugin.md)
- [AI 与开发者接手说明](tools/docs/ai-handoff.md)
- [故障排查](tools/docs/troubleshooting.md)
- [发布记录](CHANGELOG.md)

```powershell
python -B -m unittest discover -s tests
```

测试创建合成工程，不需要也不会随仓库分发真实电路。`tools/fpga_pin_assignment.py` 暂时作为原理图 S-expression 解析器依赖保留，不提供 FPGA 专项功能入口。

未指定开源许可证；本次公开托管不等于授予 MIT 等开源授权。后续许可由仓库所有者决定。
