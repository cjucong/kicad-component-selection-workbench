# 独立插件仓库协作规则

本仓库维护 KiCad 器件选料助手，不维护具体工程的原理图、PCB、BOM 或 FPGA。当前用户指令优先于本文件。

## 修改与备份

- 修改前检查工作区，使用带时间戳说明的非破坏式 `git stash create` + `git stash store` 备份已跟踪的工作区变化，不清空工作区；干净仓库无 stash 对象时记录当前提交。未跟踪文件如需改动，先另行备份。
- 保留其他协作者和用户的改动，不还原、不清理不属于本任务的文件。
- 未经要求不推送；是否提交遵从当前用户指令，不把生产记录夹带进代码提交。
- 不修改具体工程的 CAD、BOM、库或配置来让插件测试通过；生产工程不得作为写入、同步、转换或安装测试对象。

## 独立边界与路径

- 插件不得导入工程专用 FPGA/3D 工具；KiCad S-expression 解析使用 `tools/kicad_sexpr.py`。
- 服务必须显式接收 `--project` 或 `--config`，不得从源码目录、当前工作目录或历史工程猜测目标。两参数同时提供时必须验证根目录一致，桌面私有环境重启也必须传递配置。
- 保持既有配置和记录兼容，包括 `source_bom=__schematic__` 的 Path 表示。配置相对路径的基准见 [使用说明](tools/docs/part-selection.md)。
- 改记录路径不等于迁移数据；先备份、复制和核对，不得未经同意新建空记录替代原记录。
- 正式导入的符号/封装及库表属于目标工程的设计依赖；不随插件卸载或缓存清理删除。字段同步不得扩大成符号几何、引脚或 PCB 替换。

## 测试与临时产物

- 临时工程使用 `tests/_scratch.py` 的 `temporary_project()`，pytest 可用 `scratch_project`；结束、断言失败或异常展开时清理工程和派生缓存，unittest 在 setUp 立即注册 addCleanup。
- 临时 ZIP、下载、截图、日志、转换和备份放在仓库及目标工程之外。使用 `runtime_directory(project_root)`，不得将外部缓存用 junction/symlink 链回工程。
- `KICAD_PART_SELECTION_CACHE` 是绝对、工程外同盘的缓存父目录，按工程路径哈希隔离；`KICAD_PART_SELECTION_HOME` 是绝对、工程外的桌面环境与日志基目录。
- 测试夹具不得包含生产 CAD/BOM、人工选料记录、凭据或 Git 数据。不得复制真实工程的 `.git` 进行验证。
- 修改后从仓库根目录运行 `python -B -m pytest tests -p no:cacheprovider`，并设置 `PYTHONDONTWRITEBYTECODE=1`；依赖和专项验证见 [测试说明](tests/README.md)。
- 真实网络、WebView2 和 KiCad 冒烟按需单独运行，不将模拟测试描述为真实 UI 或联网验证。不为修测试自动结束用户的 KiCad 或后台服务。

## 文档

命令以独立仓库为起点，目标工程使用明确路径；不得要求复制整套工程工具，也不得将 Temp 作为默认临时目录。修改公共参数或行为时同步 README、使用教程和开发说明，并明确验证范围。
