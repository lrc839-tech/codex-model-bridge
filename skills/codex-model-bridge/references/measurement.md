# 任务用量采集

仅当用户要比较消耗或排查用量时阅读。本工具不会读取认证文件，也不会用文本长度伪造模型 Token。

## 桌面当前任务

由本机 `CODEX_THREAD_ID` / `CODEX_SESSION_ID` 定位精确会话文件名，仅解析数字用量、turn 标识和时间。起点默认采用当前 turn 开始前的累计值，覆盖执行测量命令之前的规划。

```sh
python3 <skill_dir>/scripts/bridge.py measure-start --output /absolute/private/start.json --label my-task
python3 <skill_dir>/scripts/bridge.py measure-end --start-file /absolute/private/start.json --output /absolute/private/end.json
```

`--boundary now` 只测命令之后的窗口，不能用来冒充整个任务。独立协调器可传 `--session-id <candidate-id>`，在候选任务 `task_complete` 后采集完整结果。候选会话必须是新鲜、单任务、未混入协调者研发成本的会话。

任务内部调用 `measure-end` 会标 `candidate_turn_complete: false`，只覆盖最新已落盘的 Token 事件，尚未包含后续验收和最终回答。不能在同一任务中声明这是最终总量。若已进入另一个 turn、累计计数重置、起点缺失或来源无法核验，会返回 unknown；不要把未知换成零。

会话日志格式属于版本相关的本机记录，字段变化时应失败并保留未知。只有当前会话被解析，子代理和其他会话需要另行采集、去重；关闭子代理可简化受控比较。

## 三种度量不能混为一个数

| 度量 | 来源 | 可说明什么 |
|---|---|---|
| Codex 记录 Token | 会话数字事件 / CLI `turn.completed.usage` | 指定范围的模型用量观测 |
| 外部模型 Token | `usage-ledger.jsonl` 的真实服务端 usage | 实际已确认请求用量，失败也计入 |
| Pro 周额度百分比 | 账号用量窗口 | 账号共享、粗粒度的前后变化，不能反推单任务 Token |

缓存输入是输入子项，推理输出是输出子项，不能再加到总 Token。不同供应商 Tokenizer 不同，主要报告 Codex 用量、外部费用、正确率和时间，避免只凭两家 Token 简单加总宣布节省。

外部 ledger 每个请求 start/finish 都立即 fsync。某请求无 usage、崩溃未完成或返回数字矛盾时，总量为 null / unknown，已确认部分在 known_totals。重启不会自动重发不确定请求，也不能把已确认部分当成完整成本。
