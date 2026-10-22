# KiCad 插件安装与独立窗口

安装包包含本地库存模块，复用现有 openpyxl 依赖。使用“数据 → 导入本地库存…”导入当前结余，工具栏套数与列表本地库存列核对多套需求；库存属于工程数据，不随插件升级/卸载删除。重新安装后，旧工作台使用“文件 → 重启工作台”加载新代码。详见[本地库存与套数](part-selection.md#本地库存与套数)。

[工具首页](../README.md) | [使用与配置](part-selection.md) | [转换器](easykiconverter.md) | [故障排查](troubleshooting.md)

## 从仓库链接手动安装

适用于只有仓库链接、没有安装包的 Windows 用户。以下目录是示例，源码应放在 KiCad 工程之外。

1. 安装 Windows 版 KiCad 10、64 位 Python 3.10 或更高版本，以及 Microsoft Edge WebView2 Runtime。Python 安装时勾选“Add python.exe to PATH”。打开 PowerShell，运行 `python --version` 确认版本；若只有 `py` 命令可用，下面的 `python` 替换为 `py -3`。KiCad IPC 入口还需在首选项中启用 IPC API。
2. 获取源码，下面两种方法任选一种。

**不用 Git：**打开 https://github.com/cjucong/kicad-component-selection-workbench ，点击 **Code → Download ZIP**，完整解压到例如 `D:\KiCadTools\kicad-main`。不要直接在 ZIP 内运行，也不要只下载 `tools` 中的单个文件。在解压目录空白处打开 PowerShell，确认 `Get-ChildItem` 能看到 `README.md`、`tools`、`tests`。

**使用 Git：**先安装 Git for Windows，再在 PowerShell 执行：

```powershell
git clone https://github.com/cjucong/kicad-component-selection-workbench.git D:\KiCadTools\kicad-part-selection
Set-Location D:\KiCadTools\kicad-part-selection
```

3. 在仓库根目录执行安装命令。打包器使用 Python 标准库，会生成 ZIP 并安装到当前用户 KiCad 10 插件目录：

```powershell
python -B tools/kicad_plugin/package_plugin.py --install
```

默认安装位置是 Windows 实际“文档”目录下的 `KiCad\10.0\plugins\com_smu_part-selection`，文档重定向到 OneDrive 等位置时随实际目录解析。终端最后打印的路径为准。**安装进去的是生成的插件，不要把整个 Git 仓库手动复制进 plugins。**

4. 关闭并重新打开 KiCad；从工程管理器打开已保存的 `.kicad_pro`，进入原理图或 PCB 编辑器，点击“器件选料助手”。直接安装不会出现在 PCM 已安装列表中。IPC 基础依赖由 KiCad 管理；首次启动桌面窗口可能下载独立运行环境，需要网络且耗时较长。若入口未出现或依赖下载失败，先看下文“启动与工程隔离”和 [故障排查](troubleshooting.md)，不要重复安装两份副本。
5. 若要手动安装依赖并独立打开助手，在同一仓库根目录执行：

```powershell
python -m pip install -r tools/kicad_plugin/desktop-requirements.txt
python -B tools/kicad_plugin/desktop.py --project "E:/work/Board/Board.kicad_pro"
```

把示例工程路径换成自己的真实工程。没有插件入口也可用这条命令，或双击 `tools/启动器件选料助手.bat` 选择工程。首次进入后用顶部“设置 → 工程设置…”核对数据位置；正式数据默认保存在自己的工程 `BOM/` 下，而不是此源码目录。

### 转换器是否需要另装

以上命令安装的是**轻量版**：搜索、选料、数据导出、现有库封装选择等可以使用，但不会从 GitHub 源码自动下载转换器。需要“替换立创封装”时，另行获取 [EasyKiConverter v3.1.11](https://github.com/EasyKiconverter/EasyKiConverter/releases/tag/v3.1.11) 的 Windows 运行包，完整解压并保留 DLL 等依赖；在顶部“设置 → 工程设置…”选择其中的 `easykiconverter.exe`，保存后使用。完整分发包的构建、版本校验及许可材料见 [转换器说明](easykiconverter.md)。不要把外部转换器安装目录误选为工程数据输出目录。

## 在线更新

新版安装一次后，在“设置 → 插件更新…”选择更新方式：

- **手动检查**：只在点击“检查更新”时访问仓库。
- **启动时检查并提醒（默认）**：发现正式版本后菜单显示“有新版本”，由你选择“下载更新”，包校验完成后再确认“重启并更新”，不强制立刻退出。
- **自动下载，退出工作台后更新**：下次启动自动下载并校验正式包；不打断工作，正常关闭时保存行编辑、停止所属后台，窗口退出后安装。再次打开使用新版，也可点“重启并更新”。

更新面板显示当前/目标版本、当前会话上次成功检查时间和可折叠的更新内容。检查及下载/校验期间使用不确定进度条，不伪造百分比；关闭面板不取消任务，也不安装，重新打开可继续查看状态。已下载并校验的包直接进入重启确认，不再次下载；取消确认继续使用当前版本。工程设置尚未保存时先提示处理，行编辑仍通过统一退出流程保存，保存失败不关闭窗口。更新方式保存失败会恢复原选项，避免看似保存成功。

更新首选项按本机插件安装位置保存，不放进具体工程。下载、暂存与安装备份位于 `%LOCALAPPDATA%/KiCadPartSelection/plugin-updates/<安装路径哈希>/`。更新仅替换已安装的插件，不修改源码仓库、原理图、PCB、选料记录、配置、工程库或既有转换器；原工程和配置参数在重启时保留。多工程共用安装时，请先关闭其他工作台再安装，不结束 KiCad。下载或校验失败保留当前窗口和安装；文件发布失败恢复旧目录并保留完整备份。已安装成功但启动失败时，可退出工作台后用备份目录或上一版本包手动恢复。

只接收本仓库 GitHub Releases 的正式版本，不跟随每次 main 提交，不下载预发布或降级版本。优先读取正式发布资产 `workbench-update.json`，不依赖 GitHub REST API 的匿名额度；旧版本没有清单时才兼容查询 API。成功检查结果在工程外缓存 6 小时，手动成功检查缓存 1 分钟。403/429 后按服务端 Retry-After / X-RateLimit-Reset 提示设置等待（本实现防御性限制为 1 分钟至 1 小时），保存到同一工程外更新目录的 rate-limit.json；显示可重试时间，期间手动点击或重开仍不联网。等待不阻止更改更新方式，也不丢弃已经校验的包。无需配置 GitHub Token，也不读取用户凭据。

发布 ZIP 名称必须为 `kicad-component-selection-workbench-v<版本号>.zip`，发布清单或旧版 API 提供的 SHA-256、ZIP 内容和 build-info 版本须一致；校验缺失、路径越界或尝试覆盖转换器/工程文件时拒绝安装。网络或代理不可达会提示重试，不改系统代理。若仍显示 GitHub 拒绝访问，先确认 GitHub 发布页是否能正常打开，不要连续重试；也可按下节手动安装。源码窗口可查看版本，但须用 Git 更新后运行安装器。首次安装本更新功能仍需手动更新一次。

维护者修改 `tools/kicad_plugin/updater.py` 的 `VERSION`，测试通过、提交并推送后创建对应 `vX.Y.Z` 标签；`.github/workflows/release.yml` 在 Windows 运行回归，生成同名 ZIP 和更新清单，上传草稿后一起公开发布。包含第三方转换器的完整分发包仍按转换器文档准备，本在线代码更新包不重复分发或升级转换器。

“没有更高的正式发布版本”仅表示已安装版本与 GitHub 正式发布版本的比较结果，不代表源码 main 没有新提交。维护者交付供其他电脑更新的功能时，须递增版本、推送对应标签，并确认 Actions 成功且 `releases/latest/download/workbench-update.json` 已指向新版本；只推源码或本机重装不算完成发布。禁止用不同内容覆盖同一个正式版本号。更新面板显示当前版本、最新正式版本和上次成功检查时间；同事收不到更新时，先核对这三项。新版本发布后，手动检查需距上次成功检查至少一分钟，再下载并重启更新。

### 手动更新和卸载

Git 安装：关闭助手与对应后台、退出 KiCad，进入源码目录运行 `git pull --ff-only`（本地有改动先保留处理），然后重新运行 `python -B tools/kicad_plugin/package_plugin.py --install`。Download ZIP 安装：将新 ZIP 解压到新的源码目录，在其中运行同一安装命令。安装器备份旧插件，轻量更新保留已安装的转换器，随后重新打开 KiCad。

源码目录与安装目录是两份副本，普通保存源码不会自动更新插件；此仓库开发维护流程要求每轮验证后重新安装。**卸载时只移除实际安装的 `com_smu_part-selection` 插件目录，不删除工程的 BOM、Libraries、配置或选料记录。** 正式库仍可能被设计使用；PCM 安装的副本通过 PCM 卸载，不要同时保留两种安装来源。

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

使用既有配置：

```powershell
python -B tools/kicad_plugin/desktop.py --config "E:/work/Board/BOM/选料工作台/工作台配置.json"
```

中文入口 tools/启动器件选料助手.bat 转发 --project/--config，无参数弹工程选择器。--project 为绝对工程目录或文件；--config 解析 project_root，两者同时提供必须一致。私有桌面环境重新启动时保留 --config。只支持独立工作台，不提供浏览器模式或依赖失败后的浏览器回退；外部商城快捷页面不受影响。

隐藏启动桥由 start_part_selection_tool.vbs 和 launch_part_selection.py 提供，完整保留上述参数；未指定目标时选择工程。共享 part_selection_paths.py 随安装包提供，不依赖工程 tools。

每个工程保持自己的配置、人工记录、库存和库文件；插件升级不替换这些内容。服务仅监听本机，核验程序、工程、配置及本次宿主身份，8765 被其他工程占用时选择工程隔离端口，不终止他人的服务。顶部“设置 → 工程设置…”支持输入或浏览选择路径，数据路径保存后立即应用；运行环境路径修改后使用“文件 → 重启工作台”。直接手改 JSON 前先关闭工作台并备份，详见 [配置说明](part-selection.md)。

## 关闭与重启

- 右上角叉号与“文件 → 退出工作台”采用同一流程：等待行内输入保存成功，再退出本窗口所属的工程后台；不关闭 KiCad。
- “文件 → 重启工作台”在原后台端口释放后，按相同工程和配置重新启动独立窗口；列布局及已保存记录保留。仅刷新界面不会重启后台。
- 搜索、转换、同步或其他数据请求未完成时提示等待；输入法组字未完成时暂不退出。未保存的路径配置先询问是否放弃。保存或退出失败时保留窗口和编辑内容，可处理后重试，不强杀任务。
- 宿主意外退出时，后台监视宿主进程，等待请求和任务结束后自行退出，不会强制中断写入。

升级前遗留的旧后台不支持上述接口，新版不会静默复用，也不会再为同工程创建另一个写入服务。首次升级如遇旧后台提示，按 [旧版后台处理](troubleshooting.md#旧版后台仍在运行) 手动退出已核实的旧服务一次；之后正常关闭即可退出后台。

## 窗口与日志

桌面基目录默认 `%LOCALAPPDATA%/KiCadPartSelection/`，可通过绝对、工程外的 KICAD_PART_SELECTION_HOME 修改；desktop.log 为入口日志，pcb-launch.log 为 PCB 兼容入口日志。工程哈希目录保存 server.log、window.log、runtime-install.log、desktop-runtime 和 webview 数据。

临时转换、预览、下载与备份使用 runtime_directory(project_root)，默认工程外同盘缓存；KICAD_PART_SELECTION_CACHE 可指定绝对、同盘、工程外父目录，程序追加工程哈希子目录。正式记录路径依配置，工程库仍是设计依赖。菜单保存按保存前确认复制已有正式数据；直接手改 JSON 不自动迁移，运行环境不自动搬迁。

窗口支持选择文字、原生复制粘贴和右键菜单；只读结果不变为可编辑字段。列布局保存在独立窗口 localStorage，重启保留；旧浏览器布局不自动迁入。宿主设置升级后重启工作台，插件注册变化后重启 KiCad。

## KiCad 入口及同步限制

保留 PCB/原理图 IPC 入口和 PCB ActionPlugin 兼容入口。兼容入口需要 KiCad 嵌入式 pcbnew；原 IPC 入口不依赖 pcbnew。共享 IPC 工具栏注册表异常时，可检查兼容入口与日志。

原理图窗口必须核验绝对工程路径及 PID/HWND；不能凭同名页面认定工程。没有可复用编辑器时，仅通过匹配工程的 KiCad 工程管理器打开；无法确认则停止，不回退到独立 eeschema 或其他工程。同步遇到忙碌或未保存改动时遵从提示处理，不能绕过保护。

原理图同步沿用 Windows 窗口自动化及属性写入机制，不替换符号几何/引脚，不修改 PCB，不等于实时同步。不同机器、KiCad 版本、语言或缩放的实际交互需分别验证。

## 验证

回归命令为 `python -B -m pytest tests -p no:cacheprovider`。真实桌面冒烟入口和依赖见 [测试说明](../../tests/README.md)，仅在隔离工程运行。打包成功和模拟 loader 通过不证明实际工具栏点击、网络转换或跨机器同步已通过；本说明不声明这些真实冒烟已执行。
