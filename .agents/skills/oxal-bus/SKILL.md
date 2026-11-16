---
name: oxal-bus
description: "接入或审查 OXAL MC Studio 的 Topic、EMT、回调、生命周期及 Blender Undo 时使用；只有用户明确授权的核心维护任务可修改 Bus。业务任务仅使用公开接口，资源算法另见资源导入规范。"
---

# OXAL Bus 开发规范

本 Skill 是项目 Bus 规则的唯一维护入口。资源导入等上层规范应链接到这里，不复制接口和接线规则。
适用于项目自有 `core/bus.py`，不等同于 Blender 的 `bpy.msgbus` 或 Heart 的异步运行时。

## 修改权限与实现版本

先读 [AGENTS.md 的核心修改权限](../../../AGENTS.md#核心修改权限先于业务任务执行)。本 Skill 的被选中、名称或 API 示例都不授予核心修改权；业务执行者对整个 `core/` 只读，只能按当前公开接口完成接入。接口不满足时按 AGENTS.md 的固定处理报告，不新增调度器或保护层。

下文及参考文档记录当前自动调度实现：提交只入队，Blender 主线程 timer 推进 Bus，原生 apply Operator 保留业务 Undo。核心基于 `e57cd5bf` 的简洁调度，不含额外派发 Operator、双 gate 或被动回调隔离。业务任务不得自行改动这一契约；后续核心维护必须有明确授权并同步接口文档。

## 开始工作

1. 从当前工作区确认 OXAL MC Studio 根目录，检查 `AGENTS.md` 和实际代码；不要把聊天目录当成插件目录。
2. 使用公开接口或解释语义时读 [references/api.md](references/api.md)。新增 Blender 入口、Undo 或修改启停时读 [references/blender.md](references/blender.md)。
3. 按改动只读相关实现：`core/messages.py`、`core/bus.py`、`core/runtime.py`；资源接入另看 `core/importing.py` 和对应模块。
4. 文档与实现不一致时先核实，不因此自行修改核心。只有受授权的核心维护任务可改变公开契约，并同步本 Skill 的对应参考和有意义的测试。

业务 Topic、payload、结果及 Operator 标识的变更遵循 [业务公开接口与兼容](../oxal-project/SKILL.md#业务公开接口与兼容)；共用辅助代码遵循 [util 的职责与依赖](../oxal-project/SKILL.md#util-的职责与依赖)。路径或接口变更时按 [配套内容同步维护](../oxal-project/SKILL.md#配套内容同步维护) 更新示例、登记说明和开发工具，不复制另一套规则。

本 Skill 随项目位于 `.agents/skills/oxal-bus/`。代码路径以下均相对项目根目录；相对链接以各文件所在目录解析。

## 当前实现约定（自动调度）

- 当前为单线程、定向请求总线：Topic 精确匹配，一个 Topic 一个处理函数。没有全域广播、通配订阅、asyncio 循环或自动目录发现。
- 主机通常是提交请求的 UI Operator；从机是已登记的业务能力。普通函数和绑定方法均可登记，工具类不需要继承 Bus 基类。
- 登记保存函数引用，不执行函数；绑定方法保留所属实例。构造、导入和登记保持轻量，不提前运行实际业务。
- `send()` 保存消息与回调、入队并返回请求 ID，不调用业务函数或回调；业务返回后，回复也在后续调度交付。
- Topic 决定调用谁，payload 传业务参数；点号不提供层级寻址。Operator ID 与 Topic 属于不同登记表。
- Bus 处理函数可直接返回结果，或作为生成器在小批次间 `yield`，由 timer 自动分步推进；总线不会自动拆算法。现有 `ImportEndpoint`/`CommandEndpoint` 的 loader 必须同步返回普通结果，其原生 Undo 适配不接受生成器。
- Bus 方法在同一主线程使用。4ms 预算不能抢占单次函数/native API 调用；延后交付不等于线程池执行或加载加速。
- 回调选模块级函数或生命周期明确的轻量对象；不保留已结束的 Operator、临时 Context 或旧场景 RNA。回调本身也应短小。
- Blender 请求用小型参数快照和可重新验证的目标标识，执行时再获取目标；结果返回普通数据与标识，不把 RNA 放入等待队列。
- 一个任务、回调或生成器清理异常应局部记录，不中断其它请求；失败、超时或取消不代表 Blender 数据自动回滚。

## 生命周期与分层

启用先创建 Bus，再登记能力及 Blender 类，最后启动调度。停用先停止调度并丢弃未完成任务/回调，再逆序注销模块，最后关闭 Bus。
注册失败清理已完成的登记；切换文件丢弃旧请求、回调和关联运行期状态。重载清理旧 timer 和引用，避免双重调度。

runtime 在没有渲染或锁定作业时自动调用 `pump()`，同一轮先交付已有回复，再推进任务。渲染开始只登记暂停和待取消状态；首个安全轮次取消未完成请求并清理生成器，再恢复调度。业务入口保留 `mutation_allowed()`/`ensure_mutation_allowed()` 检查；接线见 [Blender 接入规范](references/blender.md)。

UI 绘制；提交 Operator 做快照并入队；timer 自动推进已登记请求；模块登记、校验、协调及维护轻量回调状态；工具实现实际功能。目录填表和预览解码保留在模块的 `after_execute()` 钩子，回复回调只更新轻量状态，不重复业务执行。
业务算法放在已授权的工具中，不能因“可复用”移动到 `core/` 或修改共用适配器。需要 Undo 的业务接入使用当前公开适配接口，见 [Blender 接入规范](references/blender.md)。

能力须在实际的 module/operation/ui 注册入口中显式接入。模块内生命周期函数由调用者调用，不会因文件名或目录位置自动执行。

## 验证与交接

测试代码、临时演示及其产物统一遵循 [项目测试约定](../oxal-project/SKILL.md#测试代码与产物)，包括 `_test` 命名、Git/打包排除、单向依赖、无测试依赖的交付、实际结果与用后清理。

- 本地测试由独立入口通过公开接口显式登记和注销自己拥有的测试 Topic、回调与 Blender 类。断开时按当前接口的取消与回调语义处理其未完成请求，再移除测试引用；不得注销正式业务 Topic、关闭宿主 Bus 或接管调度。EMT 自动调度不应成为生产代码依赖测试文件的理由。
- 只解释接口或修改 Skill/文档时核对源码、链接和示例，无需重载 Blender 或重跑资源性能测试。
- 调度改动验证发送不内联、Blender timer 实际自动推进、快速结果延后交付、关联 ID、异常、超时/取消、容量、生成器公平推进与清理；手动调用 `pump()` 或 `_tick()` 不能代替真实 timer 验证。
- Blender 接入改动验证失败回滚、重复启停、文件切换及旧 timer 移除；涉及场景修改再测真实窗口 Undo/Redo。
- 按项目要求运行 Ruff、Pyright 和相关现有测试；按变更选择检查，不重复无关大规模验证。
- 运行期改动的热加载遵循项目约定和用户授权；不通过重新导入用户资产来验证热加载。
- 在对话中简要说明实际 Topic、处理函数、结果/错误契约、生命周期入口、已测范围和剩余限制，不生成独立交接报告；项目整理和交付遵循 [$oxal-project](../oxal-project/SKILL.md)。

资源导入另读 [$oxal-resource-import](../oxal-resource-import/SKILL.md)，负责资源身份、读取计划、依赖、拥有权、事务清理和性能基线。
