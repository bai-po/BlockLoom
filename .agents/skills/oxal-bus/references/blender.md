# Blender 接入、生命周期与 Undo

实现范围：当前 EMT 自动调度与原生 Undo 适配。核心修改权限统一见 [AGENTS.md](../../../../AGENTS.md#核心修改权限先于业务任务执行)；业务执行者按下述现有接口接入，不自行改变调度或添加另一套保护层。

## 分层

| 层 | 职责 |
| --- | --- |
| UI | 绘制及显示状态；draw 不发送业务请求或修改场景 |
| 提交 Operator | 捕获当次参数/目标标识并入队，不调用 Tool；`FINISHED` 表示提交成功，业务结果看后续回复 |
| 主线程 timer | 安全时调用 `Bus.pump()`，交付已有回复并推进请求；不自发生成业务任务 |
| 业务模块 | 登记、校验、协调执行和维护轻量回调状态 |
| 业务 Tool | 实现算法、当下验证目标，不依赖 UI 或 Bus |
| Blender 执行适配器 | 为延后修改建立 Undo，提供可重放参数 |
| core | 共用通信、调度及必要适配，不混入具体资源逻辑 |

队列用场景 UID 等运行期标识，不保存 Scene。`session_uid` 不作为跨保存文件的资源身份。
游标/枚举等“点击当时”的值先快照，执行时检查目标删除、场景变化或只读状态。
结果使用普通值、名称和业务标识；回调不保留失效 RNA。

## 生命周期

入口见 [根 __init__.py](../../../../__init__.py)、[module/__init__.py](../../../../module/__init__.py)、[runtime.py](../../../../core/runtime.py)。

- `runtime.register()` 创建 Bus，随后登记业务、Operator/UI，最后 `runtime.start()` 安装调度。
- `runtime.stop()` 先移除调度并丢弃旧请求，逆序注销业务/UI，再由 `runtime.unregister()` 关闭 Bus。
- 启动失败逆序回滚已完成阶段，不误注销未成功登记或属于其它插件的类。
- 总入口保留已启用插件身份冲突检查，避免同一插件的用户插件与扩展实例同时登记。业务登记按自身拥有的类和能力管理，不覆盖或注销来源不明的登记；runtime 不维护具体业务 RNA 名称清单。
- runtime 持久 timer 跨文件保留，`load_pre` 丢弃旧请求。额外端点同步清理自身状态、请求映射和临时调用记录。
- 重载先用旧版本注销，再卸载目标模块前缀并启用新版本；确认旧 timer 移除，新 timer/Topic 就绪。
- 登记及登记失败回滚必须兼容 Blender 真实扩展启用的 `RestrictBlend`：只定义类型、属性、能力和轻量调度，不访问 `bpy.data.scenes/window_managers` 等场景实例。不得换入口绕过受限环境。UI 回滚只撤销已确认由本模块成功登记的 RNA，不以遍历数据或清空用户 payload 为前提。
- 使用自定义 Scene RNA 的原生驱动，须分别验证先启用后打开文件、已有工程打开后启用、停用期间求值后再次启用。后两种情况可能保留 Blender 的旧 RNA 解析缓存；已有天空由用户明确提交的加载/激活任务核验精确归属并恢复输入绑定，登记、帧/依赖图回调、draw 或独立 timer 不自发扫描修复。恢复失败须保留原值、字段存在性、槽位及 World。正常保存重开不能代替冷启用测试。

当前 timer 约每 20ms 自动调用 `pump()`，每轮预算 4ms、最多 32 个步骤，先交付已有回复，再推进任务；普通函数一次完成，生成器每轮至多推进一次。渲染或界面被作业锁定时暂停。预算不能抢占正在执行的 Python/native 调用，也不是单次业务、导入或界面响应耗时保证。

### 渲染屏障

- `runtime.mutation_allowed()` 检查本插件渲染阶段、Blender `RENDER` 作业、界面锁、待取消状态及停用等待；`ensure_mutation_allowed()` 在不可修改时抛出 `MutationBlockedError`（`RuntimeError` 子类）。现有提交、Tool 和 Undo 执行入口继续使用这些检查；正常拒绝不作为原生渲染故障记录堆栈。
- `render_init` 只设置渲染与待取消标记，不执行任务、回调、生成器清理或场景修改。`render_complete/render_cancel` 只解除本地渲染标记；原生作业结束前仍暂停，待取消标记保留到安全 tick。
- 首个安全 tick 先清待取消标记，再调用 `cancel_pending()` 取消未完成请求并运行其生成器清理，然后正常 pump。安全 tick 前仍拒绝新的正式提交，避免把渲染结束后的新业务混入待取消批次。
- 渲染期间暂停业务执行、结果回调和生成器清理；未完成旧请求不会重放。已经完成并等待交付的回复保留原结果；取消只处理未完成请求，不能撤销已经完成的业务。
- 原生取消验收须观察真实 `render_cancel`、原生作业结束、队列不重放和随后再次渲染完成。手动调用保护回调或仅注入 `Window.event_simulate` 的 Esc 不构成原生取消证据；使用临时 Render 窗口的原生取消入口时记录实际路线，不把它称为物理 Esc 输入验证。
- 连续动画使用已准备好的原生驱动、节点、约束及关键帧。帧/依赖图回调不自动替换网格、删除场景 ID 或重建业务结构；结构修改通过用户明确提交的 EMT 任务执行。timer 只推进已登记请求，不另设无请求的结构扫描或更新循环。
- 不在渲染中重载或注销 RNA。用户在作业中停用插件时，入口通过 `runtime.request_shutdown()` 登记清理回调并拒绝新的正式提交；首个安全 tick 清除等待标记后执行原注销流程，停止调度、丢弃旧请求/回复并清理任务，随后注销类型与能力。清理不得重新提交业务或重建资源。重载须等停用完成，并核对生命周期登记及场景保留。保护本身不构成稳定性结论，仍须用真实单帧、连续渲染、取消与恢复验证。
插件没有自动分配的独立线程。不要把任意 `bpy` 工作丢进线程池，也不为 Bus 另建事件循环；其它执行后端需单独设计边界。

## 自动调度与原生 Undo

`endpoint.submit()` 只入队并返回请求 ID；提交 Operator 随后结束，由主线程 timer 自动调用 Bus handler。现有 `ImportEndpoint.handle()` 调用拥有 `UNDO` 的原生 apply Operator 执行业务；提交层自身不加 `UNDO`，不直接调用 Tool，也不再经过额外派发 Operator。
内部 apply 的原生调用保留 `"EXEC_DEFAULT", True` 以记录撤销。必须让实际 timer 调度一次业务后验证真实窗口 Undo/Redo，不能只看类标志、手动 pump 或后台执行结果。
内部 apply 保存独立的目标及选项；Redo 不重读 UI 选择、游标或初始样式，同时检查原目标场景、模式、渲染/作业状态与资源归属。上下文敏感操作要求原目标仍为当前原生场景；场景切换则拒绝并提示切回，不能靠部分 Scene/ViewLayer override 冒充正确的活动对象上下文。使用当前合法活动视图层，不强制改到首个视图层。
临时调用记录仅传递本次结果，使用 `finally` 清理。内部 Operator 在记录已移除后仍须能工作，不能依赖旧请求或回调。
业务失败仍由业务事务恢复；Undo、超时和取消都不替代失败清理。

## 接入现有 ImportEndpoint

不修改场景的普通能力直接使用 [API 参考](api.md) 的 register/send。
独立资源导入优先复用 [core/importing.py](../../../../core/importing.py)，避免复制一套消息状态与 Undo 桥：

```python
from ..core.importing import ImportEndpoint
from ..business.example.example import ExampleTool

tool = ExampleTool()
endpoint = ImportEndpoint(
    "example.import", "oxal_mc_studio.apply_resource_import", tool.load
)


def register():
    endpoint.register()


def unregister():
    endpoint.unregister()
```

示例工具需自行实现。现有 loader 契约为 `(scene_uid: int, options: Mapping[str, object]) -> util.resources.ImportResult`。
这个结果类型不强加给纯业务 Topic，也不要求为整理文档改写天空已有专用路径。

| API | 语义 |
| --- | --- |
| `endpoint.submit(scene_uid, options)` | 小型 JSON 可序列化选项，返回请求 ID；拒绝同场景正在处理的重复请求 |
| `endpoint.state(scene_uid)` | 获取轻量展示与计时状态 |
| `endpoint.busy(scene_uid)` | 依据真实 Bus pending 状态判断 |
| `endpoint.register()/unregister()` | 维护能力登记、文件加载清理及端点状态 |
| `endpoint.reset()` | 清理内存状态、请求映射与调用记录 |
| `endpoint.after_execute(result)` | 默认空钩子；handler 中实际 apply 成功后发布 UI 列表/预览，结果随后入回复队列；不在回复回调中重复解码或重建界面列表 |
| `endpoint.after_apply(result)` | 实际 apply 成功（包括原生 Redo）后更新瞬态 UI 目标；只修复选择/状态，不进行资源 IO、预览解码、业务提交或结构修改 |

`handle()`、`apply()`、`receive_result()` 是适配器接线，一般模块复用它们，不从 UI 调用业务处理函数。
`receive_result()` 仅更新与当前 request_id 匹配的轻量状态；旧回复不能覆盖新请求。`after_execute()` 的显示异常记录后不把已经提交的业务事务反称失败，避免重复导入。
普通 `register()` 直接调用仅能验证内部接线；发布前必须按实际用户插件或扩展交付方式，通过 `addon_utils.enable/disable` 验证真实受限启用、重复启停、故障回滚及失败后再次启用，并核对 RNA、Topic、timer 和场景 ID。
现有 `ImportEndpoint`/`CommandEndpoint` 的 loader 必须同步完成并返回普通结果，不返回生成器或添加 timer 延续结构修改。Bus 本身可登记生成器 handler 并自动分步；这不会自动扩展同步 loader 的结果或原生 Undo 契约。
内部 Operator 属性含 `topic`、`request_id`、字符串 `scene_uid` 和 `options_json`，通过 `find_endpoint(topic)` 调用 `apply()`。
属性、原生返回状态和错误传回以 `operation/resources.py` 为准；登记参考 `module/human.py`、`module/inventory.py`。

仍需显式加入 module/operation/ui 生命周期入口；构造端点不会自动登记或启用按钮。
端点目前按场景去重；更细范围须按业务语义设计，不静默合并有意创建多个实例的请求。

端点通过 `allowed_modes` 明确声明模式能力，默认只允许 `OBJECT`；需要从选中槽骨执行的资产命令可声明 `("OBJECT", "POSE")`。处理函数和实际 Undo 重放两入口均校验声明；具体 Tool 仍须验证目标及临时模式切换并恢复原活动对象、选择和模式，不因此开放 Edit 模式或其它模块的能力。

创建独立实例的导入端点声明 `new_instance=True`，在提交阶段通过 `util.resources.prepare_creation_options()` 把 `creation_id` 固定到选项快照，Tool 以 `instance_creation_id()` 校验并创建逻辑实例。混合命令端点仅对创建动作执行同一准备；每次新点击产生不同标识，原生 Redo 复用同一标识，使后续编辑/绑定的历史快照仍能定位原逻辑目标。Tool 必须拒绝实际作用域中的标识冲突，标识本身不授予拥有权；运行时 `session_uid` 不作为跨导入 Undo/Redo 的持久目标身份。

### 通用命令端点

`core.commands.CommandEndpoint(topic, loader)` 复用同一请求状态和 Undo 适配；`loader(scene_uid, options)` 返回非空普通值或数据类，不要求 `ImportResult`。提交、状态、忙碌与启停 API 和 `ImportEndpoint` 相同，内部执行入口为 `oxal_mc_studio.apply_command`，由全局 Operator 生命周期登记。实际修改及失败恢复仍由 Tool 负责。

`ImportEndpoint` 保留导入结果检查；二者共用端点登记与文件切换清理，不维护第二套 Bus 或 Undo 链路。

## 验证与授权

按实际改动验证调度、生命周期和资源接入。自动调度必须观察真实 timer 的执行及延后回复；涉及场景修改须验证真实窗口 Undo/Redo，渲染改动覆盖真实开始、取消、结束与恢复。临时测试及其产物按 AGENTS.md 的 `_test` 后缀规则命名，按需创建、用完清理，不随仓库分发，不假定已有测试文件存在。
不要在用户工作场景跑会增删测试资产的脚本，热加载验证以登记、timer、Topic 和场景快照为主。
本 Skill 不自行授权重启 Blender、改当前文件、导入资产或发送外部消息；实际动作仍依当前任务与用户授权。
