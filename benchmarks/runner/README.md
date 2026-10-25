# 本机评测底座

这是标准库 Python 模块。导入模块、执行 `probe`、导入用量都不会发送模型请求。它为三组试验保留同样的任务起点、独立进程、独立目录和最终结果；它不把 Codex CLI 测试称为桌面产品测试。

## 无付费检查

从仓库根目录执行：

```sh
python3 -m unittest discover -s benchmarks/runner/tests -v
python3 -m benchmarks.runner probe --output /absolute/private-control/isolation.json
```

真实探针会临时建立工作区、外部哨兵、兄弟任务哨兵和指向外部的符号链接，并先确认本机 TCP 监听确实可连。只有工作区读写通过，外部读写、兄弟任务读取、符号链接读取以及 TCP 连接均被策略拒绝才算通过。它只运行本地 Python，不调用模型、不访问供应商 API，不改变 HOME/CODEX_HOME。

**这个探针只证明本地命令沙箱。** 整条 agent 工作流还需分别验证启动输入、记忆、技能、插件、浏览器、内置文件工具、子智能体与供应商网络代理，不能拿一次命令拒绝结果代替全部隔离验收。

## 根调度器接口

- `isolation.probe_isolation(codex=None, read_roots=())` 返回命令策略的证据；哈希绑定本机 CLI 和精确策略。
- `adapters.probe_codex_capabilities()` 只运行 `exec --help` 和 `features list`。
- `adapters.codex_plan(workspace, prompt, model, isolation_probe, capability_probe, read_roots=(), hybrid_instruction=None)` 构建 CLI 命令。忽略用户配置和规则、禁止全局技能发现、记忆、插件、网页、浏览器、桌面控制与子智能体；只允许本任务目录及明确的可信运行时只读根。没有 `resume/fork/--add-dir`，没有无沙箱回退。混合指令是预注册的实验处理，必须在 `prepare_run` 前固定为最终 `plan.prompt`。
- `adapters.probe_dsh_capabilities(binary, environment)` 只执行官方 headless `--help`。DSH 不提供已证明的隔离证据就拒绝构建可启动计划；根调度器可以注入经过实际验证、绑定文件哈希的补丁与证据。
- `coordinator.prepare_run(control_root, task_id, fixture_dir, arm, repeat, prompt, source_metadata=...)` 只逐字节复制白名单 fixture。控制文件与其他任务在工作区之外。禁止符号链接、硬链接、Git 元数据、Agent 配置和凭据混入输入。
- `coordinator.execute_run(run_dir, plan, authorized_model_requests=True, launch_guard=...)` 只执行一次；启动前核对 fixture 与 prompt 哈希。`launch_guard` 必须同时检查完整隔离和本次总预算。供应商请求级金额拦截必须在 API broker/provider 客户端中实现，这个函数的进程启动检查不能替代每次请求限额。
- `coordinator.record_grade(...)` 只在 worker 停止后记录外部 grader 结果与 grader 哈希。官方 tests、example、隐藏评测器只能在控制区，不能映射给 worker。模型自报不作为分数。

推荐控制目录每次 campaign 独立：`<control>/<task>/<arm>/r01/{workspace,manifest.json,checkpoint.json,events.jsonl,result.json,grade.json}`。只将 `workspace` 作为 agent 根；不要把整个 `benchmarks` 或 campaign 根授权给 agent。

混合组通过文件邮箱调用宿主的可信 Bridge。外接实现先暴露为 `.bridge-candidate-NNN/` 中的未交付预览和脱敏验收模板，不直接覆盖最终 solution。Codex 逐项填写 `.bridge-review-NNN.json`；如果初审存在 blocked 或 changes_requested，须对每条 finding/unverified 提交裁决理由和 resolved 证据。只有执行、固定检查和文件哈希均有效时，宿主才调用真实 `bridge.py apply`，集成后重测通过再复制允许的 solution 并发回 receipt。

裁决只运行本地检查，不新增模型请求、不增加外包次数；每份报告只 claim 一次。预览被修改、验收缺证据、哈希失效、检查失败均禁止集成。最终 solution 必须与成功 apply 回执的哈希相同，否则标记协议违规，不能将 Codex 自行代写的主要实现当作外接模型交付。结构校验仍不能证明证据语义真实，最后判断由 Codex 负责。这条通道已做无 Key、无 API 的真实产品 apply 本地夹具测试，尚未重新做付费三组评测；原试验失败记录没有因此改分。

## 中断和用量

进程启动意图先写入 checkpoint，再实际启动。如果协调器在发出请求后中断，下一次读取会转成 `uncertain`，不会自动重发。已完成、失败、超时和不确定运行都会保留；如需重复，应预注册新的 repeat，不能把失败运行删掉重来。

宿主可以请求“当前任务结束后停止”，控制文件位于候选工作区之外：

```sh
python3 -m benchmarks.runner.control stop /absolute/private-campaign
python3 -m benchmarks.runner.control status /absolute/private-campaign
```

当前任务、独立评分和已发出请求的记账收尾完成后，不再启动下一任务。`clear` 只清除停止标志并保留历史，不会自动恢复或重发任何请求。每次候选退出后先关闭工具服务、作废该 run 的 API route，再通过可信 broker 最多等待 8 秒结清已有请求。该等待不会发新请求；仍 pending 或 usage 不完整时停止新 run，保留 unknown，不填零。

新 cohort 启动前不只记录源文件哈希，还将实际 `.py` 字节复制到私有控制区的 `source-snapshot/` 并逐一核对。执行期间核心源码变化会阻止启动下一 run。已启动 cohort 的代码变更后不能按原实验继续；旧失败与超时记录保留。停止控制文件与报告不属于候选输入。

`records.import_codex_jsonl` 读取 CLI `turn.completed.usage`；这些是 turn 汇总，**不能声称是逐 HTTP 请求 ledger**。缓存输入是输入的子集、推理输出是输出的子集，不重复相加。CLI 没返回 usage 或失败请求没有 usage 时保留 unknown，不填零。外部模型逐请求 ledger 可用 `import_request_ledger` 导入，重复 request_id 拒绝累计。原始事件文件私有保留，汇总只复制允许的字段，不复制 API Key、headers、请求正文和推理正文。

## 官方接口依据

- [Codex 非交互 JSON 输出](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Codex 命名权限与约束范围](https://learn.chatgpt.com/docs/permissions)：命令权限不自动控制 MCP、连接器、浏览器和电脑控制；这些必须另行禁用。
- [DeepSeek Harness headless 官方代码文档](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/headless/README.md)：`dsh --profile headless --permission-mode workspace-write --approval-policy never --json -`；默认新 Session。需要 `exit 0` 且 `final.reason.kind=completed`，工具返回 completed 不代表任务成功。
- [DeepSeek Harness Python SDK 官方文档](https://github.com/deepseek-ai/deepseek-harness/blob/master/python/sdk/README.md)：`deepseek-harness-sdk` 配套同版本 `deepseek-harness-runtime-bin`；显式独立 `dsh_home`，可设 `cwd/provider/model/base_url/api_key`，无需系统 Node。DSH_HOME 只通过子进程环境传入；不改变用户全局 HOME/CODEX_HOME。

到这里的模块和离线测试不代表五家供应商实测，也不代表已取得桌面 Codex 单任务用量；真实运行证据必须来自对应 campaign。
