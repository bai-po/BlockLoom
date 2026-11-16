# 当前 Bus API

实现范围：当前 EMT 自动调度。本文描述已有接口；修改权限统一见 [AGENTS.md](../../../../AGENTS.md#核心修改权限先于业务任务执行)。只有受授权的核心维护可调整接口及调度方式并同步本文，业务执行者不得自行改动或绕过。

以项目 [core/bus.py](../../../../core/bus.py)、[core/messages.py](../../../../core/messages.py) 为准。本文不定义额外运行时。

## 核心文件职责

`core/` 仅存放 EMT 运行代码；目录说明与 API 文档统一维护在本 Skill 中。

| 文件 | 职责 |
| --- | --- |
| [messages.py](../../../../core/messages.py) | Message、Reply、Failure 与处理/回调类型 |
| [bus.py](../../../../core/bus.py) | Topic 登记、请求队列、调度、回复与清理 |
| [runtime.py](../../../../core/runtime.py) | Blender 主线程自动调度、渲染暂停及生命周期接线 |
| [importing.py](../../../../core/importing.py) | EMT 端点状态及原生 Undo 执行适配 |
| [commands.py](../../../../core/commands.py) | 通用业务命令复用同一 EMT/Undo 适配 |
| [__init__.py](../../../../core/__init__.py) | EMT 公共接口导出 |

共用资源、动画、节点、存储与通用登记工具位于 [util/](../../../../util/)，具体业务位于 [business/](../../../../business/)。核心修改权限仍以 AGENTS.md 为准。

## 消息与结果

`Message(request_id, topic, payload)` 是 EMT 信封。`send()` 自动生成 ID，对 payload 最外层做浅拷贝和只读包装；嵌套对象不深拷贝，发送后不要改写。
`Handler = Callable[[Message], object]` 接收一条消息，返回业务结果。普通类的绑定方法可直接登记，无需特定基类或装饰器。
`Callback = Callable[[Reply], None]` 是结果接收函数的引用，不带调用括号。

`Reply` 含 `request_id`、`topic`、`status`、`value`、`error`；`ok` 表示成功。
状态为 `succeeded`、`failed` 或 `cancelled`。`error` 是可空的 `Failure(code, message)`，读取前检查存在。

Bus 本身支持普通 Python 对象，不做网络传输或统一 JSON 序列化。Blender 延后请求用普通快照数据；`ImportEndpoint` 的 JSON 参数是那个适配器自己的契约。

## 公共接口

| API | 当前语义 |
| --- | --- |
| `Bus(max_pending=256, clock=monotonic)` | 独立、单线程的协作式实例；不依赖 Blender，不设置主机 gate 或生成器禁令 |
| `register(topic, handler)` | 登记非空 Topic 的函数引用；重名报错；不执行函数；不接受 `async def` |
| `unregister(topic)` | 撤销能力并取消其未完成请求；已经完成的待交付结果仍可交付 |
| `send(topic, *, on_result, payload=None, timeout=30.0)` | 保存必需回调并排队，返回 ID 字符串；`timeout=None` 不设期限 |
| `is_pending(request_id)` | 待执行或待交付时为 true；回复出队准备调用回调后不再 pending |
| `pending_count` | 属性，含待执行及尚未交付的请求 |
| `cancel(request_id, *, reason="Request cancelled")` | 能取消未完成请求时为 true，关闭生成器并排队取消结果；不取消已完成待回复的请求 |
| `pump(*, budget_seconds=0.004, max_steps=32)` | 推进一轮，返回步骤数；先交付已有回复，再推进任务，禁止递归调用 |
| `discard_pending()` | 丢弃任务和回复，关闭生成器，不通知旧回调；保留能力登记 |
| `cancel_pending(*, reason)` | 对当前未完成请求逐个取消，关闭其生成器并排队取消回复，返回该批请求数；保留已完成待交付的回复。宿主必须在安全阶段调用 |
| `close()` | 清空请求/回调与能力，关闭实例；后续业务使用新实例 |

Topic 精确匹配且区分大小写，`sky` 不匹配 `sky.load`，`sky.*` 不是通配表达式。
未知 Topic 可以先入队，调度时以 `unknown_topic` 回复。空 Topic、无效回调、非正或非有限超时会拒绝；容量满或实例关闭时提交报错，入口应反馈而非伪报成功。
内建错误代码为 `unknown_topic`、`handler_error`、`timeout`、`cancelled`。处理函数异常转换成失败回复，回调异常记录日志并隔离。

## 普通类的接入示例

以下展示逻辑上下文，实际按项目分层放入工具、模块和入口文件，不是自动加载模块：

```python
from ..core import Message, Reply
from ..core.runtime import get_bus


class TextTool:
    def uppercase(self, message: Message) -> str:
        text = message.payload.get("text")
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        return text.upper()


tool = TextTool()


def register():
    get_bus().register("text.uppercase", tool.uppercase)


def unregister():
    get_bus().unregister("text.uppercase")


def receive_result(reply: Reply):
    if reply.ok:
        print(reply.value)
    elif reply.error is not None:
        print(reply.error.code, reply.error.message)


def submit():
    return get_bus().send(
        "text.uppercase",
        payload={"text": "hello"},
        on_result=receive_result,
    )
```

登记保存函数引用，不复制函数代码。绑定方法让实例保持存活，工具构造不应加载大型缓存。
回调随请求保留，交付或丢弃后释放该请求引用，没有永久积累全部历史回调的机制。

## 调度边界

普通函数在调度时完整执行。生成器在小批次后 `yield`，以最终 `return result` 返回；yield 的值不作为结果。
每个请求每轮至多推进一次，完成后排队回复，后续轮次才调用回调。`send()` 不内联调用处理函数或回调。Blender 提交 Operator 只负责入队，由 runtime 的主线程 timer 自动调用 `pump()`；独立纯 Python 使用者自行安排 pump。现有资源/命令端点的同步 loader 不接受生成器，不能把 Bus 的生成器能力当作 loader 的返回契约。
预算/超时只在步骤边界检查，不能打断正在运行的 Python/native 调用。回调、生成器 `finally` 和关闭也须短小，清理阶段不要提交替代任务。
取消与超时不自动恢复场景状态。

Bus 保持纯 Python，不识别 Blender 渲染状态。runtime 在渲染或作业锁定期间暂停 pump，正式业务入口拒绝提交和修改；`render_init` 只置状态，不调用业务、回复或生成器清理。渲染结束后的首个安全 tick 先调用 `cancel_pending()`，再正常 pump；未完成旧请求不重放，取消回复和生成器清理都发生在安全阶段。已完成的回复仍按原结果交付。不得在渲染回调中手动推进或关闭任务来绕过宿主时机。

默认启用的 Topic 为 `sky.load`、`sky.command`、`human.import`、`human.edit`、`inventory.import`、`inventory.edit`、`assets.execute`；本地演示 `demo.sum` 已断开默认登记。新增时核对实际登记，不把该列表当白名单。
调度与生命周期改动按实际行为验证；临时测试文件、目录及产物遵循 AGENTS.md 的 `_test` 后缀规则，按需创建、用完清理，不随 Git 推送。只报告实际运行的检查，不假定仓库自带测试脚本。
