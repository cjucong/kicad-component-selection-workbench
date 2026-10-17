# KiCad 安装版器件选料助手

[工具首页](../README.md) | [使用教程](part-selection.md) | [AI 接手](ai-handoff.md)

## 支持范围

- Windows 10/11、标准 KiCad 10 PCB/原理图编辑器的 IPC 插件按钮；需要在 KiCad 首选项中启用 IPC API，配置可用的 Python 3.10+。
- 独立可缩放 WebView2 窗口，不是原理图侧栏；保留现有网页全部操作。没有 WebView2 时打开系统浏览器并给出错误日志位置。
- 不修改 KiCad 安装目录，不依赖 `pcbnew`。基础依赖由 KiCad 创建的 Python 环境安装；首次打开时另建桌面专用环境安装 WebView2 宿主，可能需要几分钟和网络。
- 原理图保存、关闭、定位仍沿用现有 Windows 自动化。本机定制 `kicad-mcp` 接口不是标准 KiCad 能力，其他机器必须单独验证；插件化不代表已经实现原理图实时同步或符号替换。

## 安装

先用 Python 构建 ZIP（不需要 KiCad/Python 第三方依赖）：

```powershell
python -B tools/kicad_plugin/package_plugin.py
```

输出 `Temp/part-selection-0.1.1.zip`。同事打开 KiCad 工程管理器，进入“插件与内容管理器”，选择“从文件安装”，选择该 ZIP 并应用更改。重新打开 PCB/原理图编辑器，等待首次依赖安装完成后，工具栏会出现“器件选料助手”图标。

也可从源码直接安装到当前 Windows 用户的 KiCad 10 插件目录：

```powershell
python -B tools/kicad_plugin/package_plugin.py --install
```

这条命令使用 Windows 实际“文档”目录（支持重定向），写入 `KiCad/10.0/plugins/com.smu.part-selection/`；已有同名安装先备份到工程 `Temp/plugin_install_backups/`。这是本地开发安装，不进入 PCM 的已安装包清单。不要同时用两种方式安装同一个插件；若换用 PCM，先移走本地开发副本，避免重复入口。

插件元数据标为测试版、内部专有使用，不代表工程已授予公开开源许可。安装包只含程序，不包含工程、BOM、库存缓存或选型记录。发布 ZIP 需要另行归档；`Temp/` 不提交 Git，可随时从源码重建。

## 使用

1. 在 KiCad 打开工程及已保存的 PCB/原理图，点击插件按钮。
2. 插件通过启动它的 KiCad IPC 实例获取工程目录，不猜当前命令行目录；原理图 API 没有返回完整路径时仍需工程选择器，请选择 `.kicad_pro`。
3. 保留工程已有 `BOM/part_selection_config.json`。新工程首次使用仅新建该配置，默认显示“原理图当前数据”；有 `DOC/` 时从该目录读取，没有则读根目录。非标准目录请在配置中修改，现有工程仍使用原设置。
4. 选料、库存和同步操作与原网页相同。关闭选料窗口不关闭 KiCad，也不结束后台服务；再次点击会恢复窗口或复用匹配当前工程的服务。
5. 每次启动默认显示“原理图当前数据”；顶部搜索支持位号、C 编码、型号、Value 和封装，多词取交集。`Ctrl+K` 定位表格搜索，独立窗口启用 `Ctrl+F` 浏览器查找。芯片“更换型号”默认只搜 Comment，也支持直接输入 C 编码；有明确型号的阻容感仍按 Comment + Value + Footprint 搜索。

位号跳转按实际顶层窗口句柄识别原理图，不只看进程主窗口；KiCad 同一进程同时托管 PCB/原理图时也能复用已有编辑器。缓存记录 PID + HWND 并重新校验，加载提示窗不作为编辑器。工程归属先核对启动参数的绝对路径；参数没有路径时，只接受同一 PID 内工程管理器显示的唯一完整 `.kicad_pro` 路径及匹配的窗口标题，不凭页面同名匹配。无法确认或多工程歧义时停止，不重新启动另一个工程。同步关闭也只针对该原理图窗口，不会误关同进程的 PCB。

同一工程开着多个原理图窗口时，同步仍要求先关闭多余窗口，避免只关闭一部分造成旧数据覆盖。跳转运行日志为 `%TEMP%/kicad_schematic_search.log`，包含实际窗口 PID/HWND 和工程路径校验证据。搜索面板 IPC 不可用时尝试标准 `Ctrl+G`，自定义快捷键的机器需另行核对。

独立仓库优先使用下方 `--project` 命令指定实际工程。只有将工具目录置于目标工程中时，才可双击 `tools/启动器件选料助手.bat` 以所在工程启动；`tools/start_part_selection_tool.bat` 保留原浏览器启动方式。本仓库不发布 FPGA 专项入口。

源码调试：

```powershell
python -m pip install -r tools/kicad_plugin/desktop-requirements.txt
python -B tools/kicad_plugin/desktop.py --project "E:/path/to/project/project.kicad_pro"
python -B tools/kicad_plugin/desktop.py --project "E:/path/to/project/project.kicad_pro" --browser
```

## 数据、端口和日志

- 程序在用户插件目录；选型覆盖、库存和同步日志仍在各工程 `BOM/`。升级插件不迁移、不覆盖这些数据。
- 服务仅监听 `127.0.0.1`。优先复用 8765，但必须同时匹配工程和配置路径；被其他项目占用时使用项目稳定端口，不会终止别人的服务。
- WebView2 浏览器数据按工程单独存于 `%LOCALAPPDATA%/KiCadPartSelection/<工程哈希>/webview/`。首次从普通浏览器切换时，两者 localStorage 不共享，需在新窗口重新设置列布局。
- KiCad 管理的 `requirements.txt` 仅含 wheel 依赖；`pywebview` 的源代码依赖在上述工程哈希目录的 `desktop-runtime/` 单独安装，不修改 KiCad Python。安装日志是 `runtime-install.log`，窗口标准输出为 `window.log`。缺少可用 venv/pip 或安装失败时会提示并回退浏览器。
- 桌面启动日志：`%LOCALAPPDATA%/KiCadPartSelection/desktop.log`；后台日志：同目录 `<工程哈希>/server.log`。现有单次同步的 `attempt_id` 日志仍按原配置写入工程。
- 故障排查：先看 KiCad 状态栏的插件警告和上述日志。检查 IPC 是否启用、Python 依赖安装是否完成、WebView2 Runtime 是否可用。离线机器需提前准备依赖，不把安装按钮出现当成所有业务已实测通过。

## 开发验证

`python -B -m unittest discover -s tests`。Windows 可额外运行真实窗口冒烟测试：

```powershell
python -m pip install -r tools/kicad_plugin/desktop-requirements.txt jsonschema
python -B tests/run_kicad_plugin_smoke.py
```

测试联网校验官方 `plugin.json`/PCM schema，将安装包解压到 `Temp/kicad_plugin_qa/installed/`，使用临时工程启动 API 并实际创建 WebView2 窗口，读取页面标题和 BOM 选项。测试自动关闭自己的窗口和后台，不关闭已有服务；结果在 `Temp/kicad_plugin_qa/result.json`。正式原理图/PCB 不用于修改测试。

当前已验证安装包、API、窗口加载和后台复用；尚未在本机重新打开 PCB 编辑器后手动点击工具栏按钮，也未对正式工程执行原理图同步。跨电脑仍需验证现有原理图自动化依赖。
