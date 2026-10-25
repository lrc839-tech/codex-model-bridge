# 本机三组对照：预注册方案 v1

本方案比较三套**完整工作流**，不是把框架当成模型，也不把一次好看的示例当成智能证明。首轮为 6 道公开题 × 3 组 × 1 次，共 18 次任务；目的是检查计量、隔离和流程是否可用。尚未产生三组模型成绩。用户确认的首轮上限为总计 2 小时、外部 API 估算人民币 10 元；额度不足或测量不完整时应停止并如实保留已完成记录。

## 1. 要回答的问题

1. 在相同验收条件下，混合工作流是否减少 Codex 的输入、输出与推理用量？
2. 加上外接模型和失败重试后，是否降低“每个成功任务”的外部费用/订阅额度消耗？
3. 相比纯 Codex，混合工作流的成功率和完成时间怎样变化？
4. 相比官方 DeepSeek Harness + **同一个 DeepSeek 模型和推理设置**，Codex 规划/验收 + Bridge 是否交付更好的结果？

第 4 项得到的是“这些任务和约束下，这套产品工作流的表现”，不能据此宣称 Harness 普遍不如 Bridge，也不能证明 Codex 规划一定是收益来源。若要归因，再增加“Bridge + 全部由 DS 规划/执行/审阅”的消融组；它不属于首轮 18 次。

## 2. 三组必须真正独立

| 组 | 输入与过程 | 计量边界 |
|---|---|---|
| A `codex` | 新会话的 Codex 独立理解、规划、实现和检查 | 从收到原始需求到最终提交，包括失败和修复 |
| B `hybrid` | 新会话的 Codex 收到同一需求，自行规划；外接 DS 实现/初审；Codex 判断并必要时修订任务。首轮最多两次委派，不静默切换回自行实现 | Codex 的全部阶段 + DS 的全部请求；不可只计 DS 阶段 |
| C `deepseek-harness` | 官方 DeepSeek Harness 收到同一需求，自行规划、实现和检查 | Harness 内所有 DS 请求、工具调用和修复 |

每题三组的起始文件、公开材料、可执行命令、网络策略和总超时相同。B 组的 Codex 规划不能复制到 A/C 组，其他组的代码/评审/报错也不能传给另一组。协调者可以调度和收集日志，不能在后跑的组里扮演“已经知道答案”的解题者。

**桌面与 CLI 分开标记。** 当前桌面会话可担任协调者；如果后台 worker 实际使用 Codex CLI，则结果标为 `codex-cli-controlled`，并记录 CLI 模型、推理档、版本和插件策略，不能把成绩写成已实测桌面图形界面全链路。直接用这个已经讨论过方案、看过其他项目的长会话作为 A/B 的解题者，会造成上下文不公平。

## 3. 为什么先用这六题

官方 [Exercism Python 仓库](https://github.com/exercism/python) 使用 MIT 许可，练习有说明、starter、参考实现和 unittest。已锁定 commit `6105f6d9d3ef66ce3d5c8f529d2882af3d76b706`，不是运行时下载 `main`。[锁定的配置](https://github.com/exercism/python/blob/6105f6d9d3ef66ce3d5c8f529d2882af3d76b706/config.json)有 142 道 practice 题；题目官方难度取配置字段，而不取可能滞后的说明表。

| 分层 | 抽到的题 | 主要能力 | 官方测试数 |
|---|---|---|---:|
| 解析器 | `sgf-parsing` | 树形格式、转义、非法输入和准确异常信息 | 23 |
| 状态机 | `bank-account` | 打开/关闭、金额状态、边界和异常 | 16 |
| I/O 与协议 | `paasio` | 包装器、上下文管理器、部分读写与统计 | 25 |
| 搜索 | `word-search` | 八方向搜索、坐标和不存在情况 | 24 |
| 树上下文 | `zipper` | 带上下文的导航、更新与还原整树 | 14 |
| 组合优化 | `book-store` | 折扣组合的全局最低价 | 20 |

这些题比 3+2、Hello World 或单个字符串函数复杂，又不要求先装整个大型仓库。它们仍然是小型实现题，强模型可能全部通过，不能代替真实多文件工程评测。若三组都通过，结论应聚焦于用量、耗时、修复和稳定性，而不是硬分“智能高低”。

### 采样方法事先冻结

池、种子和算法都写入 `task-candidates.json`：六个技能层，每层对 `sha256(seed + ':' + category + ':' + slug)` 排序，选最小项；种子为 `codex-model-bridge-pilot-2026-10-25-v1`。没有看三组成绩再挑选有利题目。准备阶段若官方参考实现不通过或 starter 已全部通过，则整题标为环境/题目无效并停止发布这份计划，不偷偷换到有利任务；排除理由和新的抽样版本要留档。

### 公开输入与评分控制区

```text
benchmarks/fixtures/exercism-python/<slug>/
  INSTRUCTIONS.md          官方原文和官方 append，不能漏掉异常消息等要求
  INTERFACE.md             接口、返回值、坐标/金额单位等公开澄清
  <starter>.py             原始空实现
  LICENSE.exercism-python

benchmarks/evaluators/exercism-python/<slug>/
  *_test.py, test_utils.py 官方测试与必要 helper
  reference/<solution>.py 官方参考实现，仅验证评分器
  SOURCE.json              每份源文件的 commit/path/hash
  LICENSE.exercism-python
```

**只有 fixture 目录可以给 solver。** 不能把整个项目、benchmarks、evaluators 或其他题目挂进沙箱。复制 grader 时必须使用 `grading.copy_files`，它不包含 `reference/`。完整评分在 solver 退出之后进行；测试失败内容不能反馈回本次任务重做。公开的语法检查可以在执行过程中运行，但不能把语法通过当成验收成功。

Exercism 原本是公开测试的学习练习；这里将官方测试放在 solver 看不到的本地控制区，是我们明确披露的评测改造，不是“官方标准隐藏榜”。这些题和答案早已公开，不能排除训练污染；本地隔离只防止这次运行去翻答案，不能抹除模型已有记忆。`INTERFACE.md` 的澄清需在三组开始前固定，避免把不明示的签名、计价单位或异常文案变成隐藏要求。

### 可复现准备与评分

仅准备数据、校验官方参考和 starter，不调用模型 API：

```bash
python3 benchmarks/prepare_exercism.py
```

脚本从固定 commit 拉取，不全局安装依赖；源文件已有但 hash 不同会拒绝覆盖。源文件设置只读，同时在任务清单中记录 hash；只读标记本身不是安全边界。校验通过条件是官方参考全通过、原始 starter 不全通过、两者执行了预期数量的测试。校验使用 Codex 的断网只读沙箱。当前本机 Python 若超出上游声明的 3.10–3.13 支持范围，会显式提示；本轮三组和评分器必须固定同一解释器版本，不能混跑。

评分器在单独的全新目录合并本次提交的**允许产物**和控制区的 `grading.copy_files`，再运行：

```bash
python3 -B -m unittest discover -s . -p '*_test.py' -v
```

示例只表达评分命令，不能在 solver 的工作目录直接展开隐藏测试。权威结果必须在干净评分副本中重放提交文件，且核对测试文件 hash、执行数量、跳过数量与退出码。官方参考、空 starter 和模型产物的评分目录不得复用。

## 4. 其他公开候选与取舍

| 数据集 | 固定来源/许可 | 规模与评分器 | 本 Mac 的前置条件 | 是否首轮 |
|---|---|---|---|---|
| Exercism Python | 上述锁定 commit；MIT | 142 practice 中的分层六题；标准库 unittest | Python；任务小、离线；官方支持 Python 3.10–3.13 | 是，计量与隔离 pilot |
| [Exercism JavaScript](https://github.com/exercism/javascript/tree/ff04f35158cdf61eebfdf18a774010495a1fd6d9) | commit `ff04f35158cdf61eebfdf18a774010495a1fd6d9`；MIT | practice/concept 题；官方 Node 测试脚本 | Node/Corepack/pnpm；应预装锁定依赖并断网运行 | 否，首轮先固定一种语言减少环境差异 |
| [SWE-bench Lite](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite/tree/6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2) | 数据 revision `6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2`；[评分代码 MIT](https://github.com/SWE-bench/SWE-bench/tree/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e)；任务代码保留上游许可 | 300 test、23 dev；fail-to-pass / pass-to-pass，resolved rate | Docker；官方推荐 120GB 空间、16GB RAM、8 核，ARM 实验支持；需 Buildx 本地构建 | 第二阶段优先，先逐题做 gold/nop 门禁 |
| [SWE-bench Pro V2](https://github.com/scaleapi/SWE-bench_Pro-os/tree/66f92766bba642462d4bbe5479e83f91f9211862/v2) | `v2.0.0` / `66f92766bba642462d4bbe5479e83f91f9211862`；harness MIT，题目按各上游许可 | 当前 V2 为 642 题/11 仓库；完整功能/回归测试、干净镜像重评；不是旧版 731 | 官方镜像 linux/amd64；锁定协议每题 50 分钟；M 芯片模拟有额外差异 | 否，首轮太重；以后选择已通过环境门禁的小型实例 |
| [Terminal-Bench](https://github.com/harbor-framework/terminal-bench/tree/e67a2cb7db4d9795c212909b3395d8c22ad60aec) | 仓库 commit `e67a2cb7db4d9795c212909b3395d8c22ad60aec`；Apache-2.0；每题素材许可另查 | 持续更新的任务集；当前 release 有 v4.0.0，不能再默认“只有 89 题”；每题 verifier/reward | Harbor + 容器运行环境；主 README 的发布流程使用 Modal；本地 Docker 支持与 ARM 需逐题验证 | 否，先锁定某个 release 再选不需 GPU/服务/网络的任务 |
| [BigCodeBench](https://github.com/bigcode-project/bigcodebench/tree/09dd993f46c3fbf3a799465bb96d524edcb0b199) | 代码 commit `09dd993f46c3fbf3a799465bb96d524edcb0b199`；Apache-2.0；[数据 revision](https://huggingface.co/datasets/bigcode/bigcodebench/tree/b74c0d0bf70d2c0bc459be537895cca163007f1a) | 1,140 函数题，Hard 148；自带 tests，Complete/Instruct 两种输入 | 依赖多种第三方 Python 库，官方容器较合适；仓库当前已归档 | 否，适合补充工具库能力，不能代表仓库级 harness 协作 |
| [SWE-smith](https://github.com/SWE-bench/SWE-smith/tree/9b74ac08118a85c39c356802f7961893af73e07f) | 代码 commit `9b74ac08118a85c39c356802f7961893af73e07f`；MIT；上游代码许可另查 | 合成修复/定位任务；固定[数据 revision](https://huggingface.co/datasets/SWE-bench/SWE-smith/tree/ea6d7173829c7ec8fa16c22055699ff2e9188091)卡片为 59,136 条，README 仍概称 52k；既有测试检验 | 官方明确开发/测试在 Ubuntu22.04，不计划支持 macOS/Windows | 否，不用来承诺本机即装即跑；且作为训练集污染较强 |

以上规模是这次查询/锁定版本的信息。任务内容、源码、评分器和外部素材可能使用不同许可，不能把 harness 的 MIT 自动套到所有上游项目。我们不把公开参考答案打进群友的执行目录。

SWE-bench 第二阶段建议：先预注册按仓库分层抽样、按题目元数据筛除 GPU/联网/不支持架构的实例，再对所有入选题验证 gold 通过与 nop 失败。不能按 A/B/C 的成绩筛选；不能用修复 patch 代码内容来指导任务规划。当前 Bridge 有文件数、单文件大小和文本工具范围限制；若不能向三组提供同一完整仓库视图，先修能力边界或把结果标为共同受限子集，不能给 B 组选过的关键文件而让另两组自己搜整个仓库。

## 5. 防止偷用其他项目答案

仅换一个文件夹远远不够。每次运行前必须通过以下探针，并将真实结果保存：

1. 为每个 `task × arm × repetition` 新建工作副本、新会话和独立可写状态；起始文件 hash 必须相同。
2. 在控制区、另一个任务、日常项目和隐藏 evaluator 各放一个随机 canary；solver 读取这些位置必须失败。路径范围包括父目录、绝对路径、`/tmp`、符号链接和日常用户主目录；不能只禁写不禁读。
3. 任务进程无法访问其他会话记录、全局 AGENTS/skills/memories、其他组的日志/答案、Git 历史、stash、额外 refs 和共享结果缓存。需要 Git 的任务使用清理历史后的快照。
4. Shell 子进程断网；模型 API 由宿主 broker 或只允许指定模型域名的代理访问。不能让 agent 用 curl/git/browser 搜题或拉 fixing commit。仅“提示词里说不要”不算强制限制。
5. 隐藏测试、参考答案、原始评分数据一直在控制区。worker 退出后由 grader 创建全新副本；只接收白名单产物，拒绝符号链接及对 tests/env/cache 的修改。原任务工作副本不直接拿来当权威评分环境。
6. A/B/C 必须通过同样的越界和网络探针。任何一组探针失败，该次结果为 `isolation_failed`，不运行正式任务、不统计成正常能力成绩。模型 endpoint 要保留的登录权限不能扩散给生成的 shell 命令。

同一 Mac 上的 Docker/OS 沙箱仍需实测规则，不能仅以“在容器内”为证。若桌面控制插件拥有全盘读取权限，就不能把它无约束地暴露给基准解题者。首轮任务为纯文本，不需要图像/浏览器；视觉任务另开赛道，明确给各模型原图还是 Codex 文字描述，否则输入信息量不同。

## 6. 用量与时间的记录规则

记录每个原始请求的 provider/model、阶段、开始/结束、状态和 API usage，保留缓存输入、普通输入、输出、推理 Token 的原字段。失败、重试、取消、超时也应写事件。服务端没返回 usage 要记 `unknown`，不能记 0，更不能用文本长度假装服务端 Token 数。

Codex 订阅用量与 DS Token 分开：

- CLI/会话事件能报告 Token 时，保存事件来源和 run/thread id，仅累加实际属于本次任务的增量。推理 Token 如果已包含在输出 Token 中，不再次相加。
- 周额度/短时额度前后差只作为账号级辅助指标；同时跑的其他聊天和显示精度会干扰，不能换算成精确 Token。
- 不同模型的 Tokenizer 不同；“两家总 Token 相加”只是数量描述，主要成本结论看 Codex 用量变化、DS API 实际费用、按成功任务的成本。没有权威价格/账单时仅报告估算。
- 所有计量包括规划、交接、执行、独立初审、失败诊断、修订和最终检查，不能只截取最便宜的一段。

时间使用单调时钟；另记 UTC 方便对账。分开保存：准备/下载/环境构建（一次性）、排队等待、首次可评分产物、完整任务墙钟、工具执行、模型等待和隐藏评分。比较产品体验时报告从任务派发到最终合格产物的端到端时间；性能分析可另报去除环境预热的时间，不能只挑有利数字。

先统一预热，不在三组正式时间中混入首次安装和首次拉镜像。API 服务器缓存无法强制清空，应记录 cache usage；采用按题分块的交错顺序减少缓存/时段偏差。首轮 6 题可轮转 `ABC/BCA/CAB/ACB/CBA/BAC`，每次只运行一组，避免争抢本机资源或同账号并发干扰周额度。

## 7. 评分与公平性

主指标是 `resolved`：固定隐藏测试全部通过、数量匹配、没有跳过/篡改、产物范围合法。辅指标为通过测试比例、首个提交成功率、修复轮数、API/工具失败率、人工介入次数、越界次数和超时率。只通过公开语法检查不能算成功；模型自己说通过不能作为证据。

报告还应包含：

- 质量不下降时的 Codex 用量变化；失败也计成本。
- 每个成功任务的 Codex 用量、DS 费用和墙钟时间。
- 超时/取消/无 usage 的数量；缺失不得排成“最省”的第一名。
- 任务级配对结果和全量轨迹，不能只展示成功截图。
- 主观代码可维护性可做盲审，评分规则与组别遮挡事先固定；不能让同一个执行模型担任权威裁判。

六题一次不是显著性证明，按题报告差异，不写“提升 X% 智能”。下一阶段扩到至少 20–30 个独立任务，并对运行随机性有影响的任务做多次独立重复，再做按题聚类的 bootstrap 置信区间。不能把每个测试断言当成独立任务扩大样本量，也不能因为第一次失败就额外给某组抽一次幸运重试。

模型版本、推理强度、上下文上限、工具集合、语言、公开输入和总时间预算都需冻结。三组天然的模型/工具次数可以不同，因为这是工作流对照；应原样记录。如果要比较“同等资源的效率”，另加等总时间或等外部费用预算的条件，不把这种条件混入主结果。B 的执行与复审使用同一个 DS，也不是完全独立的错误来源；最终权威来自固定评分器。

## 8. 无人值守运行的启动门禁

先用本机模拟 API 完成运行时协议与工具调用探针，验证官方 Harness 的退出、工具隔离和 usage 采集，再通过题目 gold/nop、预算与暂停机制。模拟响应不算真实供应商模型验收；为保持最多18次任务运行，首个正式条目同时验证真实连接，若失败照实计入本轮，不额外赠送重跑。任何运行器、接口或沙箱不可用就先停在 `blocked`，不能悄悄把 C 换成 Bridge 的 DS worker，也不能把 B 换成 DS-only。

首轮 18 次任务的总预算到达任一上限即停止：2 小时、估算 DS 人民币 10 元、认证/额度错误或隔离失败。预计费用只是服务端结算前的近似，硬 Token/请求上限更可靠；没有当前模型价格时，不得声称已强制执行精确人民币上限。保存已完成任务和停止原因，次日可以重现式续跑，不自动重做已经成功的条目。

无人值守应调用官方 CLI/API 运行器，不依赖持续点桌面按钮、焦点位置或解锁屏幕。Mac 休眠、电源状态、网络断开、服务限流、订阅额度重置/耗尽和本机时间变化都要成为明确状态；不能无限重试。最终汇总必须区分已完成、环境失败、超时、预算终止和从未开始。

## 9. 本次核查的官方来源

- [Exercism Python README，Python 支持与许可](https://github.com/exercism/python/blob/6105f6d9d3ef66ce3d5c8f529d2882af3d76b706/README.md)
- [Exercism Python 各题文件清单与官方说明](https://github.com/exercism/python/tree/6105f6d9d3ef66ce3d5c8f529d2882af3d76b706/exercises/practice)
- [SWE-bench 当前 README，Docker/ARM/资源要求](https://github.com/SWE-bench/SWE-bench/blob/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e/README.md)
- [SWE-bench 官方快速开始与 gold 校验](https://www.swebench.com/SWE-bench/guides/quickstart/)
- [SWE-bench Pro V2 的隐藏评分、50 分钟预算及 pristine regrade](https://github.com/scaleapi/SWE-bench_Pro-os/blob/66f92766bba642462d4bbe5479e83f91f9211862/v2/README.md)
- [Terminal-Bench 当前官方仓库与运行方式](https://github.com/harbor-framework/terminal-bench/tree/e67a2cb7db4d9795c212909b3395d8c22ad60aec)
- [BigCodeBench 官方 README](https://github.com/bigcode-project/bigcodebench/blob/09dd993f46c3fbf3a799465bb96d524edcb0b199/README.md)
- [SWE-smith 官方平台支持说明](https://github.com/SWE-bench/SWE-smith/blob/9b74ac08118a85c39c356802f7961893af73e07f/README.md)

源数据准备脚本会实际验证本机题目的评分门禁；列在候选表中的其他框架/数据集未在这台 Mac 上完成安装或模型跑分，不把文档可行性当成已验收。
