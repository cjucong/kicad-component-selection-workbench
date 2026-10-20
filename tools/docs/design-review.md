# 密集表格工作台 UX 参考

日期：2026-10-21。基线：`8dea141`，修改前工作区干净，`git stash create` 无对象，以此提交为备份基点。已读 `AGENTS.md`，由后台研究代理查阅并交叉核验官方资料。前半部分记录参考和建议，实际复现及测试结果单列于末节；本轮不操作真实 WebView2/KiCad 工程。

## 官方参考事实

以下为实际读取的 4 组一手参考，共 7 个页面的相关章节，访问均返回 HTTP 200。Qt 为在线 Qt 6 文档；WAI-ARIA APG 是交互实践指南，不能单凭遵循示例声称通过无障碍合规认证。

1. **KiCad 9，Symbol Fields Table**：[手册对应章节](https://docs.kicad.org/9.0/en/eeschema/eeschema.html#symbol-fields-table)。单元格支持方向键、Tab/Shift+Tab，Enter 下移；顶部 Filter 按位号过滤，支持 `*`、`?`。字段显隐、分组、显示范围和排除 DNP 分别配置；预设保存字段显隐、分组字段和列序。它没有规定本项目的跨筛选勾选策略或异步请求策略。
2. **Qt，表格与表头**：[QTableView 的 sortingEnabled](https://doc.qt.io/qt-6/qtableview.html#sortingEnabled-prop)、[QHeaderView 的 ResizeMode](https://doc.qt.io/qt-6/qheaderview.html#ResizeMode-enum) 及同页 Detailed Description。QTableView 排序默认关闭，启用后可点击表头排序；QHeaderView 区分可拖动列宽的 `Interactive` 与按内容计算的 `ResizeToContents`。移动列会改变视觉索引，不改变逻辑索引。这些是能力说明，不要求每次保存重新适配列宽。
3. **Qt，源数据与视图分离**：[QSortFilterProxyModel，Detailed Description / Sorting / Filtering](https://doc.qt.io/qt-6/qsortfilterproxymodel.html#details)。排序与过滤通过索引映射改变视图，不必改写底层数据；提供 `mapToSource` 和选择映射。文档明确指出模型 reset 会丢失选择信息并重新填充代理模型；不能据此推断 Qt 自动保留被过滤隐藏的业务勾选。
4. **W3C APG，菜单和模态框**：[Menu Button](https://www.w3.org/WAI/ARIA/apg/patterns/menu-button/#keyboardinteraction)、[Menu and Menubar](https://www.w3.org/WAI/ARIA/apg/patterns/menubar/#keyboardinteraction)、[Dialog (Modal)](https://www.w3.org/WAI/ARIA/apg/patterns/dialog-modal/#keyboardinteraction)，实读 Keyboard Interaction、角色/状态及 Note。菜单按钮用 Enter/Space 打开并聚焦首项，菜单内用方向键移动，Escape 返回调用上下文；按钮的 `aria-expanded` 与开闭状态一致。模态框打开时移入焦点，Tab/Shift+Tab 在内部循环，Escape 关闭；关闭通常返回触发元素，元素已消失时转到合理后续位置。背景实际不可操作才使用模态语义。

## 当前实现边界

静态阅读 [render_index 及内联脚本](../part_selection_server.py)、[row-edits.js](../web/row-edits.js)、[footprint-compare.js](../web/footprint-compare.js) 和 [path-settings.js](../web/path-settings.js) 得到：

- 工具栏已有原理图数据、库存、同步、导出、列表视图分组，本地图标、可访问名称和提示；状态与进度在底部状态栏。保留已批准的紧凑 KiCad 风格，不重排为大按钮或卡片。
- `applyActiveFilter` 在现有行上切换显隐；`selectedRowRefs` 按位号存储，`selectedSyncRefs` 只取当前可见的已选位号，表头全选也只作用于可见行。隐藏选择与实际批量作用范围不是同一概念。
- `loadRows` 已有 `rowsLoadVersion`；候选搜索已有 token、取消和当前关键词校验；库封装加载已有取消、序号校验、Tab 循环和关闭后恢复焦点。应复用并验证，不重复引入第二套机制。

上述是源码观察，不证明真实交互全部正确，也不构成缺陷复现。

## 增量建议

以下均为项目设计判断，不是官方强制要求，按建议实施顺序排列：

1. **先固定范围契约**：沿用“所选同步仅当前可见项”，不静默扩大到隐藏项。在现有状态栏或确认处区分“本次作用 N 行/位号”和“另有隐藏选择 M”，避免增加工具栏高度。筛选只是改变视图，不应修改人工选料记录。[参考 1、3]
2. **保留筛选和列设置结构**：沿用现有搜索、列显隐、列序、默认视图和手动自动列宽入口。候选筛选继续同字段多值并集、跨字段交集；清除条件后恢复原结果。不要因 KiCad 支持位号通配符就改变本项目现有全字段搜索语义。若后续增加表头排序，先覆盖数值、空值及同值稳定次序，按字段键而非视觉列号绑定排序；无需为此迁移 Qt。[参考 1、2、3]
3. **保存时最小更新**：保留输入焦点、光标/选区、当前单元格身份、滚动锚点与用户列宽；仅分组合并等必要情况重绘。确认状态就地更新，Enter 继续使用既有保存队列，不照搬 KiCad 的 Enter 下移。焦点所在行消失时选择合理可见位置，不能落到不可见控件。[参考 1、3；具体策略为项目建议]
4. **验证异步屏障的完整性**：沿用版本号/token；成功、错误、计数和 loading 收尾都只允许当前请求更新。测试切换来源、关键词及关闭/重开弹窗；取消是优化，不代替返回时身份检查。这是并发正确性建议，以上参考未规定网络请求算法。
5. **按控件类型补键盘验收**：命令菜单按 APG 验证打开移焦、方向键、执行与 Escape 返回；含复选框/下拉框的列设置面板不机械改成 `role=menu`。设置与封装弹窗验证初始焦点、循环、嵌套关闭顺序及触发点消失后的回退；不要只添加 ARIA 角色。对普通表格保留原生控件语义，完整实现导航前不强加 `role=grid`。[参考 4；面板分类为项目建议]

## 测试目标

已读 [候选筛选测试](../../tests/test_candidate_filters.py) 和 [工具栏测试](../../tests/test_workbench_toolbar.py)：已有组合筛选、单位大小写、零计数保留、工具栏结构、整页 JS 语法及部分 Node 行为断言。以下为应核对或补充的目标，不是声称这些用例目前全部缺失，更不是已执行结果。

| 重点 | 可执行验收目标 | 合适层级 |
| --- | --- | --- |
| 筛选 | 空结果、清除恢复、缺字段、库存条件与多条件组合；结果计数一致，原数据不变 | 复用现有 Node 纯函数测试；补 1 条真实 DOM 筛选流程 |
| 跨筛选选择 | 选 A/B，过滤隐藏 A；全选/取消只改变可见集合；所选同步 payload 不含隐藏 A；清除过滤后 A 仍选中；分组部分选择呈混合态 | 最小 DOM 状态测试加请求断言，隔离同步端点 |
| 焦点与位置 | 编辑后自动保存及 Enter 均不抢焦点；选区、列宽、锚点不变；需要合并重绘时可合理恢复；筛选框输入不丢焦点 | 真浏览器 DOM 测试，不能用 HTML 子串断言替代 |
| 过期响应 | 可控 Promise 令旧 A 晚于新 B 成功/失败；B 的行、计数、错误及 loading 不被 A 覆盖；关闭/重开后旧响应无效 | 模拟 fetch 的确定性 JS/DOM 测试，不靠 sleep 或真实商城网络 |
| 键盘与布局 | 纯键盘完成菜单和弹窗；检查 activeElement、ARIA 状态、Tab 循环与 Escape 回焦；1366x768、1920x1080 及 125% 缩放下弹层不遮挡关键操作 | 浏览器交互与截图；最后单独做 WebView2/辅助技术冒烟 |

实现时使用合成数据与仓库临时工程夹具，产物放工程外；全套 pytest、真实 WebView2 和 KiCad 验证分别报告。浏览器测试只作隔离测试，不恢复用户浏览器模式；不以源码存在或模拟测试通过代替真实桌面验证。

## 本轮落地与验证

2026-10-21 在上述基线上先运行 `python -B tests/run_workbench_interactions_smoke.py`，确认三组断言失败：方向键未展开菜单；模拟启动 HTTP 503 导致 `Cannot read properties of undefined (reading 'map')` 且没有重试反馈；筛选空态没有明确提示。前两项是复现问题，第三项是本轮新增交互验收。

已落地：菜单键盘导航与回焦（BOM 的 select 保留原生操作）；数据源 HTTP/JSON/空列表错误提示、重新读取重试及未加载时的操作保护；筛选空态与清除条件（不改数据及勾选范围）。专项测试还发现 `package_plugin` 独立导入失败，修复为兼容脚本/包两种入口，并用全新 Python 进程回归，避免导入顺序掩盖问题。

本轮全套 pytest：**475 通过**。实际执行的 Chromium 脚本：`run_workbench_interactions_smoke.py`、`run_row_editing_smoke.py`、`run_candidate_search_smoke.py`、`run_directed_row_merge_smoke.py`、`run_path_settings_smoke.py` 均通过，页面脚本错误为空。查看了 1440 和 800 像素宽度截图；仅菜单/空态等局部调整，不改变现有分组或外观方向。测试工程自动清理，截图位于工程外缓存，未修改实际 CAD 或选料记录。保留已有 Python 字符串转义弃用告警，本轮未扩大为内联脚本重构。

这不是全部建议已完成的声明：真实 WebView2、KiCad 保存/关闭、商城联网转换及辅助技术兼容未在本轮执行；焦点陷阱、更广泛的表格键盘导航等仍属后续核查项。
