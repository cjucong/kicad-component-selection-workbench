# 开发接手说明

[工具首页](../README.md) | [使用与配置](part-selection.md) | [插件安装](kicad-plugin.md) | [测试](../../tests/README.md)

## 仓库边界

先读 [AGENTS.md](../../AGENTS.md)。本仓库只有通用选料插件、桌面/打包入口、网页资源及测试；CAD、生产 BOM、工程库、FPGA、PCB 3D 工具和专项电路测试在工程仓库。

独立 KiCad 解析器是 `tools/kicad_sexpr.py`，不得重新导入 `fpga_pin_assignment.py`。安装包按白名单收集程序与资源，不携带工程配置、记录、缓存、日志、测试临时产物或 Git 数据。生产 BOM 导出器与其规则由工程维护，不是插件依赖。

## 路径契约

- desktop 接受绝对 `--project` 工程目录/文件或显式 `--config`；server 同样要求明确目标。两者同时提供时验证 project_root 一致。
- 不从源码父目录、当前目录、上一次工程猜测。中文启动器转发参数，无参数使用工程选择器；桌面私有 Python 环境重启必须保留 --config。
- 已有 BOM/part_selection_config.json 继续使用，不强制重写。project_root 相对配置父目录；schematic_dir、bom_dir、source_bom、input_refs、output_overrides、stock_cache、converter_executable 相对工程根目录或使用展开后的绝对路径。
- 默认 source_bom 是 `__schematic__`，保持其 Path 表示兼容测试；默认匹配 `BOM/*LCSC*.csv`。具体工程可保留原 CSV 及匹配规则。
- “设置 → 工程设置…”通过 GET/POST /api/path_settings 读取、校验和保存，/api/path_picker 打开隐藏进程承载的原生路径选择器。正式数据默认工程 BOM，可指定其他目录；保存时先备份并按保存前确认复制原记录、导出和日志，保留原文件、拒绝覆盖不同目标。配置版本变化或任务忙碌时拒绝切换；数据路径即时应用，runtime_cache/desktop_home 下次完整重启生效。旧 /api/open_path_config 保留兼容，但菜单不再启动记事本。直接手改 JSON 仍需手动迁移并重启。
- `runtime_directory(project_root)` 返回工程外同盘缓存，KICAD_PART_SELECTION_CACHE 指定绝对父目录，程序按工程路径哈希隔离。KICAD_PART_SELECTION_HOME 指定工程外绝对桌面环境/日志基目录。
- 正式导入库仍是工程设计依赖，目标选择在现有预览 UI 中完成；不与临时缓存混用。

## 模块与数据流

### 桌面生命周期

用户入口仅保留 WebView2 独立窗口；删除 `--browser`、`--open` 和自动浏览器回退。localhost HTTP 保留作桌面内部传输及隔离诊断/测试，不再是面向用户的浏览器模式；外部商城链接仍可用 Chrome 打开。

`part_selection_lifecycle.py` 管理宿主与后台。宿主生成随机令牌，通过环境变量 `KICAD_PART_SELECTION_DESKTOP_TOKEN` 交给子服务，服务读取后移除；令牌不写命令行、配置或日志。`/api/config` 的 desktop_launcher_version=3，desktop_owner_id 仅返回 SHA256 摘要。启动核对程序目录、工程、配置和宿主摘要，同工程旧/其他宿主服务需退出，不静默复用或另起同工程写入服务。

WebView 只暴露 `DesktopBridge.finish_exit`，不得直接暴露带有 window 对象的控制器。`web/desktop-lifecycle.js` 统一处理叉号和菜单退出/重启，先 flushRowEdits，失败保留窗口；原生 Closing 事件先取消关闭，在另一线程调 JS，避免 UI 线程等待自己。后台 `POST /api/desktop_shutdown` 验证 X-Desktop-Token，并通过 SettingsGate 拒绝在途请求和搜索/转换，停止接收新请求后正常 shutdown。确认 TCP 端口释放才能销毁窗口；重启先释放工程窗口互斥锁，再携带显式工程/配置启动隐藏宿主。不结束 KiCad、不强杀用户任务。

后台监视 `--desktop-owner-pid`，Windows 使用进程句柄避免 PID 重用；宿主异常退出后等待零请求及任务空闲再停止服务。旧服务没有该接口，需要首次升级时人工核对后退出，不能绕过归属校验批量结束 Python 进程。

```text
显式工程/配置
  -> 配置 + 当前原理图/BOM + overrides + stock_cache
  -> PartSelectionApp.rows()
  -> 分组、候选、主料/备选/订货记录
  -> 用户确认同步 -> 工程归属/编辑器保护 -> 属性写入 -> 磁盘校验 -> 审计日志
```

| 模块 | 职责 |
| --- | --- |
| part_selection_server.py | HTTP 路由、应用服务、工作台 HTML/CSS/JS 和批量同步 |
| part_selection_helper.py / kicad_sexpr.py | 原理图属性/BOM 处理、通用解析 |
| part_selection_overrides.py | 配置、主料及覆盖记录兼容 |
| part_selection_paths.py | 共享显式工程/配置 CLI 解析，随包分发 |
| part_selection_settings.py / web/path-settings.* | 菜单路径表单、版本校验、任务互斥、备份复制与保存回滚 |
| start_part_selection_tool.vbs / launch_part_selection.py | 隐藏启动桥，完整转发 --project/--config，未指定目标时选择工程 |
| part_stock_lookup.py / part_selection_search.py | 库存、参数、分批候选、缓存与请求竞态 |
| part_selection_library.py | 转换器发现、隐藏有界 CLI、库注册与回滚 |
| part_selection_footprints.py / web/ | 封装预览、编号检查、绑定确认与前端资源 |
| part_selection_runtime.py | 工程外临时路径与同盘约束 |
| kicad_plugin/desktop.py | IPC/显式工程识别、私有运行时、窗口与服务身份 |
| kicad_plugin/package_plugin.py | ZIP 白名单和备份安装 |
| kicad_plugin/prepare_converter_bundle.py | 固定版本转换器来源与许可材料 |

读取工作台状态使用 PartSelectionApp.rows()，不要自行拼合 CSV、记录和库存。选料记录默认在目标工程 BOM/part_selection_overrides.json；同步审计和运行日志在 bom_dir，分别以 CSV 和 JSONL 保存。按 attempt_id 串联事务。

## 保持的行为边界

- 分组只按 Comment + Value + Footprint；主料冲突显式清空，不回退历史料号。备份后持久化，不丢备注、备选、订货和审计信息。
- replace_value、replace_comment 为独立严格布尔值，缺省 false；保留字段不等于删除已存在待同步覆盖。
- keep/library/convert 三种封装模式默认 keep。library/convert 的正式网页确认必须基于 phase=preview 及绑定的 footprint_choice，检查候选、位号、行版本、原理图/库摘要、焊盘编号和人工确认。取消、过期或并发变化不保存此次选择。
- library 仅 render=true 才渲染，查询编号/尺寸不运行转换器。目标库选择保留在 UI；导入正式库时使用工程相对库表，并保护共享旧封装。
- 库发布与记录保存是不同阶段；后者失败需明确提示“库已保存但记录未保存”，不能静默删除已安全新增的库。
- 同步只更新 property，不改符号几何、引脚、实例 UUID 或 PCB。明确 selected_refs 只处理所选位号，不扩大成整个相交分组，不套用历史工程位号映射。
- 工程路径及窗口 PID/HWND 使用前重新核验。无法唯一确认或 KICAD_BUSY 时停止，不回退到第一个窗口或直接强杀 KiCad。
- 搜索结果、并发和缓存均有界；客户端序号隔离旧请求，精确 C 编码不扩为宽泛替代搜索。库存不能当成实时采购承诺。
- 新窗口支持原生文字选择/复制粘贴，不能全局阻止浏览器默认操作；列宽手动设置和候选窗口最小化状态保持兼容。

## 安装与验证

每轮修复按根目录 AGENTS 执行非破坏式备份、回归、提交推送插件 origin、重新安装并核对。不要把本插件推到工程仓库；不强推，不监视普通保存直接发布未验证文件。行编辑使用 web/row-edits.js 串行自动保存，按字段发送部分更新；新增输入需接入自动保存并尊重输入法组合事件。拆分的选中位号合到同一临时行，逐位号元数据不能越过拆分范围。

KiCad 管理的 requirements.txt 仅放适用的 wheel 依赖；pywebview 通过 desktop-requirements.txt 装入独立桌面环境，不污染 KiCad Python。轻量直接安装保留已有 third_party 转换器，完整包须携带固定版本源码与许可。--plugin-dir 可覆盖默认的实际 Windows 文档目录，不能固定某台开发机路径。

从仓库根目录执行：

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
python -B -m pytest tests -p no:cacheprovider
python -B -c "from pathlib import Path; files=list(Path('tools').rglob('*.py')); [compile(p.read_bytes(),str(p),'exec') for p in files]; print(f'{len(files)} Python files parsed')"
```

测试只使用工程外自动清理的临时项目，不带入生产 CAD、BOM 或 .git；专项冒烟与环境见 [测试说明](../../tests/README.md)。抽取前端脚本、KiCad CLI 解析和截图也必须输出到外部缓存，不能写 Temp 或源码目录。

交付注明实际执行的命令、通过/失败/跳过范围；没有运行真实 KiCad UI、网络查询或 EasyKiConverter 就明确未验证。原工程既有改动不可混入插件提交。
