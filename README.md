# KiCad Component Selection Workbench

独立于工程的 KiCad 10 选料插件，提供本地工作台、立创候选与库存查询、主料/备选料/订货记录、封装对比和原理图字段同步。程序可服务不同工程；CAD、BOM、人工记录和工程库由各工程保管。

## 安装与启动

列表列宽可在“列表视图 → 列设置 → 自动调整列宽”中重新适配，列设置会自动记忆。

第一次拿到仓库链接，按 [从 GitHub 下载到手动安装](tools/docs/kicad-plugin.md#从仓库链接手动安装) 操作；不需要原作者的工程、BOM 或 tools 副本。支持 Git 克隆和网页 Download ZIP 两种获取方式。

支持 Windows 10/11、Python 3.10+；独立窗口使用 WebView2 Runtime。网络查询需要联网，外部商城快捷页面使用 Edge 或 Chrome。KiCad 插件入口需要启用 IPC API。安装包不含工程文件，也不依赖 FPGA 或 PCB 3D 工具。

将本仓库单独检出到工程之外，例如与 `SMU_DL_v2/` 并列。从本仓库根目录执行：

```powershell
python -m pip install -r tools/kicad_plugin/desktop-requirements.txt
python -B tools/kicad_plugin/desktop.py --project "E:/path/to/project/project.kicad_pro"
```

也可双击 `tools/启动器件选料助手.bat` 选择工程，或向它传入 `--project` / `--config`；无参数时不绑定任何固定工程。已有配置可直接使用：

```powershell
python -B tools/kicad_plugin/desktop.py --config "E:/path/to/project/BOM/part_selection_config.json"
```

仅保留独立工作台，不再提供浏览器模式或启动失败后的浏览器回退。关闭窗口会先保存行内编辑，再退出本工程后台；也可使用“文件 → 重启工作台 / 退出工作台”。搜索、转换或同步未完成时会提示等待，退出失败时保留窗口及编辑内容。

安装到 KiCad 工具栏或构建 ZIP，见 [插件安装](tools/docs/kicad-plugin.md)。轻量安装包不内置转换器，完整版准备方法见 [转换器说明](tools/docs/easykiconverter.md)。

通过“设置 → 插件更新…”检查 GitHub 正式发布版本、选择“更新并重启”，或启用“自动下载，退出工作台后更新”。默认仅启动时检查提醒，不自动安装；也可关闭自动检查。更新校验下载包 SHA-256、版本及插件标识，保留转换器与工程数据，目录切换失败恢复旧安装。源码运行使用 Git 更新，不通过菜单覆盖。[更新说明](tools/docs/kicad-plugin.md#在线更新)

更新检查优先读取 GitHub 正式发布下载清单，不依赖 REST API 匿名额度，无需 Token；启动检查缓存 6 小时，手动检查间隔至少 1 分钟。

## 工程数据与路径

“设置 → 工程设置… → 原理图跳转”可选择跳转后鼠标“返回原位”（默认）或“留在原理图画布”，按工程保存并立即生效。位号跳转保留 KiCad 当前窗口大小与最大化状态，仅在最小化时还原。

确认料状态就地更新，不再重新加载整表；Comment、Value、Footprint 自动保存后立即显示未同步的旧值 → 新值，与按 Enter 的反馈一致，改回原理图已保存值时清除对应差异。Enter 仅在分组确实变化时重新合行，并保留所在行的屏幕位置。

同步提示合并同一行的重复变化，不再重复列位号和 C 编码链接。Footprint 输入框只显示封装名称，共用库名（如 DSMU）放在列表头；存在多个库时各行单独标出。保存与同步仍使用完整的 `库名:封装名`，输入完整标识可切换该行库，空白保持为空；不会因改其他字段而丢失库名。

保留已有 `BOM/part_selection_config.json`，无需把工程复制到插件仓库。命令显式指定工程或配置；同时指定时两者解析出的工程根目录必须一致。后端不从插件源码父目录猜测工程。

通过顶部“设置 → 工程设置…”选择原理图数据、导入 BOM、工程选料数据目录和转换器；具体文件与运行路径在折叠的“高级路径设置”中。每次启动默认读取已保存的原理图，导入 BOM 仅本次生效。选料记录、导出和同步日志默认在当前工程 `BOM/`，修改目录时保存前确认复制，原文件保留，目标冲突不覆盖；已有自定义文件位置不会因无关设置变动而重置。运行目录修改后可直接点“重启工作台”。[设置及迁移步骤](tools/docs/part-selection.md)

临时转换、预览和备份位于工程外同盘缓存；已注册的符号/封装库仍是工程设计依赖，不能当作插件缓存删除。目标库选择已在封装对比界面提供。

## 文档与开发

- [工具索引](tools/README.md)
- [使用与配置](tools/docs/part-selection.md)
- [开发接手](tools/docs/ai-handoff.md)
- [故障排查](tools/docs/troubleshooting.md)
- [测试说明](tests/README.md)
- [协作规则](AGENTS.md)

本仓库仅包含插件源码、资源、打包入口和测试，不携带 CAD、生产 BOM、工程专用规则或 Git 数据快照。测试通过不代表实际 KiCad 按钮、跨机器窗口自动化或网络转换已验证；真实冒烟需另行在隔离工程执行并记录结果。

仓库地址：https://github.com/cjucong/kicad-component-selection-workbench.git 。源码每轮修改后按协作规则备份、测试、提交推送并更新本机安装；源码文件的普通保存不会直接改动运行中的安装副本。

未指定开源许可证；公开托管不等于授予 MIT 等开源授权，后续许可由仓库所有者决定。
