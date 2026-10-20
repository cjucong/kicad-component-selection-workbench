# 立创符号与封装转换

## 选料时如何使用

在“更换型号”窗口搜索框后的“封装”下拉框选择：

- **不替换**（默认）：更新 C 编码及物料参数，不把商城的 `0805`、`BGA-64` 等标签写入 Footprint；每个位号保留自身当前/待保存封装。Value、Comment 由另外两个下拉选项独立控制，默认都不替换。
- **替换库封装**：点击“选用主料”后打开工程库列表，默认选择当前封装；可按库和名称筛选。只读取焊盘编号和尺寸，不下载、不转换、不自动生成预览。需要看图时点击“预览”；确认无需等待预览。编号不一致或原封装不可读时禁止替换，编号一致仍须人工确认尺寸和引脚功能。取消不保存此次选料；确认仅记录所选库标识，不修改库文件、不导入符号。
- **替换立创封装**：点击“选用主料”后后台调用 EasyKiConverter，在工程外缓存下隔离转换并用 KiCad 原生渲染真实封装。先比较当前工程库与转换结果，确认后才添加到目标库并保存此次主料；取消或转换失败不修改正式库及选料记录。

### 对比与目标库

预览窗内显示焊盘数量、编号、单焊盘尺寸与焊盘包络（不是本体尺寸）。图形各自适配显示，不是同一比例叠加；点击预览可放大。位号/值文字仅在渲染副本中去除，正式封装不删字段。可选择“使用现有封装”，或确认转换结果后选择：

- **合并到现有库**（默认）：优先工程已注册的 `DSMU`，也可选其他工程内 KiCad `.pretty` 库；不向全局、外部或旧版受管理的 C 编码库写入。
- **新建封装库**：输入库名和工程内相对目录，如 `Libraries/DSMU`，自动注册工程库表。
- **独立转换库**：保存在 `Libraries/DSMU/LCSC/selected/LCSC_Cxxxx_Selected.pretty`。

封装名默认采用安全的商城封装名，例如 `0603`，不适用时保留实际转换名；不把商品标签当成几何正确性的证明。同名同几何复用，同名不同几何追加 `_Cxxxx`，仍冲突时追加摘要；绝不覆盖共享旧封装。库名前缀仍是 KiCad 必需的 `库名:封装名`，例如 `DSMU:0603_C53280145`。

转换结果与该行原有封装的焊盘编号不一致、或无法读取原有工程本地封装时，禁止直接采用转换结果。编号一致也不证明电气兼容，须核对数据手册和引脚功能，保存关闭库编辑器后勾选确认。预览绑定原始候选、位号、选项及文件摘要，30 分钟有效；行记录、原理图封装或库文件变化时要求重新预览。取消保留搜索窗口和结果。

同一搜索栏提供 Value、Comment 两个独立的“替换 / 不替换”选项，每次新开默认不替换；最小化后恢复当前窗口会保留选项。不替换的字段保留当前值，包括已保存但未同步的人工修改。选择替换后按位号类别生成：

| 器件 | 替换 Comment | 替换 Value |
| --- | --- | --- |
| 电阻 R | `resistor` | 阻值，如 `50kΩ` |
| 电容 C | `capacitor` | 电容量，如 `100nF` |
| 电感 L | `inductor` | 电感值，如 `1uH` |
| 芯片及其他器件 | 完整型号 | 完整型号 |

完整型号始终保存在物料参数/Description 的“型号”行。阻容感数值只能从明确参数或可信参数摘要中提取，不猜测型号中的数字；找不到数值时保留原 Value。三个选项（Value、Comment、封装）彼此独立。选料保存只修改网页待同步状态，仍须手动同步原理图。

确认后写入选料覆盖记录的 Footprint 是实际注册的目标标识，例如 `DSMU:0603_C53280145`。仍需点击“同步原理图”或勾选同步，才会写入原理图属性。选择转换封装时符号单独导入 `Libraries/DSMU/LCSC/symbols/`，Footprint 引用同步为最终标识；选择复用现有封装则不导入符号。**不会替换已放置的原理图符号，不会修改 PCB**。请核对后再从原理图更新 PCB。订货链接没有有效 C 编码时不提供转换。

## 工程文件

```text
Libraries/DSMU/DSMU.pretty/封装名_Cxxxx.kicad_mod
Libraries/DSMU/LCSC/symbols/LCSC_Cxxxx_摘要.kicad_sym
sym-lib-table
fp-lib-table
```

库表使用 `${KIPRJMOD}` 相对路径，可随工程复制。只新增封装，不覆盖 DSMU 旧封装；同名异路径注册、导入文件被人工修改或解析失败时停止。临时缓存由 `part_selection_runtime.runtime_directory(project_root)` 定位，默认在工程外同盘的 `../.kicad-part-selection-cache/<工程名>-<路径哈希>/`，避免 KiCad 递归备份，且保证跨目录原子写入不跨磁盘。库表备份和事务清单在其 `part_selection_library_backups/`；转换预览在 `footprint-preview-*/`，最多保留 8 份、后续转换会清理过期项。日志路径以错误提示中的绝对路径为准。正式转换库仍在工程 `Libraries/`，旧脚本直接转换接口保留按 C 编码隔离的旧路径及校验规则。

库文件及库表发布失败会回滚本次已写且未被外部修改的文件；发现并发编辑时保留外部内容并提示备份位置。库发布后若选料记录保存失败，提示“库已保存但记录未保存”；已有安全新增库不会因记录故障被静默删除，可重新预览复用。不要把此类本地保存故障误认为网络转换失败。

当前只转换符号和封装；上游即便未导出 3D 仍可能写入失效模型链接，导入前会移除这些 model 块，并规范化上游旧封装命名空间。不会将未经核实的 3D 模型挂到 PCB。

## 转换器查找顺序

1. 工程配置中的 `converter_executable`。
2. 环境变量 `PART_SELECTION_CONVERTER`。
3. 安装包内 `third_party/EasyKiConverter/runtime/easykiconverter.exe`。
4. 当前 Windows 用户 KiCad 10 的本地插件完整版（使用实际“文档”目录）。
5. PATH 中的 `easykiconverter`。

开发机器可临时设置 `PART_SELECTION_CONVERTER` 指向本机安装；分发包不写死 `D:/app`，完整版同事不需要另装转换器。转换独立进程运行、隐藏命令行、最长 180 秒；队列最多 4 个，每次执行一个。

## 构建完整版

准备脚本会下载固定的上游发布 ZIP 并核对预置 SHA-256。构建阶段逐文件比较运行时与该可信发布包，不能用修改过的 EXE 或仅版本字符串相同的程序冒充对应源码。发布 ZIP 保留在准备目录，与 source、notices 同级。

运行 `tools/kicad_plugin/prepare_converter_bundle.py --help` 查看准备参数，再用准备目录构建：

```powershell
python -B tools/kicad_plugin/prepare_converter_bundle.py --runtime D:/app/ezkiconv
$cache = python -B -c "import sys; sys.path.insert(0, 'tools'); from part_selection_runtime import runtime_directory; print(runtime_directory('.'))"
python -B tools/kicad_plugin/package_plugin.py --output "$cache/part-selection-full-0.1.2.zip" --converter-runtime D:/app/ezkiconv --converter-source "$cache/easyki-bundle/source" --converter-notices "$cache/easyki-bundle/notices"
```

完整版包含固定 v3.1.11 / `1acc47c028ed1f75066ba134f6a2f327c0751c0e` 的独立 CLI 运行时、完整对应转换器源码 ZIP、GPL 许可、依赖许可/来源记录与 SHA-256 清单。不打包用户生成库、缓存、配置、日志或静态开发库。Qt 动态库保持可独立替换，依赖源码获取地址见 THIRD_PARTY.md。插件自身现有许可不作变更；源码和许可材料应随安装包一起保留，公开分发前另行核对所用二进制的依赖与再分发条款。

不传 `--converter-*` 的原命令仍构建轻量包，可用保留封装和库封装选择模式；转换需自行配置运行时。`--install` 支持同一组参数，安装前应退出选料窗口和 KiCad。不能把只包含 Python 代码的轻量包称为已内置转换器的完整版。

## 接口与测试

### 代理与失败排查

转换器 v3.1.11 实际通过 EasyEDA 的 components API 下载 CAD 数据，不是直接解析立创商品网页。当前上游 CLI 没有代理开关；Qt 可能使用 Windows 系统代理，仅清除 HTTP_PROXY/HTTPS_PROXY 不能保证直连。本插件不修改系统代理，不宣称已实现自动绕过。

如果怀疑代理影响，请在代理软件中对 easyeda.com 设置直连或临时关闭代理后重试，比较同一 C 编码；代理开启本身不代表必然失败。实际转出的 C4283613 在本机代理开启时也通过。网络不通、网站拒绝、缺少 CAD 数据和本地文件占用应分开排查。

每次转换记录输出和代理启用状态（不记录代理地址、密码），超时也会保留日志；位置为工程外缓存的 `part_selection_converter_logs/`，预览内部转换日志以错误中的绝对路径为准。界面进度区分转换、校验、注册和本地保存。转换失败不保存此次选型；本地保存失败会明确提示权限或文件占用，不能通过反复点击掩盖。运行中短暂轮询失败会继续等待原任务，不重新发起转换。

- `POST /api/select_candidate`：旧调用兼容，缺省 `footprint_mode=keep`，`convert` 时同步执行。
- `footprint_mode=library` 必须先提交 `phase=preview` 获取绑定的库选择上下文；随后调用 `POST /api/library_footprint`，传入 `preview_id/id` 只读所选封装信息，只有显式 `render=true` 才运行 CLI 渲染。库列表限工程已注册且位于工程内的本地 KiCad `.pretty`，不包含外部/全局库。最后提交 `footprint_choice`（`source=existing/existing_id/acknowledged=true`）；未读取、未确认、编号不符、过期或文件/行/原理图并发变化均拒绝保存。
- `replace_value`、`replace_comment`：两个接口均接受严格 JSON 布尔值，缺省 `false`；字符串 `"false"`、整数及 `null` 会在转换/保存前拒绝。选择“不替换”不是清空字段，也不会删除之前保存的字段覆盖。
- `POST /api/start_selection`：异步选料任务，接受相同 payload，返回 job_id。
- 网页转换采用 `phase=preview`，任务结果 `result.preview` 包含真实 SVG、尺寸、目标库及不可猜测的 `preview_id`；不会保存候选。确认调用 `POST /api/select_candidate`，只提交 `footprint_choice`（`preview_id/source/existing_id/target_mode/target_library/target_folder/name/acknowledged`），原始候选和字段替换选项由服务端预览上下文恢复，不接受客户端改换位号或 C 编码。
- `GET /api/selection_job?id=...`：读取 stage/message/done/error/result。任务失败不会被当成保存成功。
- `python -B -m pytest tests -q`：包含保留封装、转换失败、注册回滚、复用校验、进度任务、安装包内容及现有回归测试。真实网络转换请只在工程外的隔离测试工程执行，不对正式 CAD 做测试写入。
