# 开发与 AI 协作约束

- 本仓库是独立工具，不包含真实 KiCad 电路。不要从原工程复制 BOM、缓存、日志、原理图、PCB、库或 Git 历史。
- 修改前检查 `git status`，有已有改动时用 `git stash create` + `git stash store` 做非破坏式备份，不清空工作区。
- 不覆盖他人改动，不使用 `git reset --hard`。只提交当前任务的文件；源码修改同步更新 `CHANGELOG.md`。
- 所有临时文件、打包 ZIP 与测试输出放在 `Temp/`，不得提交。
- 对目标 KiCad 工程的写入必须显式确定工程目录，先备份并处理编辑器占用。字段同步不允许删除或重建符号。
- 禁止添加原工程专用位号映射作为全局默认。网页状态必须复用 `PartSelectionApp.rows()` 合并逻辑。
- Windows 后台子进程保持隐藏；不得按端口号直接结束未经核实的服务。
- 不提交密钥、账号、cookie、浏览器配置或抓取的真实交易信息。不擅自添加开源许可证。
- 功能变更先补回归测试，执行 `python -B -m unittest discover -s tests`。没有实测的 KiCad UI 流程需明确说明，不把单测通过描述为硬件工程同步验证。
