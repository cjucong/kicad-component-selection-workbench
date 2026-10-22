# 立创符号与封装转换

[工具首页](../README.md) | [使用与路径配置](part-selection.md) | [插件安装](kicad-plugin.md)

命令从独立插件仓库根目录执行，目标 CAD、BOM 与库位于另一个工程。下文 DSMU 和 Libraries/DSMU 是原工程示例；其他工程使用自己的已注册库和目标目录。

## 选料时如何使用

在“更换型号”窗口搜索框后的“封装”下拉框选择：

- **不替换**（默认）：更新 C 编码及物料参数，不把商城的 `0805`、`BGA-64` 等标签写入 Footprint；每个位号保留自身当前/待保存封装。Value、Comment 由另外两个下拉选项独立控制，默认都不替换。
- **替换库封装**：点击“选用主料”后打开工程库列表，默认选择当前封装；可按库和名称筛选。只读取焊盘编号和尺寸，不下载、不转换、不自动生成预览。需要看图时点击“预览”；确认无需等待预览。编号不一致或原封装不可读时禁止替换，编号一致仍须人工确认尺寸和引脚功能。取消不保存此次选料；确认仅记录所选库标识，不修改库文件、不导入符号。
- **仅改名保留几何**：优先读取已保存 PCB 中该位号的实际封装，找不到时才退回当前绑定本地库；选择目标工程库、填写新名称后绑定。可否共用只按铜皮焊盘编号、局部坐标和方向判断；若丝印/用户图形不同，会提示并统一采用所选基准封装的版本。原点、焊盘和其他基准几何不会被转换器改动；不联网、不调用转换器、不导入符号。同名不同内容时提示覆盖或另存新名称，不自动追加 C 编码。焊盘编号、局部坐标或方向不同，或原封装不可读时停止并要求拆行。无需等待预览，勾选核对后即可保存。
- **替换立创封装**：点击“选用主料”后后台调用 EasyKiConverter，在工程外缓存下隔离转换并用 KiCad 原生渲染真实封装。先比较当前工程库与转换结果，按当前基准封装的同编号焊盘局部中心校正方向，再按焊盘包络中心对齐；丝印文字位置或圆/方焊盘形状不同不会误拦截，但编号、局部中心或方向不一致仍会要求拆行/人工核对。确认后才添加到目标库并保存此次主料；取消或转换失败不修改正式库及选料记录。

### 对比与目标库

预览默认叠加本地原封装与配准后的立创封装，也可切换同尺度并排；共同视野使用同一毫米坐标，不分别居中或独立缩放。使用不同颜色区分来源，可调透明度，分别显隐铜层、正反丝印、位号、数值、装配层和庭院层，原点/1 号焊盘标记辅助核对。显隐只影响预览副本，不删除正式封装内容。焊盘数量、编号、单焊盘尺寸与包络（不是本体尺寸）显示在预览区内。相同基准的渲染结果在本次预览内复用，切换显隐不再转换或启动渲染。可选择“使用现有封装”“仅改名保留几何”，或确认转换结果后选择：

- **合并到现有库**（默认）：优先工程已注册的 `DSMU`，也可选其他工程内 KiCad `.pretty` 库；不向全局、外部或旧版受管理的 C 编码库写入。
- **新建封装库**：输入库名和工程内相对目录，如 `Libraries/DSMU`，自动注册工程库表。
- **独立转换库**：保存在 `Libraries/DSMU/LCSC/selected/LCSC_Cxxxx_Selected.pretty`。

封装名默认采用安全的商城封装名，例如 `0603`，不适用时保留实际转换名；不把商品标签当成几何正确性的证明。同名同几何复用（仅改名还核对字段等内容）；同名不同内容弹出“已有同名封装”，暂停保存此次选料。默认可输入新名称另存，也可选择覆盖，并勾选“其他引用此库封装的器件也可能受影响”后确认。覆盖前备份原文件，发布失败回滚；目标变化后须重新核对。取消不修改此次选料及工程库。不自动添加 `_Cxxxx` 或摘要，已有历史后缀不自动迁移。库名前缀仍是 KiCad 必需的 `库名:封装名`，例如 `DSMU:0603`。

转换结果与该行原有封装的焊盘编号不一致、或无法读取原有工程本地封装时，禁止直接采用转换结果。编号一致也不证明电气兼容，须核对数据手册和引脚功能，保存关闭库编辑器后勾选确认。预览绑定原始候选、位号、选项及文件摘要，30 分钟有效；行记录、原理图封装或库文件变化时要求重新预览。取消保留搜索窗口和结果。

采用立创几何前，以该行当前绑定的本地库封装为基准，按同编号焊盘位置计算刚性旋转与平移，并将变换应用于实际保存的转换封装，不只对齐图片。不会改变焊盘编号，不缩放、不镜像。编号缺失、重复编号歧义、点数不足或对应布局不能在容差内重合时禁止应用；仅外形大小一样不能判为可配准。图形变换使用 KiCad 自带 Python/pcbnew，环境缺失时提示，不静默保存未配准几何。“仅改名”使用原封装副本，不受转换封装配准失败影响。

本地库原点与 PCB 上的放置坐标是两层信息：该功能规范库内原点/方向，不主动移动 PCB 上的器件或走线。它不能把不同间距的焊盘变得一致，也不替代对 PCB 中人工修改过的封装实例进行核对。同步原理图后，在 KiCad 的更新 PCB 预览中检查目标封装、位置、方向及连接，再执行正式更新。

同一搜索栏提供 Value、Comment 两个独立的“替换 / 不替换”选项，每次新开默认不替换；最小化后恢复当前窗口会保留选项。不替换的字段保留当前值，包括已保存但未同步的人工修改。选择替换后按位号类别生成：

| 器件 | 替换 Comment | 替换 Value |
| --- | --- | --- |
| 电阻 R | `resistor` | 阻值，如 `50kΩ` |
| 电容 C | `capacitor` | 电容量，如 `100nF` |
| 电感 L | `inductor` | 电感值，如 `1uH` |
| 芯片及其他器件 | 完整型号 | 完整型号 |

完整型号始终保存在物料参数/Description 的“型号”行。阻容感数值只能从明确参数或可信参数摘要中提取，不猜测型号中的数字；找不到数值时保留原 Value。三个选项（Value、Comment、封装）彼此独立。选料保存只修改网页待同步状态，仍须手动同步原理图。

确认后写入选料覆盖记录的 Footprint 是实际注册的目标标识，例如 `DSMU:0603`。仍需点击“同步原理图”或勾选同步，才会写入原理图属性。选择转换封装时符号单独导入 `Libraries/DSMU/LCSC/symbols/`，Footprint 引用同步为最终标识；选择复用现有封装则不导入符号。**不会替换已放置的原理图符号，不会修改 PCB**。请核对后再从原理图更新 PCB。订货链接没有有效 C 编码时不提供转换。

## 工程文件

```text
Libraries/DSMU/DSMU.pretty/封装名.kicad_mod
Libraries/DSMU/LCSC/symbols/LCSC_Cxxxx_摘要.kicad_sym
sym-lib-table
fp-lib-table
```

库表使用 `${KIPRJMOD}` 相对路径，可随工程复制。默认新增或复用，覆盖同名不同内容必须由本次冲突提示明确确认并先备份；同名异路径注册、预览源被人工修改或解析失败时停止。临时缓存由 `part_selection_runtime.runtime_directory(project_root)` 定位，默认在工程外同盘的 `../.kicad-part-selection-cache/<工程名>-<路径哈希>/`，避免 KiCad 递归备份，且保证跨目录原子写入不跨磁盘。库表备份和事务清单在其 `part_selection_library_backups/`；转换预览在 `footprint-preview-*/`，最多保留 8 份、后续转换会清理过期项。日志路径以错误提示中的绝对路径为准。正式转换库仍在工程 `Libraries/`。底层转换器在暂存区生成按 C 编码隔离的产物，但更换型号接口必须先预览再确认，不能通过旧请求跳过配准直接保存。

目标库选择已在封装对比 UI 中提供。正式符号、封装及库表是工程设计依赖，不随插件搬家或缓存清理删除。顶部“设置 → 工程设置…”可选择转换器；缓存父目录及桌面目录在折叠的高级路径设置中；缓存须绝对、工程外且同盘，桌面目录须绝对且在工程外，运行目录下次完整重启生效。留空时分别使用 KICAD_PART_SELECTION_CACHE/HOME 或默认位置。菜单支持先备份再复制已有正式记录并即时应用；直接手改 JSON 不自动迁移且需要重启，不能未经确认建立空记录。

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
$converter = 'E:/Converters/EasyKiConverter-3.1.11'
python -B tools/kicad_plugin/prepare_converter_bundle.py --runtime $converter
$cache = python -B -c "import sys; sys.path.insert(0, 'tools'); from part_selection_runtime import runtime_directory; print(runtime_directory('.'))"
python -B tools/kicad_plugin/package_plugin.py --output "$cache/part-selection-full-0.1.2.zip" --converter-runtime $converter --converter-source "$cache/easyki-bundle/source" --converter-notices "$cache/easyki-bundle/notices"
```

完整版包含固定 v3.1.11 / `1acc47c028ed1f75066ba134f6a2f327c0751c0e` 的独立 CLI 运行时、完整对应转换器源码 ZIP、GPL 许可、依赖许可/来源记录与 SHA-256 清单。不打包用户生成库、缓存、配置、日志或静态开发库。Qt 动态库保持可独立替换，依赖源码获取地址见 THIRD_PARTY.md。插件自身现有许可不作变更；源码和许可材料应随安装包一起保留，公开分发前另行核对所用二进制的依赖与再分发条款。

不传 `--converter-*` 的原命令仍构建轻量包，可用保留封装和库封装选择模式；转换需自行配置运行时。`--install` 支持同一组参数，安装前应退出选料窗口和 KiCad。不能把只包含 Python 代码的轻量包称为已内置转换器的完整版。

轻量直接安装会保留旧插件中的 third_party 转换器，轻量 ZIP 自身仍不含转换器。`--install --plugin-dir "E:/KiCadUser/10.0/plugins"` 可覆盖实际 Windows 文档目录的默认插件父目录；工程配置、记录和正式导入库不进入安装包。

## 接口与测试

### 代理与失败排查

转换器 v3.1.11 实际通过 EasyEDA 的 components API 下载 CAD 数据，不是直接解析立创商品网页。当前上游 CLI 没有代理开关；Qt 可能使用 Windows 系统代理，仅清除 HTTP_PROXY/HTTPS_PROXY 不能保证直连。本插件不修改系统代理，不宣称已实现自动绕过。

如果怀疑代理影响，请在代理软件中对 easyeda.com 设置直连或临时关闭代理后重试，比较同一 C 编码；代理开启本身不代表必然失败。网络不通、网站拒绝、缺少 CAD 数据和本地文件占用应分开排查；本说明不声明独立仓库已完成真实联网转换验证。

每次转换记录输出和代理启用状态（不记录代理地址、密码），超时也会保留日志；位置为工程外缓存的 `part_selection_converter_logs/`，预览内部转换日志以错误中的绝对路径为准。界面进度区分转换、校验、注册和本地保存。转换失败不保存此次选型；本地保存失败会明确提示权限或文件占用，不能通过反复点击掩盖。运行中短暂轮询失败会继续等待原任务，不重新发起转换。

- `POST /api/select_candidate`：旧调用兼容，缺省 `footprint_mode=keep`，`convert` 时同步执行。
- `footprint_mode=library` 必须先提交 `phase=preview` 获取绑定的库选择上下文；随后调用 `POST /api/library_footprint`，传入 `preview_id/id` 只读所选封装信息，只有显式 `render=true` 才运行 CLI 渲染。库列表限工程已注册且位于工程内的本地 KiCad `.pretty`，不包含外部/全局库。最后提交 `footprint_choice`（`source=existing/existing_id/acknowledged=true`）；未读取、未确认、编号不符、过期或文件/行/原理图并发变化均拒绝保存。
- `replace_value`、`replace_comment`：两个接口均接受严格 JSON 布尔值，缺省 `false`；字符串 `"false"`、整数及 `null` 会在转换/保存前拒绝。选择“不替换”不是清空字段，也不会删除之前保存的字段覆盖。
- `POST /api/start_selection`：异步选料任务，接受相同 payload，返回 job_id。
- 网页转换采用 `phase=preview`，任务结果 `result.preview` 包含真实 SVG、尺寸、目标库及不可猜测的 `preview_id`；不会保存候选。确认调用 `POST /api/select_candidate`，只提交 `footprint_choice`（`preview_id/source/existing_id/target_mode/target_library/target_folder/name/acknowledged`），原始候选和字段替换选项由服务端预览上下文恢复，不接受客户端改换位号或 C 编码。
- 同名不同内容返回 `ok=true, footprint_conflict={footprint,name,token,changed}`，不保存候选。另存时修改 `footprint_choice.name` 再提交；覆盖时保留原选择并提交 `collision=overwrite, collision_token=token`。令牌绑定本次完整选择与目标文件摘要，不能用于其他文件；目标改变则重新提示或拒绝过期预览。没有有效令牌不允许覆盖。
- `GET /api/selection_job?id=...`：读取 stage/message/done/error/result。任务失败不会被当成保存成功。
- `python -B -m pytest tests -p no:cacheprovider`：包含保留封装、转换失败、注册回滚、复用校验、进度任务、安装包内容及现有回归测试。设置 PYTHONDONTWRITEBYTECODE=1；真实网络转换请只在工程外的自动清理隔离测试工程执行，不对正式 CAD/BOM 做测试写入，不带入 Git 数据。普通回归不能代替真实 UI 或网络冒烟，见 [测试说明](../../tests/README.md)。
