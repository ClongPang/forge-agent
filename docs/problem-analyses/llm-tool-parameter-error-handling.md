# 大模型工具参数错误处理方案分析

## 问题背景

在当前项目中，大模型通过 `Action -> ToolCall -> ToolRegistry -> Tool.execute()` 的链路调用工具。工具参数来自模型输出，例如：

```json
{"tool": "file_read", "params": {"path": "src/main.py"}}
```

这类参数并不总是可靠。模型可能返回缺失字段、类型错误、非法 JSON、错误工具名，或者语法上合法但语义上无效、危险的参数。

当前项目已经有一些基础兜底：

- OpenAI-compatible function calling 参数 JSON 解析失败时会降级为 `{"raw": "..."}`
- 文本 fallback 模式中 `params` 不是对象时会降级为 `{}`
- 工具名不存在时，`ToolRegistry` 返回 `Unknown tool`
- 工具内部抛异常时，`ToolRegistry` 捕获并返回 `Tool raised an unexpected error`
- 部分工具有手写校验，例如 `shell` 缺 `cmd`、`git_commit` 缺 `message`、`search_text` regex 非法

这些处理能避免 agent 进程轻易崩溃，但错误反馈不够稳定，也没有统一的参数 schema 校验。

## 成熟 Agent 的常见处理方式

成熟 agent 一般不会直接信任模型输出，而是分层处理：

```text
LLM ToolCall
  -> 结构化解析
  -> schema validation
  -> safety validation
  -> tool business validation
  -> tool execution
  -> observation feedback
```

这么做的动机是：

- 让参数错误可恢复，而不是直接中断任务
- 给模型稳定、可修正的错误信息
- 防止合法格式中的危险操作绕过安全边界
- 避免各工具重复实现低层参数校验
- 让日志和测试能够稳定地区分错误类型

## 推荐演进顺序

### 1. 在 `ToolRegistry.execute_tool()` 中做统一 schema validation

`ToolRegistry` 是所有工具调用的统一入口：

```text
Agent.run()
  -> registry.execute_tool(tool_name, params)
  -> tool.execute(params)
```

在这里校验 `tool.parameters_schema`，一次改动即可覆盖所有工具。

这样可以拦住典型的“参数形状错误”：

- 缺必填字段：`{"tool": "file_read", "params": {}}`
- 类型错误：`{"tool": "shell", "params": {"cmd": 123}}`
- 非对象参数：`params` 是字符串、数组或 null
- enum / integer / boolean 等基本 schema 不匹配

不建议把这类校验散落到每个工具里，因为会导致重复、遗漏和错误格式不一致。

### 2. 统一 invalid params observation 格式

schema validation 发现错误后，应返回稳定格式，例如：

```text
Invalid params for file_read: missing required property 'path'
Expected schema:
{"path": "string"}
```

或者更简洁：

```text
Invalid params for shell: property 'cmd' must be string
```

这条错误会进入 observation，再进入下一轮模型上下文。稳定的错误文本能帮助模型自我修正。

如果只返回 Python 异常，例如：

```text
Tool raised an unexpected error: expected str, bytes or os.PathLike object, not int
```

模型很难准确理解应该如何改参数。

### 3. 保留工具内部业务语义校验

schema validation 只负责“形状”，工具仍然必须负责“语义”。

例如：

```json
{"path": "src/main.py"}
```

schema 只能判断 `path` 是字符串，不能判断：

- 文件是否存在
- 是否是文件而不是目录
- 是否可读
- 是否超过大小限制
- 是否越过 workspace boundary

类似地：

- `search_text` 需要判断 regex 是否可编译
- `pytest` 需要处理测试失败和超时
- `git_commit` 需要处理没有 staged changes
- `shell` 需要处理命令退出码和超时

因此，工具内部业务校验不能被 schema validation 替代。

### 4. 安全边界继续放在 path guard / tool / runtime 层

安全校验不是普通 schema 校验。

例如：

```json
{"path": "/etc/hosts"}
```

schema 看起来完全合法，因为 `path` 是字符串。但安全层必须判断它是否越过工作区边界。

再比如：

```json
{"cmd": "cat /etc/hosts"}
```

schema 只能知道 `cmd` 是字符串，无法可靠判断 shell 字符串中的路径、副作用和间接行为。

因此当前的 `WorkspaceBoundary`、`ShellTool` 路径扫描、`LocalRuntime` cwd 校验、`DockerRuntime` 路径转换仍然需要保留。schema validation 是更早的形状过滤，不是安全边界替代品。

### 5. 最后再做连续参数错误触发 reflection

连续错误处理应建立在稳定错误类型之上。

如果没有统一 schema validation，错误可能表现为各种工具内部异常，难以判断是不是模型参数错误。

有了统一错误后，可以做策略层处理：

```text
连续 3 次 Invalid params
  -> 注入 reflection prompt
  -> 要求模型重新检查工具 schema 和参数
```

这一步应放在最后，因为它依赖前面几层提供清晰、稳定的错误信号。

## 能解决的问题

完成上述改造后，可以明显改善：

- 模型漏传必填参数
- 模型传错参数类型
- 模型返回 `params` 不是 object
- 模型调用不存在工具
- 工具内部低级 TypeError 泄漏给模型
- 错误提示不稳定，模型难以自修正
- 连续无效工具调用浪费 steps 和 tokens

典型示例：

```json
{"tool": "file_read", "params": {}}
```

应返回：

```text
Invalid params for file_read: missing required property 'path'
```

```json
{"tool": "shell", "params": {"cmd": 123}}
```

应返回：

```text
Invalid params for shell: property 'cmd' must be string
```

## 不能解决的问题

这套机制不能解决所有 agent 失败。

仍然不能完全解决：

- 模型选择了错误工具，但参数格式合法
- 模型计划错误，例如不该 commit 却调用了 `git_commit`
- 参数格式合法但业务语义无效，例如文件不存在
- shell 字符串中复杂间接行为，例如 `python -c 'open("/etc/hosts").read()'`
- 代码修改质量差
- 测试失败后的推理能力不足
- 外部 API、网络、权限、依赖环境问题

这些分别需要规划改进、工具语义校验、安全沙箱、测试反馈和模型能力来处理。

## 对当前项目的落地建议

### 第一阶段：轻量 schema validation

在 `tools/base.py` 的 `ToolRegistry.execute_tool()` 中，在调用 `tool.execute(params)` 之前做校验。

建议先支持项目当前 schema 中已经用到的最小子集：

- `type: object`
- `required`
- `properties`
- `string`
- `integer`
- `boolean`
- `array`

不建议一开始引入完整 JSON Schema 依赖，除非后续 schema 复杂度明显提高。当前项目的 schema 较简单，手写轻量校验足够。

### 第二阶段：统一错误格式

新增类似：

```python
def _validate_params(schema: dict, params: Any) -> str | None:
    ...
```

返回 `None` 表示通过；返回字符串表示错误。

`execute_tool()` 中：

```python
error = _validate_params(tool.parameters_schema, params)
if error:
    return ToolResult(
        success=False,
        output="",
        error=f"Invalid params for {name}: {error}",
    )
```

### 第三阶段：补测试

建议新增或扩展 `tests/test_day3.py` / `tests/test_confirm.py` / 新文件：

- 缺必填参数
- 参数类型错误
- `params` 不是 dict
- 未知工具名
- 校验失败时工具不执行
- 校验错误会转为 observation
- 合法参数仍然走工具业务逻辑

### 第四阶段：连续参数错误 reflection

等错误格式稳定后，再在 `Agent.run()` 中统计连续 invalid params observation。

例如：

```text
连续 N 次 observation.error.startswith("Invalid params")
  -> log_reflection(reason="invalid_tool_params")
  -> history.add(reflection prompt)
```

这个阶段可以独立做，不要和 schema validation 混在同一次大改里。

## 设计边界

推荐保持职责分离：

```text
LLM backend
  负责把供应商响应解析成 Action / ToolCall

ToolRegistry
  负责工具存在性检查、参数形状校验、异常兜底

Tool
  负责业务语义校验和实际执行

WorkspaceBoundary / Runtime
  负责路径和执行安全边界

Agent Core
  负责把 observation 写回 history，并处理 reflection / loop detection
```

不要让某一层承担所有职责。这样后续新增工具或替换模型后，错误处理仍然稳定。

## 结论

推荐顺序是：

```text
1. ToolRegistry schema validation
2. 统一 invalid params observation
3. 保留工具业务语义校验
4. 保留 path guard / safety checks
5. 连续参数错误 reflection
```

这个顺序从最低层、最确定、覆盖面最大的错误开始处理，再逐步上升到策略层。

这样做完以后，项目能更稳定地处理模型参数错误，减少工具内部异常泄漏，并提高模型下一轮自修正的概率；但它不会替代业务判断、安全沙箱和模型规划能力。
