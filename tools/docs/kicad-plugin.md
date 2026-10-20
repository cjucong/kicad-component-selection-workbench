# KiCad 插件安装与独立窗口

[工具首页](../README.md) | [使用与配置](part-selection.md) | [转换器](easykiconverter.md) | [故障排查](troubleshooting.md)

## 环境与安装

Windows 10/11、KiCad 10、Python 3.10+，独立窗口使用 WebView2 Runtime。KiCad IPC 入口需要在首选项启用 IPC API；基础依赖由 KiCad 管理，WebView2 宿主依赖使用独立桌面环境，首次安装可能需要网络。

从独立插件仓库根目录构建轻量 ZIP：

```powershell
python -B tools/kicad_plugin/package_plugin.py
```

命令打印外部缓存中的 ZIP 路径。在 KiCad 工程管理器的“插件与内容管理器”选择“从文件安装”，应用后重启相关编辑器。安装包只含程序与资源，使用独立解析器，不携带 FPGA/PCB 3D 工具或工程数据。

也可直接开发安装：

```powershell
python -B tools/kicad_plugin/package_plugin.py --install
```

默认解析 Windows 实际“文档”目录（支持重定向），安装到 `KiCad/10.0/plugins/com_smu_part-selection/`。自定义插件父目录：

```powershell
python -B tools/kicad_plugin/package_plugin.py --install --plugin-dir "E:/KiCadUser/10.0/plugins"
```

--plugin-dir 指定父目录，由安装器创建插件子目录。更新前关闭插件窗口和 KiCad，旧目录先备份到外部缓存的 plugin_install_backups，失败恢复。旧 dotted 名目录按安装器迁移规则处理。轻量直接安装会保留已安装的 third_party 转换器；轻量 ZIP 本身仍不含转换器。完整版构建见 [转换说明](easykiconverter.md)。

直接安装不进入 PCM 已安装列表，不要同时保留 PCM 与开发安装副本。元数据标记测试版及现有许可，不意味着工程或插件新增公开开源授权。

## 启动与工程隔离

在 KiCad 打开已保存工程，再点击插件按钮。入口优先识别调用它的 IPC 工程；信息不完整或有歧义时要求选择 .kicad_pro，不从插件目录猜测。

独立源码启动：

```powershell
python -m pip install -r tools/kicad_plugin/desktop-requirements.txt
python -B tools/kicad_plugin/desktop.py --project "E:/work/Board/Board.kicad_pro"
```

使用既有配置或浏览器模式：

```powershell
python -B tools/kicad_plugin/desktop.py --config "E:/work/Board/BOM/part_selection_config.json"
python -B tools/kicad_plugin/desktop.py --config "E:/work/Board/BOM/part_selection_config.json" --browser
```

上述两条择一。中文入口 tools/启动器件选料助手.bat 转发 --project/--config，无参数弹工程选择器。--project 为绝对工程目录或文件；--config 解析 project_root，两者同时提供必须一致。私有桌面环境重新启动时保留 --config。

隐藏启动桥由 start_part_selection_tool.vbs 和 launch_part_selection.py 提供，完整保留上述参数；未指定目标时选择工程。共享 part_selection_paths.py 随安装包提供，不依赖工程 tools。

每个工程保持自己的配置、人工记录、库存和库文件；插件升级不替换这些内容。服务仅监听本机，复用时核验工程与配置身份，8765 被其他工程占用时选择工程隔离端口，不终止他人的服务。“文件 → 路径配置文件…”可用记事本打开当前 JSON，不提供 UI 路径编辑或实时应用。关闭窗口不等于关闭后端；修改 JSON 后应停止并重启对应后端，详见 [配置说明](part-selection.md)。

## 窗口与日志

桌面基目录默认 `%LOCALAPPDATA%/KiCadPartSelection/`，可通过绝对、工程外的 KICAD_PART_SELECTION_HOME 修改；desktop.log 为入口日志，pcb-launch.log 为 PCB 兼容入口日志。工程哈希目录保存 server.log、window.log、runtime-install.log、desktop-runtime 和 webview 数据。

临时转换、预览、下载与备份使用 runtime_directory(project_root)，默认工程外同盘缓存；KICAD_PART_SELECTION_CACHE 可指定绝对、同盘、工程外父目录，程序追加工程哈希子目录。正式记录路径依配置，工程库仍是设计依赖。更改任何路径不自动迁移已有数据。

窗口支持选择文字、原生复制粘贴和右键菜单；只读结果不变为可编辑字段。浏览器与独立窗口的 localStorage 不共享。宿主设置升级后重开助手窗口，插件注册变化后重启 KiCad。

## KiCad 入口及同步限制

保留 PCB/原理图 IPC 入口和 PCB ActionPlugin 兼容入口。兼容入口需要 KiCad 嵌入式 pcbnew；原 IPC 入口不依赖 pcbnew。共享 IPC 工具栏注册表异常时，可检查兼容入口与日志。

原理图窗口必须核验绝对工程路径及 PID/HWND；不能凭同名页面认定工程。没有可复用编辑器时，仅通过匹配工程的 KiCad 工程管理器打开；无法确认则停止，不回退到独立 eeschema 或其他工程。同步遇到忙碌或未保存改动时遵从提示处理，不能绕过保护。

原理图同步沿用 Windows 窗口自动化及属性写入机制，不替换符号几何/引脚，不修改 PCB，不等于实时同步。不同机器、KiCad 版本、语言或缩放的实际交互需分别验证。

## 验证

回归命令为 `python -B -m pytest tests -p no:cacheprovider`。真实桌面冒烟入口和依赖见 [测试说明](../../tests/README.md)，仅在隔离工程运行。打包成功和模拟 loader 通过不证明实际工具栏点击、网络转换或跨机器同步已通过；本说明不声明这些真实冒烟已执行。
