# 插件回归测试

`test_selected_bom_export.py` 使用合成工程覆盖勾选行 BOM 导出的分组数量、部分分组、空/过期勾选、NC 排除、HTTP 与前端选择范围；不读写生产 CAD 或联网查料。`run_selected_bom_export_smoke.py` 使用隔离 HTTP 工程和真实 Chromium 验证可见勾选、隐藏行排除、清空选择和恢复选择。

PCB 重复待核验：`test_design_status.py` 覆盖精确等值数字及顶层排序、真实坐标/UUID/字段/编号/重复对象/轮廓顺序反例；`test_pcb_native_types.py` 覆盖直线反向与位置/宽度/层变化；`test_pad_mapping_native_types.py` 覆盖已应用适配目标变更的无变化核验、首次写入及真实焊盘变化拒绝。完整事务不采用完成状态的几何容差；真实 `run_native_design_keep_open.py --with-footprints` 另验保存、重开工作台及状态证据持久化。

本地库存手动编辑：`test_part_selection_inventory.py` 覆盖独立 HTTP 更新、非负安全整数、原值冲突、来源保留、工程外备份、失败不覆盖及 CAD/选料不变。`run_local_inventory_smoke.py` 用真实 Chromium 与隔离 HTTP 工程验证 600 ms、Enter、失焦、Esc、同编码跨行/NC 共享、连续在途编辑、失败重试、跨窗口冲突确认、刷新保持焦点和重开持久化。`run_stock_refresh_smoke.py` 验证单按钮开始/暂停/恢复、折叠菜单在 1440/768/390 宽度可用、Esc 收起、刷新全部不跳过近期缓存。上游库存响应为受控合成数据，不冒充真实商城性能测试。

`test_design_sync.py`、`test_design_sync_ui.py` 覆盖保留自动流程意图、重新预检后只续接一次、来源变化拒绝、专项端子风险暂停以及不再重复选择对齐。`run_native_design_keep_open.py --with-footprints --report-dir <工程外目录>` 用真实一次性 KiCad 工程验证字段更新、仅改名、标准库替换、预检故障后续接，以及手动 17° 旋转和平移的实际焊盘坐标；完整核验保存、网络、未选对象与无变化重检，不写生产 CAD。

`test_pcb_library.py` 与 `run_native_library_reader_probe.py --report-dir <工程外目录>` 验证私有 Windows 桌面上的完整原生库读取、当前桌面无读库窗口、复杂焊盘/图形/正背面、256 项 LRU 及哈希失效、失败不回退和本任务进程清理。`test_footprint_change_types.py` 验证可信原始 PCB 的丝印绘图点 1 nm 重载舍入兼容，未知子结构、铜皮、位置、身份和超限仍拒绝，失败缓存不妨碍撤销后重试。

`test_footprint_unify.py` 覆盖不同原点/方向和丝印的铜皮等价、实际间距/尺寸/钻孔/阻焊/机械孔变化拒绝、跨行原子记录保存、撤回、确认和 C 编码保留、来源改变及冲突跳过。`test_footprint_overlay_ui.py` 的 `library-reuse` 场景验证预览调整不隐式生成 `_mapped`；显式适配、放弃/额外端子和转换手动调整仍跑原场景。库存另覆盖两个不同编码同时读取、独立临时浏览器配置及清理；不声称商城单次请求变快。

`python -B tests/run_native_unification_roundtrip.py --report <工程外绝对报告.json>` 使用 KiCad 捆绑 pcbnew 构造正/背面、不同原点和角度的等效两焊盘封装，核验实际批量工作台记录写入，再按正式纯放置求解器调整一次性 PCB 并保存/LoadBoard 回读，保留网络和走线，清理夹具及缓存。它不是 GUI/IPC 事务或工程管理器重开测试。2026-10-26 本轮原生 UI 适配器检查通过选库/同步/原位事务/撤销/显式保存，但管理器重开因文档归属 unknown 未通过；全工程 cycle 在 Search panel unavailable 导航阶段未通过。不要将这些子阶段通过表述为完整 GUI 闭环通过，错误报告保留在工程外。

库存退出回归：`test_desktop_lifecycle.py` 使用真实 HTTP 服务验证慢库存取消、错误身份拒绝、后台端口释放及活跃写入保护；`test_part_stock_lookup.py` 覆盖阻塞在 `urlopen` 内的取消、迟到结果不写缓存、同编码完整请求合并和独立搜索共享来源的取消隔离。外网均替换为受控阻塞，不声称真实商城响应速度。`run_desktop_lifecycle_smoke.py --lifecycle-only` 在真实 WebView2、可清理工程中核验标题栏关闭、慢行库存取消、行编辑保存、重启和端口释放；不运行候选窗口流程，也不写生产 CAD。网络读取为只读守护线程（最多 4 个），缓存提交仍在请求线程完成，不能通过忽略活跃请求计数绕过退出保护。

取消覆盖 `/api/stock` 和默认带库存的 `/api/rows`，后者将取消域传播到每个批量子任务；回归包含 8 编码中 4 个活跃、其余排队时的取消及迟到缓存隔离。Future 完成边界分别验证成功结果、真实源超时，以及共享所有者取消不污染独立搜索重试。

改名来源实线兼容：`test_footprint_change_types.py` 与 `test_pcb_field_updates.py` 覆盖 `default` / `solid` 等价、尚未改名/已改名的原生预检，以及位置/方向/正反面/UUID/路径/焊盘/图形/线宽/虚线变化的拒绝。混合旧哈希必须先核对原 PCB 文件 SHA-256 和原实例哈希；缺失/损坏备份不放行，缓存不能掩盖之后的移动。`run_native_rename_source_probe.py --report-dir <工程外目录>` 专门执行真实正背面实例的工作台改名、原理图同步、PCB 更新、保存关闭重开与服务重建，确认旧版实线哈希及新版规范哈希均正确识别无变化，保持完整实例和走线；不读取无关标准库或执行转换。完整 `run_native_grouped_footprint_probe.py` 也包含同一保存重开回归阶段。两者都只使用可清理的合成工程，不接受生产工程参数。

历史额外焊盘重复同步：`test_pad_mapping_native_types.py` 使用原生 protobuf 跑实际 PCB 预检，区分已接网/字段修改/服务重启与首次冲突、缺失、重复及真实几何变化；`test_pcb_native_types.py` 覆盖舍入改变图形排序、多重集数量及非贪心匹配，不放宽嵌套节点顺序。`run_native_pad_mapping_probe.py` 增加符号由 8 扩到 10 脚、真实 F8 接网、保存重开及重复预检的网络/UUID 保留检查。若系统禁止定位输入，可显式使用 `--allow-navigation-unavailable` 记录导航失败后继续 PCB 验证；此模式不代表导航通过，默认仍要求导航完成。

`test_footprint_overlay_ui.py` 的 `viewport` 场景使用真实 Chromium 滚轮/右键/按钮，验证两个来源的鼠标锚点缩放、同比例并排、滚轮平移、缩放上限、展开/Esc/窄屏，以及缩放后的毫米拖动。查看操作不能清除确认、发起网络读取或改变配准；切换封装重置视图。

PCB 保存超时：`test_pcb_session.py` 覆盖保存独立限时、禁止 Req0 重发、断开迟到回复连接、实时/磁盘/会话不一致不误报、失败证据。`run_native_project_cycle_probe.py --save-reply-timeout --report-dir <工程外目录>` 在可清理的真实 KiCad 工程完成原生保存，并注入 5.2 秒延迟与丢失回复，验证 UI 阶段、单次保存、只读恢复及保存重开后的字段、几何、网络和走线；这是故障注入，不声称复现了生产工程的具体耗时原因。

无法使用系统定位按键的环境可单独运行 `run_native_pcb_save_probe.py --report-dir <工程外目录>`：创建 23 个正背面器件的临时工程，通过管理器打开 PCB、原生事务更新、严格回读、真实保存加慢回复/丢失回复注入、只读恢复并关闭重开。核对所选及未选封装（重开仅使用既有 1 nm/角度序列化兼容）、网络和走线；不模拟或宣称通过原理图定位，测试工程及进程正常退出后自动清理。

`run_native_footprint_viewport_probe.py --report-dir <工程外目录>` 在真实 WebView2 中验证两个来源的缩放、平移、展开、Esc 和配准不变并截图；输入图层为合成夹具，滚轮事件通过 DOM 派发，不声称物理鼠标端到端或 KiCad 同步验证。该只读探测不访问工程、商城或 PCB。运行完整回归时将 `PYTHONPATH` 设置为仓库 `tools` 的绝对路径，以供桌面运行时子进程导入模块。

`run_native_grouped_footprint_probe.py` 另以真实 IPC 验证锁定实例读取、改名和新增自定义字段、同批无变化项、原生回读及单次撤销；新增字段不能带原生序列化不保留的 `text.parent`。它不依赖原理图定位快捷键，不能替代完整保存/关闭/重开测试。

锁定实例：`test_pcb_session.py` 验证只读快照不拒绝锁定；`test_pcb_update.py`、`test_pcb_field_updates.py` 覆盖无需更新/改名/字段允许与几何拒绝、底层禁止伪装成字段来移动/旋转/改网络/解锁。`test_design_sync.py`、`test_design_sync_ui.py` 验证受阻原因、读取完成状态及解锁后重新预检。`run_native_project_cycle_probe.py --report-dir <工程外目录> --locked-metadata` 在可清理真实 KiCad 工程验证锁定字段/改名/无需更新、保存关闭重开保持锁定及几何，并确认不安全写入在备份和事务前拒绝；然后仅对测试夹具显式解锁，继续既有几何替换闭环。

层级批量回归另覆盖根页、嵌套页、重复实例与孤立旧页排除，以及 UUID/引脚/字节变化使预览失效。写回接口不能覆盖所选层级实例时必须在关窗和任何写入前拒绝；验证结果中的嵌套 missing_refs 也不得误报成功。

同名封装批量管理：`test_footprint_batch.py` 验证完整库 ID、跨行范围及过期成员检测、各位号独立选料/撤回记录和合行不继承其他物料。Chromium `batch` 场景验证范围确认及共享库铜皮差异确认；`test_footprint_choices.py` 保证风险确认不能绕过编号/局部中心安全检查。`run_native_grouped_footprint_probe.py` 从 R1 单行打开，验证实际扩展至同封装 R1/R2 后分别更新正背面 PCB 实例，保留各自网络、位置、走线及撤销。

同步阶段与恢复：`test_design_sync.py`、`test_design_cycle.py`、`test_design_sync_ui.py` 验证常驻真实状态/耗时、无变化跳过关窗、保存成功但导航失败时不重放写入，以及单独重试打开/定位。`test_pcb_native_types.py` 对保存重开角度兼容 KiCad 七/八位小数格式化；仍拒绝非格式化角度、超过 1 nm 的坐标变化、未知字段及网络/几何变化。真实完整闭环由 `run_native_project_cycle_probe.py` 单独验证，不把模拟测试视作原生通过。

`test_export_folder.py` 用一次性工程通过两个真实导出 HTTP 入口检查写入并关闭文件后才打开实际目录（含中文、空格和自定义路径），以及导出失败不打开、目录打开失败不影响已导出文件。shell 调用在测试中替换，不打开用户的资源管理器，不读写生产数据。

`test_stock_delivery.py` 覆盖主商品渲染交期（4 小时、24 小时、工作日）、SSR/卡片三重身份、页脚排除、候选缓存、国际查询补齐与网络失败保留、主表转义及旧缓存保护；还验证超过 4 小时高库存、立即零值与部分供货、小时上限、未知及 NC 的 Python/JS 分类一致。`test_bom_export_template.py` 另验证延迟物料进入异常/当前筛选导出但不冒充数量低库存，以及本地库存零值/未知/无编码/陈旧编码、跨行共享不扣减；均使用合成数据。`run_supply_column_smoke.py` 在真实 Chromium 检查初始交期异常、快速发货局部刷新清除警告、高库存长交期重新归类与筛选。

`test_bom_export_template.py` 使用合成分组与可清理工程验证立创模板前 10 列顺序、本地库存数量/说明紧随模板、附加信息后置、同料号参数与旧缓存隔离、单套数量、筛选后连续编号、空表表头和中文/逗号/换行往返。`run_local_inventory_smoke.py` 另验证手动编辑后立即点击真实导出按钮、等待在途库存保存、零值导出、保存失败不生成旧数量文件，并逐行解析实际 CSV，确认数量不随套数或需求改变、共享编码不重复累加；不读写真实采购单或生产 CAD。原导出范围与 NC 排除由 `test_part_selection_overrides.py` 继续覆盖。

自定义封装名称：`test_footprint_choices.py` 检查后端报错指出封装名/目标库及具体限制。Chromium `custom-name` 场景覆盖两个入口的非法字符/完整库前缀/保留名称提前拦截、中文自定义名、保存失败重试、取消冲突保留草稿及保存期间 Esc/导航关闭保护。真实 WebView2 探测在六位号 8→10 的两种入口中分别使用自定义中文名称，检查非法名不能提交、注入一次保存失败后名称/映射/偏移保留，随后真实保存并核验工程库文件；只有上游立创输入和故意失败是合成夹具，不使用生产工程写入。

## 手动焊盘对应

3D 引用回归：`test_footprint_geometry.py` 覆盖带缺失 STEP/WRL 路径的恒等复制、自动平移、手动平移/旋转、多个模型、XYZ 偏移/欧拉角、非均匀缩放、显隐和透明度，实际原生保存回读且不加载模型文件。`run_native_tool_windows_probe.py` 的六位号 8→10 保存测试同时从库/合成立创入口应用非零手动偏移，使用默认名称，检查工程库文件中仍保留模型引用；报告仅记录测试工程的保存选择，失败写出对应回溯。未知顶层对象仍保留拒绝回归。

`test_footprint_pad_mapping.py` 覆盖一对一对应、对齐子集、8→10 / 10→8、不同编号、额外编号冲突、显式放弃、原理图端子验证、源库保护、现有/新建工程库发布及回滚。`test_pad_mapping_native_types.py` 用真实 KiCad protobuf 核验焊盘 UUID、网络、symbol-pin、正反面变换、新焊盘无网及重复同步保留后来分配的网络。`test_pcb_update.py` 和 `run_design_sync_smoke.py` 核验严格模式阈值、映射/几何明细及放弃已接网端子的独立二次确认。

`test_footprint_overlay_ui.py` 的 `pad-mapping` 使用 Chromium 验证两个来源的映射、额外焊盘、放弃确认、原始→最终编号、对齐子集及筛选不重置草稿。未启用手动对应时，原有编号不兼容测试仍拒绝发布；明确对应和放弃后允许应用，不再把编号不同一律当作只能预览。

`run_native_pad_mapping_probe.py --report-dir <工程外绝对目录>` 在自动清理的短路径合成工程，经过真实封装渲染/发布、分阶段同步、原生 PCB 事务、自动保存及管理器重开，覆盖库/立创两个来源、正背面及任意角度、8→10 和 10→8、不同编号、手动姿态不被二次拟合抵消、已接网端子放弃确认、映射身份/网络、额外焊盘无网和未选对象/走线不变。最后实际调用 KiCad 的“从原理图更新 PCB”，通过已核验归属的原生按钮执行并保存重开，再次核对编号和网络。仅上游立创下载转换输入是合成夹具，不代表商城联网转换或电气兼容/DRC 通过。

`run_native_tool_windows_probe.py` 另在真实 WebView2 子窗口，通过页面控件保存两个来源的手动映射及工程适配副本，保存截图并核验源库未改；夹具包含真实已保存 PCB，防止只测无 PCB 的库基准路径。另验证六位号共用 8 焊盘基准对比 10 焊盘目标，手动映射可见、额外 9/10 无网提示及取消不改工程文件。`library-pcb-reference` 的 Chromium 回归检查比较和应用始终使用服务端 PCB 基准，未提供基准的旧响应仍回退到库条目。该脚本不更新 PCB。上述脚本均不以生产工程作写入测试。

预检角度提示以模 360° 的差值比较，兼容 ±180° 等价和 0.0001° 内的纳米坐标拟合误差；真实 90° / 0.01° 变化仍显示。这只影响预检差异报告，不放宽完整事务回读、位置阈值或实际几何写入检查。库适配目标输入框在桌面/窄屏保持可输入，原生窗口展开映射后预览仍可滚动到并拖动，避免被裁切或挡住确认控件。

手动配准回归：`test_footprint_overlay_ui.py` 的 `manual` / `manual-safety` 使用真实 Chromium 指针拖动、旋转中心、角度输入及快速匹配，覆盖全部编号、9 对 5 禁用、无效输入、切行清理、取消重开和标签下方导航。`test_footprint_geometry.py`、`test_footprint_choices.py`、`test_footprint_alignment_integration.py` 验证变换校验、真实封装写入、缓存隔离与编号安全。`run_native_project_cycle_probe.py` 在一次性 KiCad 工程实际发布手动变换封装、同步原理图、更新 PCB、保存重开并核验焊盘、网络、走线及未选对象；仅上游转换输入为合成夹具，不代表商城联网转换或物理鼠标端到端测试。

本目录保留可复用测试和稳定夹具，不含生产 CAD、BOM、人工选料记录或 Git 数据。电路连通性、FPGA、工程目录检查和 U46 审核测试由原工程维护。

文字编辑菜单：`test_edit_menu.py` 使用实际 Chromium 右键输入验证原生菜单请求、输入选区、只读框、可编辑文本、对话框和位号菜单兼容；合成事件不得请求菜单。`test_kicad_selection_plugin.py` 验证页面仅能打开原生菜单、不能直接执行粘贴，只有原生菜单选择才执行固定编辑命令，导航后拒绝旧菜单。`run_native_edit_menu_probe.py --report <工程外 JSON 路径>` 在真实 WebView2 中用 CDP 右键输入打开原生菜单，通过原生菜单项的 PerformClick 检查复制事件、对话框内操作及撤销/重做/全选，不代表物理鼠标点击。系统剪贴板可用时另验复制/剪切/粘贴内容并恢复原剪贴板。操作系统拒绝访问时必须在报告中保留内容验证缺口，不把菜单/复制事件测试称为剪贴板端到端通过。Esc 由 Windows 原生菜单处理。

菜单收起回归：原生探测通过 CDP 左键点击普通页面及 HTML 弹窗内的空白，核验菜单不可见、无复制/剪切/粘贴事件且底层弹窗保持打开。Chromium 回归覆盖点击/重复点击/窗口调整/失焦通知，宿主单元测试覆盖取消、文档版本及消息乱序，不把消息测试冒充操作系统真实切窗测试。

预览权限回归：`test_footprint_choices.py` 验证编号不兼容/同行铜皮不一致仍可只读预览，但不能发布转换封装；`test_footprint_overlay_ui.py` 覆盖不兼容预览、迟到响应及原生模式单层标题布局。`test_kicad_busy_guard.py` 验证短暂无响应后恢复和持续忙碌时不发送保存/关闭。系统编辑菜单命令过滤由 `test_kicad_selection_plugin.py` 覆盖；原生窗口探测验证真实 WebView2 的编辑菜单/快捷键设置及处理器已安装，不代表操作系统右键点击、剪贴板端到端测试。

分组封装误差回归：`test_footprint_geometry.py` 覆盖单焊盘 0.65000/0.65001 mm、20 纳米绝对欧氏距离边界及编号/非有限值拒绝；`test_footprint_choices.py` 覆盖转换、独立改名和转换页改名，两种基准选择及超限时不发布。`test_footprint_alignment_integration.py` 使用真实 pcbnew 覆盖正背面、非整数放置、旋转和 10 纳米历史差异。`run_native_grouped_footprint_probe.py --report-dir <工程外绝对目录>` 使用桌面 Python/kipy、真实临时 KiCad 工程验证多位号改名/转换、同步原理图、PCB 更新及撤销；仅上游转换输入为合成夹具，不代表真实商城联网转换。复用原生标准库探测的正背面、网络、走线、保存重开和自动清理验证。

封装窗口生命周期回归：`test_tool_windows.py` 覆盖跨行独立窗口、同一行复用、关闭重开及过期桥接；`run_tool_windows_smoke.py` 覆盖加载失败可重试、加载中取消及迟到结果不恢复预览。`run_native_tool_windows_probe.py --report-dir <工程外目录>` 使用真实 WebView2 和合成库，实际应用库封装、改名、前进后退、Esc、多行窗口及主窗口联动关闭后台；不调用生产 CAD，不代表真实商城联网转换或 KiCad PCB 更新测试。

撤回/关闭回归：`test_workbench_undo.py` 验证跨同步旧快照回退与新编辑基线，`test_row_footprint.py` 验证真实行改名后只撤回封装记录、不改 C 编码或 CAD；`test_tool_desktop_ui.py` 覆盖十种子窗口 Esc、未保存保护、输入法及内层对话框，`test_footprint_overlay_ui.py` 的 escape 场景验证只关闭最上层及对比返回预检。`run_tool_windows_smoke.py` 还验证聚焦 C 编码时刷新改名不会误发保存请求。

更新回归包含 `test_tool_windows.py` 的网络阻塞并发场景（工具可打开，退出和重复更新受保护），以及 `test_plugin_updates.py` 的 TLS 中断重试、证书拒绝、超时提示。真实 WebView2 的 `run_native_tool_windows_probe.py` 还通过实际桥接挂起更新检查并打开设置窗口；网络延迟为受控测试，不代表真实下载验证。

`test_design_status.py` 使用一次性工程验证原理图无差异但 PCB 未完成、同名几何待核验、预检受阻、保存结果未知、重启恢复、目标库/实例变化使证据失效、属性映射、缺失/歧义/损坏 PCB，以及 Chromium 主表筛选、组数/器件计数和局部刷新保持焦点。主表读取不调用原生 IPC、不写 CAD 或确认状态；`run_native_design_keep_open.py --with-footprints` 另验证真实同步保存后对应待同步状态消除。

## 运行

从独立插件仓库根目录执行：

```powershell
python -m pip install -r tools/requirements.txt pytest
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = Join-Path $PWD 'tools'
python -B -m pytest tests -p no:cacheprovider
```

Node.js 用于部分前端行为检查；涉及 KiCad CLI/pcbnew、WebView2 或转换器的检查需对应运行环境。报告实际失败、跳过及缺失依赖，不把跳过当成验证通过。仅 unittest discovery 不覆盖所有 pytest 测试。

| 测试类别 | 范围 |
| --- | --- |
| `test_workbench_undo.py`、`run_workbench_undo_smoke.py` | 未同步编辑链快照、重开保持、旧记录回退、同步后失效、过期预览及缺位号保护；Chromium 取消/确认、自动保存队列、选料和确认状态恢复、桌面/窄屏边界。仅用一次性工程，不回写生产 CAD |
| `test_supply_column.py`、`run_supply_column_smoke.py` | 合并主料/供货列、旧列布局及默认配置迁移；Chromium 编码自动保存/清空/重开、库存刷新保持输入焦点、订货渠道链接及桌面/窄屏控件边界。使用合成工程与本地库存响应，不联网或写生产 CAD |
| `test_stock_delivery.py` | 总库存不推成立即发货、明确交期优先、分批发货量、立即发货为零与未知、国内页面字段保留、候选保存和旧缓存只读兼容；主表可见交期与 HTML 转义。`run_supply_column_smoke.py` 同时覆盖桌面/窄屏交期直接显示、库存零与未知 |
| `test_row_footprint.py`、`run_inventory_footprint_smoke.py` | 独立行封装预览/应用、无编码改名、确认状态、过期/取消/转换失败隔离、同名冲突及事务回滚；合并库存的未知与零值、同编码请求合并、跨行并行和迟到结果保护。使用可清理合成工程，不写生产 CAD |
| `test_part_selection_specs.py`、`test_spec_check_integration.py`、`run_spec_checks_smoke.py` | 阻容感值等价、限制信息、可靠尺寸识别、缺失信息及只读 API 与原理图基线；Chromium 警告弹窗、迟到响应、字段编辑和实时库存反馈 |
| `test_row_filters.py`、`run_row_filters_smoke.py` | 主表字段/页面多选组合、条件保存和清除；筛选不减少共享库存总需求；Chromium 桌面/窄屏、隐藏列、存储失败及候选窗口隔离 |
| `test_part_selection_inventory.py`、`run_local_inventory_smoke.py` | 汇总列优先、去重/冲突、待匹配采购编码、确认令牌与变更检测、原子失败保护、共享库存/NC/套数、路径迁移、HTTP 与真实 Chromium 导入/持久化/列设置；只用合成表格及一次性工程 |
| `test_auto_column_widths.py`、`test_workbench_toolbar.py` | 自动列宽按钮位置、列宽计算与持久化，以及整页生成 JavaScript 的语法校验（包括 Python 字符串转义） |
| `test_workbench_interactions.py` | 初始化 HTTP/JSON/空来源失败及重试、未加载/切换来源时按钮保护、打包模块独立进程导入（防止测试顺序掩盖依赖问题） |
| `test_candidate_*`、`test_progressive_search.py`、`test_part_stock_lookup.py` | 搜索、筛选、库存、订货与请求竞态 |
| `test_product_details.py`、`test_candidate_parameter_completion.py`、`run_candidate_parameter_completion_smoke.py` | 商品详情页完整属性、身份/来源隔离、固定域名、大小/缓存/并发边界；搜索和库存刷新保留同料号已知参数；Chromium 后台补取、迟到结果、失败重试、温度系数/工作电压筛选及保存。默认合成来源，不修改生产 CAD/记录 |
| `test_*selection*`、`test_selected_schematic_sync.py` | 配置、主料一致性、记录及同步范围 |
| `test_design_sync.py`、`test_design_source.py` | 同步预检参数与主表一致、同编码缓存及已保存参数优先级、冻结快照与过期检查；一次性原理图实际写入/回读、缺失或映射参数字段、同步后差异消除、确认状态保留及重复同步不操作编辑器 |
| `test_path_settings.py`、`test_part_selection_overrides.py` | 更换型号网页搜索偏好、确认时间戳持久化及路径设置 |
| `test_field_mapping.py` | 中英文自动识别、标准字段优先、手动映射持久化、冲突拒绝、缺失属性隐藏创建、单行/所选同步与内置 Value/Footprint 镜像校验；仅使用合成工程 |
| `test_chinese_data_paths.py` | 中文与空格路径下的保存/加载、中文导出、隔离工程同步、旧标准配置迁移、外部备份、冲突及失败回滚；不操作生产工程 |
| `test_footprint_choices.py`、`test_part_selection_library.py` | 封装选择、仅改名按铜皮焊盘布局匹配并提示丝印差异、同名暂停保存、显式另存/令牌授权覆盖、目标变化与备份、覆盖失败回滚 |
| `test_footprint_geometry.py`、`test_footprint_alignment_integration.py` | 编号焊盘刚性配准、中心对齐及间距差异提示、圆/方焊盘差异保留、PCB 实例基准/正反面/旋转还原、原理图已换而 PCB 未换、转换与仅改名的实际焊盘坐标不变、显式旧库恢复、真实 pcbnew 几何变换、图层渲染、库发布及 Chromium SVG 像素检查；使用合成 PCB，不写生产 CAD |
| `test_footprint_overlay_ui.py`、`run_footprint_overlay_smoke.py` | Chromium 同比例叠加/并排、独立图层与不透明度、原点/1号焊盘、迟到响应、仅改名快捷路径、同名覆盖警告/另存/取消及桌面和窄屏边界；候选保存重试链另见 test_candidate_selection.py |
| `test_kicad_*`、`test_start_part_selection_tool.py` | 桌面/安装入口、工程归属和忙碌保护 |
| `test_desktop_lifecycle.py` | 退出授权、并发保护、真实宿主进程退出监视及前端保存失败/重试 |
| `test_plugin_updates.py` | 正式发布清单、匿名额度规避与检查缓存、403/429 等待持久化/过期/损坏缓存、已校验包保留、版本/哈希/ZIP 安全、转换器保留、安装回滚及多宿主保护 |
| `test_test_artifacts.py` | 临时产物清理 |
| `run_*_smoke.py` | 按需真实窗口、安装包或联网转换冒烟 |

电感回归还覆盖 `RMS Current` / `Sat Current`（以及旧版 `Rated Current` / `Saturation Current`）的数字和 `not specified` 标签：只解析感值，不把电流当成另一个感值；错误电流单位和将电流标签用于电容不判一致。候选搜索回归覆盖前后端普通电感的简洁搜索及特例型号搜索。

位号输入专项（Windows PowerShell 5.1）：

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
python -B -m pytest tests/test_kicad_search_text.py tests/test_kicad_busy_guard.py tests/test_kicad_manager_open.py tests/test_kicad_save_fallback.py -p no:cacheprovider
```

`test_kicad_search_text.py` 仅加载搜索脚本函数，模拟 Win32/UIA 边界并注入 `C103 → C03`、读写超时、非目标焦点和不支持的控件等故障；另用一次性隐藏原生 Edit 控件验证实际消息写入、完整值读取与文本变化通知。位号写入不经过 SendKeys 或剪贴板，搜索面板不再重复键入已校验的值；无法读回完整值时停止跳转。该专项不启动 KiCad、不保存或关闭真实原理图，也不能替代实际 KiCad/wx 控件、输入法环境和搜索结果刷新的人工冒烟。

## 隔离与清理

导航专项：

```powershell
python -B -m pytest tests/test_kicad_navigation_preferences.py tests/test_path_settings.py -p no:cacheprovider
```

上述测试模拟 Win32 边界，确认仅最小化时还原、默认恢复鼠标及留在原理图模式、设置校验与即时生效，不操作用户编辑器。行编辑 Chromium 冒烟另覆盖自动保存差异、连续修改与回改、Enter 反馈一致及确认料不重建当前行；设置冒烟覆盖鼠标选项取消、保存和重开保持。

```python
from pathlib import Path
from _scratch import temporary_project

with temporary_project(prefix="example_") as directory:
    project = Path(directory)
    # Only disposable fixtures belong here.
```

pytest 可使用 `conftest.py` 的 `scratch_project`；unittest 在 setUp 中立即注册 `self.addCleanup(tmp.cleanup)`。临时工程及它生成的同级运行缓存均在外部容器，正常结束、断言失败或异常展开时清理。其他 TemporaryDirectory/tmp_path 也须满足相同边界。强杀或文件占用可能留下残余，后续核对绝对路径再清理。

临时 ZIP、解压文件、截图、报告、下载、字节码和测试缓存不得写入源码或正式工程，不得提交。稳定夹具应最小化并说明用途，禁止为测试复制真实工程或其 `.git`。

## 真实冒烟

原生流程夹具使用同盘短路径的一次性工程，避免 KiCad 管理器将长路径省略后无法核实工程归属。PCB 实例配准回归同时覆盖更新后附带的 `sheetname` / `sheetfile` 元数据，不把这些字段误判为不支持的几何。

`run_native_footprint_workflow.py` 为真实工作台和 KiCad 更新流程创建可清理的双器件工程：联网调用已安装转换器，兼容平铺及分单元符号引脚，先用 KiCad CLI 导出网表验证每个引脚网络再建立 PCB 基线。给原封装加入非零局部原点和 90 度旋转，PCB 包含正面 37 度电阻、背面 90 度芯片及各焊盘走线。只用于 Windows、KiCad 10 和已安装插件；报告目录必须在仓库和正式工程外。它不是自动通过的 pytest，也不模拟 KiCad 的更新操作。

```powershell
python -B tests/run_native_footprint_workflow.py --report-dir C:/external/footprint-workflow
# 保留该进程。另一个终端启动同一临时工程的工程管理器及编辑器：
python -B tests/run_native_footprint_workflow.py --launch-session C:/external/footprint-workflow/session.json
# 在真实工作台中选择“仅改名”，同步原理图，再用 KiCad“从原理图更新 PCB”并保存。
python -B tests/run_native_footprint_workflow.py --verify-session C:/external/footprint-workflow/session.json --stage rename
# 再实际选择“替换立创封装”，同步、更新并保存 PCB 后：
python -B tests/run_native_footprint_workflow.py --verify-session C:/external/footprint-workflow/session.json --stage converted
```

可通过 Playwright CDP 操作实际 WebView2 工作台，通过 KiCad IPC 触发更新和保存；原生更新确认不由该脚本替代。校验会拒绝未真正更换封装的 PCB，检查 PCB 库标识与原理图一致、器件原点/角度/正反面/UUID 路径不变、逐编号焊盘世界坐标偏差不超过 5 nm、网络及所有原有走线不变。工程管理器启动使用外部隔离配置和独立 IPC，保留用户全局设置。验证完成先正常关闭所属工作台和测试 KiCad 窗口，再在保留的进程输入 `finish` 清理；不要对生产编辑器发退出命令。只有两阶段均实际更新并通过校验，才可描述为端到端通过。

`python -B tests/run_footprint_overlay_smoke.py` 使用合成比较接口，在真实 Chromium 中检查预览图层、叠加/并排切换、缓存和过期响应、关闭清理及 1440/390 像素布局，截图在工程外运行缓存。`navigation` 场景还覆盖资料未返回时先显示窗口、同窗前进后退、搜索和改名保持、转换失败不阻塞改名、关闭后迟到响应不复活、读取失败重试。`test_footprint_choices.py` 覆盖库对比按原坐标渲染、双源缓存及源文件变更校验。Node.js 需能解析 Playwright，可用 `NODE_PATH` 指向已安装依赖。完整 pytest 在依赖缺失时明确跳过对应浏览器检查；显式冒烟缺少依赖会失败。`test_footprint_geometry.py` 与 `test_footprint_alignment_integration.py` 自动寻找 KiCad CLI 相邻或 Windows KiCad 安装目录的 Python，存在时执行真实 pcbnew 变换和渲染，不存在时明确跳过。集成测试仅模拟联网转换输入，使用真实几何配准/库发布/Chromium SVG 像素校验；不声明真实联网转换或生产 PCB 更新已验证。

`python -B tests/run_plugin_updates_smoke.py` 用真实 Chromium 驱动实际更新面板脚本，原生桥接为合成模拟：检查模式保存失败回退、已校验包不重复下载、独立下载后稍后重启、下载失败/重试、忙碌时关闭重开、源码模式不误报最新、自动下载不强制退出、未保存工程设置阻止重启、更新内容不执行 HTML。无后端、GitHub 请求或安装操作，不调用真实 KiCad；可设置工程外绝对目录 `PLUGIN_UPDATE_SCREENSHOTS` 留存桌面/390 像素宽截图。运行环境同其他 Node Playwright 脚本。

`python -B tests/run_workbench_interactions_smoke.py` 使用 Chromium 与自动清理的合成工程，验证菜单方向键/Home/End/Enter/空格/Tab/Esc、执行后焦点恢复、BOM 原生下拉框、启动 503 失败后重新读取、空筛选提示与清除、隐藏行不计入所选同步且取消筛选后勾选保留。可设置工程外已存在的 `WORKBENCH_AUDIT_SCREENSHOTS` 目录保存桌面和 800 像素宽截图；Node 需要能解析 playwright（可设置 NODE_PATH）。不访问商城、不调用真实同步、不打开用户 KiCad；localhost 仅为隔离测试传输。

`python -B tests/run_directed_row_merge_smoke.py` 在 Chromium 中分别以自动保存和 Enter 将 `4.7nF` 并入已有 `4.7nF 50V` 目标行，验证目标 C 编码、参数、备选与确认状态保留、待同步差异正确及重开持久化。仅使用 `test_directed_row_merge.py` 的一次性合成工程，不写真实工程、不执行 KiCad 同步；单元回归另覆盖 Comment/封装、反向位号、订货链接、目标不唯一和无目标场景。

`python -B tests/run_candidate_search_smoke.py` 使用真实 Chromium 和一次性合成工程，模拟上游分阶段响应，验证 600 ms 自动搜索、输入法组合输入、Enter 立即搜索、清空失效，以及查询中不误报无结果、部分返回、补充来源、最终空结果与失败、筛选交互和 1475 / 800 / 390 像素宽度的状态栏布局。可设置工程外目录 `CANDIDATE_SCREENSHOTS` 保存截图。此测试不访问真实商城、不选择主料、不改生产记录，不代表真实网络时延验证。

`python -B tests/run_candidate_parameter_completion_smoke.py` 使用真实 Chromium 与合成列表/详情响应，验证首屏不等待详情、不误报厂商缺参、旧查询迟到参数不串料、完整参数筛选、失败重试、选用后保存及 CAD 保持不变。截图存到工程外运行缓存，Node 需能解析 playwright（可设置 NODE_PATH）。真实商城读取需另行只读验证，不能把此离线测试当作联网验证。

生命周期冒烟还注入退出脚本缺失、列表未加载的场景，验证空白窗口可正常关闭；失败时输出页面地址、加载状态、行数及桥接可用性。测试子进程固定 UTF-8，避免 Windows 输出解码错误掩盖实际故障。

安装桌面依赖后运行 `python -B tests/run_desktop_lifecycle_smoke.py`，或通过 `--python "C:/path/to/desktop-runtime/Scripts/python.exe"` 指定已准备好依赖的解释器。它在外部临时合成工程打开隐藏的真实 WebView2 窗口，验证原生叉号触发行编辑保存、所属后台端口释放、重开数据保持、菜单重启及退出；正常清理测试宿主和工程，不触碰生产服务或 CAD。它不验证实际 KiCad 插件按钮、网络转换或真实工程同步。

`python -B tests/run_row_editing_smoke.py` 使用 Node Playwright 启动无头 Chromium，在自动清理的合成工程验证页面多选合行、字段/备注/C 编码自动保存、无 Enter 的自动分组/合行、输入法事件、失败重试和请求期间回改。Node 应能解析已安装的 playwright 包（可设置 NODE_PATH）；不对生产工程写入，也不执行真实 KiCad 保存关闭。

`test_path_settings.py` 持续回归路径默认值、转换器检测、原子保存、版本冲突、复制保留、目标冲突、失败回滚及启动来源；`python -B tests/run_path_settings_smoke.py` 用真实 Chromium 和临时工程验证设置菜单、默认折叠、取消、浏览按钮、保存前迁移确认、显式单次 BOM 导入、原理图启动默认值及桌面/窄屏布局。可设置工程外绝对目录 `PATH_SETTINGS_SCREENSHOTS` 留存截图。浏览选择接口在浏览器测试中模拟，不会弹出真实 Windows 对话框，不代表已验证 WebView2 或 KiCad 原生窗口。

查看 `python -B tests/run_kicad_plugin_smoke.py --help` 和 `python -B tests/run_easykiconverter_smoke.py --help` 后按需执行，只使用自动清理的隔离工程。桌面冒烟另需 `tools/kicad_plugin/desktop-requirements.txt` 与 `jsonschema`，部分步骤会联网。可复用的依赖运行时允许保留在外部缓存，并与一次性测试结果区分。

单元测试、打包和模拟 loader 检查不能证明实际 KiCad 按钮、窗口交互、网络转换或跨机器兼容已通过；本说明不声明这些真实冒烟已执行。

`test_value_parameters.py` 回归阻容感参数选择、单位标签、缺失/冲突、同类替换和特殊限制保留；`test_part_selection_specs.py` 包含电阻 ppm 温漂尾缀的保守解析。`python -B tests/run_value_parameters_smoke.py` 用真实 Chromium 和自动清理的合成工程测试箭头、勾选预览、取消/应用、自动保存及重开、过期拒绝、确认状态和 C 编码保留，以及 1440/768/390 像素布局。截图在工程外缓存；不访问商城、不同步或修改生产 CAD，不代表真实 KiCad 更新验证。

`test_candidate_native_window.py` 回归独立候选窗口的复用、地址/参数限制、桥接最小权限、保存忙碌拒绝退出、退出失败恢复和双窗清理。`python -B tests/run_candidate_window_smoke.py` 用 Chromium/合成商城响应验证候选独立页、父页桥接、收起后搜索筛选保持、不重复查询、保存回传、失败恢复、空白点击及拖选越界。可用工程外绝对路径 `CANDIDATE_WINDOW_SCREENSHOT` 保存截图。

封装管理的 `pane-radios` 与 `rename-retry` 浏览器回归分别检查缓存页单选组隔离、原封装资料失败后的就地重试且保留改名草稿。`run_inventory_footprint_smoke.py` 在一次性工程通过真实 KiCad 渲染核验改名、现有库选用、转换封装三条独立保存路径；上游转换输入为合成夹具，不将它表述为联网或 PCB 更新验证。

`run_desktop_lifecycle_smoke.py` 还验证真实 WebView2 独立窗口移到父窗外、收起恢复、主料保存后刷新父页、忙碌拒绝关闭和退出销毁。`--only-candidate` 可只运行此阶段；商城响应在子页模拟、正式保存仅写一次性工程，不打开真实商城或修改生产 CAD。这验证原生窗口程序移动，不等同于人工跨屏拖动或真实 KiCad 按钮测试。

## 标准库原位更新 PCB

库存断点查询回归：`test_stock_refresh.py` 覆盖有效期、去重与旧项优先、暂停/重建/继续、崩溃恢复、料号移除、失败旧缓存、跨任务增量游标、独立取消域、4 线程上限、写进度失败及国内/国际并行；`test_stock_delivery.py` 验证行内时间、过期边界和失败标签。`python -B tests/run_stock_refresh_smoke.py` 使用真实 Chromium、HTTP 和合成上游，执行开始/暂停/磁盘重建后台队列/重载/继续，核验近期及已完成项不重查、1440/768/390 布局和生产 CAD 不写入。可设工程外绝对目录 `STOCK_SCREENSHOT_DIR` 留图，Node 需解析 Playwright。此测试不是商城联网速度或原生 KiCad 写入验证；真实 WebView2 退出另跑 `run_desktop_lifecycle_smoke.py --lifecycle-only`。

`run_native_project_cycle_probe.py --report-dir <工程外绝对目录>` 保留旧分阶段接口的完整关闭重开回归，使用可清理的长路径合成工程，不接受生产工程。`run_native_design_keep_open.py --report-dir <工程外绝对目录>` 验证新的统一同步：23 件真实预检、仅实际变化器件提交、原理图一致时不重写、PCB 原生保存和全盘核验后保留同一会话与管理器，检查位置、角度、网络及目录/进程清理。两者均使用桌面运行时，报告在工程外；不将离线模拟称为实际生产同步。

`test_design_cycle.py`、`test_design_source.py`、`test_kicad_manager_open.py`、`test_kicad_save_fallback.py` 另覆盖保存失败不关闭、关闭失败保留已保存证据、重开核验不符、格式化/器件改动区分、截断路径/无登记/登记不符，以及单次原生保存请求及干净状态回读。以下“不自动保存、单步撤销”历史原生探测记录针对底层事务适配器；当前用户同步流程另有自动保存和重开，不能将旧脚本原样成功视作新流程通过。

`test_design_sync.py`、`test_design_source.py`、`test_footprint_change_types.py` 覆盖分阶段目标/范围、源版本、仅改名证据、字段更新、失败及 PCB 单独重试；`run_design_sync_smoke.py` 检查 B 菜单及确认流程。`run_tool_windows_smoke.py` 检查固定工具页、草稿保护及父表局部刷新；`run_native_tool_windows_probe.py --report-dir <工程外目录>` 用真实 WebView2 检查工具窗口和受限桥接，不是 KiCad 写入测试。

`run_native_design_sync_probe.py --report-dir <工程外目录>` 是上述全工程闭环探测的兼容入口。原来的不保存/单步撤销与“弹窗后继续 PCB”假设不适用于新自动保存关闭流程，不再沿用旧成功文案或旧进程连接验证新流程。`run_native_pcb_in_place_probe.py` 保留底层事务检查：Chromium 实际选库，随后测试显式安全暂关原理图、同步字段及调用不保存适配器；不宣称这覆盖完整同步菜单编排或真实联网转换。

2026-10-23 验证记录：1,169 项 pytest 通过；B 菜单 Chromium 检查覆盖 1440/768/390 像素，工具窗口检查覆盖草稿保护及未改行 DOM/焦点保持。真实 WebView2 覆盖 10 类窗口、移出主窗口及受限桥接。真实 KiCad `10.0.0-19-g65df3ab11c` 完整探测通过字段、仅改名、原生弹窗受阻后的 PCB 单独重试、正背面严格/中心对齐、重复预检不重复换封装、网络/走线、单步撤销、自动打开、保存重开，以及进程和目录清理。该记录不代表真实联网转换、生产工程更新或其他 KiCad 版本验证。

`test_pcb_native_types.py` 的对象顺序回归覆盖字段、焊盘、图形混排，以及字段值/几何/网络/UUID/位置/元数据/重复对象数量变化。只允许顶层封装对象顺序变化，不能以此放宽完整内容校验。真实探测夹具包含 `LCSC` 和 `Comment` 自定义 PCB 字段，覆盖原生暂存类型重排和回读。

重开核验另覆盖实测 1 nm 坐标舍入及七位小数角度格式化，含 Any 内焊盘、自定义文字和多边形点；超过 1 nm、非格式化角度、尺寸、UUID、网络、字段、重复数量及未知 protobuf 字段变化均拒绝。此兼容仅用于重开，不改变实时暂存/发布后的严格比较。

2026-10-24 原生验证记录（KiCad `10.0.0-19-g65df3ab11c`）：新全工程探测通过管理器单独打开/已关闭、长路径窄窗口、旧连接 WebView2 入口不弹选择器、原理图单独同步、此前脏 PCB 保留、字段/改名/标准库替换、PCB 单独同步、自动保存/关闭/管理器重开和首个位号搜索执行。分组适配器探测另通过 10 nm 历史偏差、多实例改名/合成转换、正背面严格/中心对齐、单步撤销、保存重开；两套进程与目录清理均通过。全工程探测使用正式后台编排，Chromium 另验证实际选库，未宣称全套原生同步菜单鼠标操作、独立视觉定位验证、真实联网转换、生产工程写入或其他 KiCad 版本通过。

中心对齐另由 `test_pcb_in_place_geometry.py` 覆盖 L9 同型间距差（3.6068 / 3.048 mm）、中心不动、逐焊盘移动 0.2794 mm、正背面及编号拒绝；`run_pcb_updates_smoke.py` 覆盖默认中心模式、严格模式失败、切换清除确认和迟到响应隔离。真实探测在严格流程后运行不同间距标准库的正背面中心更新，独立核验中心、实际移动量、目标几何、网络、走线不动、单步撤销与保存重开。两种模式的“位置保持”含义不同，不能把中心对齐写成每颗焊盘原位。

`test_pcb_session.py` 另覆盖从原理图入口按需连接 PCB、PCB 晚于工作台打开、多工程/同工程重复编辑器、独立服务端令牌及本机接口发现。真实探测脚本的工作台阶段不传入 PCB 启动接口，由正式连接逻辑发现并核验一次性工程的实时 PCB。

`test_pcb_connection.py` 另覆盖经工程管理器启动、启动等待不重复 CIM 扫描、正常关闭后再开与连接超时后的防重复启动。`test_design_sync.py` 验证原生连接等待期间异步预检可轮询及阶段计时；两个 Chromium 同步测试覆盖动态进度、耗时展开和迟到预检响应。真实探测使用正式管理器入口打开 PCB，保存后只关闭 PCB，再由正式连接逻辑复用同一管理器重开，核验编辑器归属及进度事件；不会以测试脚本直接启动 pcbnew.exe 代替此项验收。

管理器专项回归还覆盖身份不明和同工程多管理器拒绝自动打开；超时后仅在重新完整核实目标已关闭时允许重试。只读预检支持取消排队和合作式停止，不允许用预检取消接口中断写入事务；浏览器关闭/切换对齐方式/迟到启动响应都需取消旧任务。统一同步每 25 件更新同一计数阶段，不为无变化器件堆叠进度。最近已核验连接的短暂繁忙仅限时只读重连，预算传至各地址；超时或忙碌会话不能被漏算为唯一连接。

`run_native_design_keep_open.py --with-footprints --report-dir <工程外绝对目录>` 还实际通过行封装接口选库改名、标准库替换并同步原理图和 PCB，核对焊盘、网络、姿态及事务外对象，重复同步应无变化。跨原理图安全重载时 KiCad 可能有 1 nm 舍入，事务外对象采用重载后的事务基线严格比较，不放宽实际写入或保存核验。

该探测还在一次性工程中创建闭合多边形，向实际 KiCad PCB 事务提交含重复闭合末点的字段更新，核验原生返回去掉冗余末点后仍可完整回读、保存且同会话保留；生产失败记录只在内存只读回放。`test_pcb_native_types.py` 覆盖轮廓、孔洞、自定义焊盘的等价闭合，以及 1 nm 真变动、未知属性、开放轮廓和圆弧端点拒绝；`test_pcb_transaction.py` 核验错误位号诊断不缩小整批撤回范围。`test_design_sync_ui.py` 使用离线 Chromium 验证核对 1246 个器件、原理图 2 个差异、PCB 113 个差异独立计数，自动同步失败后保留实际清单并刷新已完成的原理图阶段。

`test_pcb_schematic_identity.py` 覆盖复制工程遗留根 UUID、PCB 省略根 UUID、实际 Sheetfile 层级（含子目录）、无关页面/错误页面链/其他工程实例拒绝、缺页和循环诊断。真实 PCB 探测夹具采用三级页面、历史根实例记录和省略根 UUID 的 PCB 路径，避免只测手工拼出的平面根路径。

`test_pcb_in_place_geometry.py`、`test_pcb_session.py`、`test_pcb_transaction.py`、`test_pcb_update.py` 与 `test_pcb_updates_ui.py` 覆盖严格编号配准、工程身份、批次边界、备份失败、暂存/回读失败、未知结果不重放、源变化及确认状态隔离。`run_pcb_updates_smoke.py` 为可复用离线 Chromium 交互检查；设置 `PCB_SCREENSHOT` 为工程外绝对文件名前缀可保留截图。

`run_native_pcb_in_place_probe.py --report-dir <工程外绝对目录>` 是明确选择运行的真实 KiCad 验证，使用已安装桌面 Python 环境的 `kipy`、KiCad 捆绑 Python 和 Node Playwright（需配置 `NODE_PATH`）。创建并自动清理合成工程，不接受生产工程路径；验证正面任意角度、背面、偏移原点、真实标准库几何替换、网络与走线不变、此前未保存修改、一个撤销步骤，以及显式保存重开。包含真实工作台选库、原理图保存关闭/恢复、分阶段同步及 PCB 更新流程。报告和截图在指定工程外目录，不能把离线浏览器模拟或接口存在误称为真实流程通过。
