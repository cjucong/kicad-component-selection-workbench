# KiCad 器件选料助手

独立于工程的 KiCad 10 选料插件，提供本地工作台、立创候选与库存查询、主料/备选料/订货记录、封装对比和原理图字段同步。程序可服务不同工程；CAD、BOM、人工记录和工程库由各工程保管。

## 安装与启动

支持 Windows 10/11、Python 3.10+；独立窗口使用 WebView2 Runtime。网络查询需要联网，网页回退需要 Edge 或 Chrome。KiCad 插件入口需要启用 IPC API。安装包不含工程文件，也不依赖 FPGA 或 PCB 3D 工具。

将本仓库单独检出到工程之外，例如与 `SMU_DL_v2/` 并列。从本仓库根目录执行：

```powershell
python -m pip install -r tools/kicad_plugin/desktop-requirements.txt
python -B tools/kicad_plugin/desktop.py --project "E:/path/to/project/project.kicad_pro"
```

也可双击 `tools/启动器件选料助手.bat` 选择工程，或向它传入 `--project` / `--config`；无参数时不绑定任何固定工程。已有配置可直接使用：

```powershell
python -B tools/kicad_plugin/desktop.py --config "E:/path/to/project/BOM/part_selection_config.json"
```

浏览器模式：

```powershell
python -m pip install -r tools/requirements.txt
python -B tools/part_selection_server.py --project "E:/path/to/project/project.kicad_pro" --open
```

安装到 KiCad 工具栏或构建 ZIP，见 [插件安装](tools/docs/kicad-plugin.md)。轻量安装包不内置转换器，完整版准备方法见 [转换器说明](tools/docs/easykiconverter.md)。

## 工程数据与路径

保留已有 `BOM/part_selection_config.json`，无需把工程复制到插件仓库。命令显式指定工程或配置；同时指定时两者解析出的工程根目录必须一致。后端不从插件源码父目录猜测工程。

通过“文件 → 路径配置文件…”可用记事本打开当前 JSON，调整路径、记录和缓存位置；这不是 UI 路径编辑器，修改后必须重启对应后端。**改输出路径不会迁移旧记录**：先备份并复制已有 overrides、库存缓存及所需日志，再切换配置并核对，未经确认不得以空文件替代旧记录。[路径配置及迁移步骤](tools/docs/part-selection.md)

临时转换、预览和备份位于工程外同盘缓存；已注册的符号/封装库仍是工程设计依赖，不能当作插件缓存删除。目标库选择已在封装对比界面提供。

## 文档与开发

- [工具索引](tools/README.md)
- [使用与配置](tools/docs/part-selection.md)
- [开发接手](tools/docs/ai-handoff.md)
- [故障排查](tools/docs/troubleshooting.md)
- [测试说明](tests/README.md)
- [协作规则](AGENTS.md)

本仓库仅包含插件源码、资源、打包入口和测试，不携带 CAD、生产 BOM、工程专用规则或 Git 数据快照。测试通过不代表实际 KiCad 按钮、跨机器窗口自动化或网络转换已验证；真实冒烟需另行在隔离工程执行并记录结果。

仓库地址：https://github.com/cjucong/kicad-.git 。源码每轮修改后按协作规则备份、测试、提交推送并更新本机安装；源码文件的普通保存不会直接改动运行中的安装副本。

未指定开源许可证；公开托管不等于授予 MIT 等开源授权，后续许可由仓库所有者决定。
