---
name: codex-model-bridge
description: "用户明确选择混合工作流时，让桌面 Codex 规划和最终决策，通过本地工具将有固定验收的低风险文件子任务的实现与初检交给用户配置的兼容模型。引用技能不强制外调，普通问答和不适合委派的任务由 Codex 直接处理。"
---

# Codex Model Bridge

桌面 Codex 始终负责整体规划、任务拆分、风险判断和最终决定。默认纯 Codex；混合工作流只有省额档（内部名称 `saver`），不再提供严格档。`--mixed` 记录用户已明确选择外部模型，不代表可以自行替用户切换。

**引用 Skill 不是强制外调。** 先判断任务是否值得委派。简单算术、普通问答、无需文件实现的解释，直接由 Codex 回答，不为启动工具而编造文件任务，不运行连接测试。高风险任务或缺少可靠验收证据时，也由桌面 Codex 自己处理。

## 使用前

1. 仅对适合委派的任务运行本 Skill 的 `scripts/bridge.py status`，它不调用模型。以本文件所在目录定位脚本，不写死用户名或项目路径。标准库 Python 3.9+；安装器在 `.bridge-install.json` 保存解释器和 Codex CLI 路径，可读取此元数据。
2. 尚未配置时运行 `scripts/bridge.py configure` 打开本机三字段设置页。让用户在页面输入 API 地址、Key、模型；不要要求粘贴到聊天，不读取 `profile.json`、钥匙串或旧工具凭据。也可运行安装包中的 `configure.command`。取得 URL 后继续，不等待服务退出。
3. 用户点“测试连接”会消耗少量供应商额度，验证对话和工具往返；安装和 status 不消耗 API。独立初审另需 JSON 对象输出能力。需要 Chat Completions、函数工具和 JSON 对象输出。不要因模型不能执行而自动换供应商。

## 划定任务与图像边界

只委派低风险、范围清楚、可以固定本地检查的一个文件子任务。认证、支付、安全、生产发布、破坏性数据库改动、关键业务规则，以及缺少可验证标准的修改，由桌面 Codex 处理。选入的文本会发给用户指定服务；仅选必需 UTF-8 文件，不带凭据、聊天历史、整个项目或无关材料。需要外发授权时先取得授权，已配置 Key 不等于授权外发任意材料。

当前外部执行器只有文本消息与文件读写、受限命令工具，**没有图片输入、浏览器、截图或桌面插件**。外部模型是纯文本模型也可以执行适合文本的任务；选了能识图的模型也不会自动得到图像输入。

遇到图片参考或可视化要求，桌面 Codex 先查看材料，将实现所需布局、元素、坐标、文字和约束提炼为文字合同；无法可靠文字化的部分自己完成。HTML 等可视任务的固定检查只证明结构与可检查行为，最终画面、动作和交互由桌面 Codex 打开浏览器核对。不得声称外部模型看过图片，或用代码包含某元素替代视觉验收。

## 固定合同

桌面 Codex 执行前制定任务与验收，不交给执行者自行放宽标准。示例见 `references/task-example.json`，workspace 换成实际绝对路径。

```json
{
  "workspace": "/absolute/project/path",
  "task": "明确的一个子任务",
  "risk_level": "low",
  "input_files": ["test_feature.py"],
  "writable_files": ["feature.py"],
  "verification_files": ["test_feature.py"],
  "acceptance": ["已确认的行为与边界要求"],
  "acceptance_checks": [{
    "name": "固定验收",
    "argv": ["python3", "-m", "unittest", "test_feature.py"],
    "timeout_seconds": 30,
    "success_contains": ["Ran 8 tests", "OK"]
  }]
}
```

每条重要标准应能对应观察方法和证据。按实际测试数填写成功证据，不照抄示例；验收文件为只读输入，不能列入 writable_files。可创建新输出，不自动集成删除，不复制目录、二进制或符号链接；最多64文件，单文件2MiB、总计16MiB。依赖完整大型构建环境的任务由桌面 Codex 处理。

外部文本初审只检查 `acceptance` 与固定检查证据；实际渲染、外观、动作、桌面观察等放在 `desktop_acceptance` 文字列表，由Codex真实查看后验收。最终报告必须覆盖两类标准，不能把文本初审通过当成视觉通过。

## 执行与有限修复

使用已检测的 Python 解释器运行唯一的混合流程：

```sh
python3 <skill_dir>/scripts/bridge.py run --mixed --task-file /absolute/task.json --timeout 300
```

可先加 `--dry-run` 检查合同和复制范围，不读取凭据、不调用模型。没有 `--mode` 参数；旧的 `--mode strict` 会报错，不静默替换流程。流程为外部实现 → 不可修改的固定检查 → 全新无工具、无执行历史的独立初审 → 有针对性的有限修复并重测 → 精简交接。初审使用配置的同一个模型。

自动修复最多两轮，`--max-repairs 0` 可关闭。总时间最多1800秒，每个执行阶段最多16轮模型请求。每阶段可能有多次 API 请求，`model_stages` 不是请求数或费用上限。命令必须使用 Codex CLI macOS 沙箱并断网，不退回裸终端。

## 桌面验收与失败处理

正常 stdout 为精简交接，不反复加载全部日志。`needs_desktop_review` 表示等待桌面验收，不等于完成。Codex 核对差异、固定检查和独立初审，为每条重要标准记录 **`status: pass | fail | unverified` 与 `evidence`**：通过需有相应测试、位置或实际观察，无法检查就写 unverified；视觉标准需实际渲染证据。模型自述不算通过证据，抽查不能替代关键标准。无法保证检查发现所有缺陷，向用户准确说明仍未覆盖的部分。

发现不通过时，先定位“哪条标准、哪里偏离、什么证据”，然后分类处理：

- **合同或方案错误**：检查自己是否遗漏、冲突或错误描述需求，纠正合同；不能为迁就产物而放宽用户标准。
- **实现错误**：把具体文件、现象、预期与复现证据交给外部模型，只修相关部分；修后重跑受影响检查与必要回归。
- **验收缺口**：补充能验证真实要求的检查或视觉观察；缺证据期间不得判通过。
- **环境问题**：先排查依赖、权限、工具和运行条件，不要求模型重复生成同样实现来碰运气。

禁止原样盲目重试。自动修复用尽后，不得通过新建任务重置自动修复预算。同一原始任务内，桌面纠正后重新委派最多再两轮，每轮设置 `--max-repairs 0`，并先设剩余时间或费用上限（默认每轮不超过300秒）。连续两次没有新的有效证据或改善、范围或风险扩大、预算耗尽、需要用户决策、无法建立可靠验收时停止委派，由桌面接手或明确报告未完成。

只有重要标准均已验收且确认可集成时运行：

先将 `desktop-review.template.json` 另存为同目录 `desktop-review.json`。保留合同和副本哈希，逐项填 `status: pass` 及非空 `evidence`，全部核对后设置 `decision: accept`。存在 fail / unverified 或缺证据时不集成；`--desktop-reviewed` 单独不再足够。报告是 Codex 的结构化验收记录，程序只能检查格式、绑定关系和完整性，不能证明判断一定正确。

外部初审blocked或changes_requested时先看副本、`review-unvalidated.txt`和`validation.json`，区分真实缺陷与协议问题，不因为格式失败重写全部代码。当前轮执行及固定检查均成功且文件未变时，Codex可在模板的 `external_review_resolution` 填理由，并为每个id记录 `status: resolved` 和新增证据。证据不足仍保持未验证；worker/检查失败或哈希失效不可用人工报告覆盖。裁决后仍重跑集成检查。

```sh
python3 <skill_dir>/scripts/bridge.py apply --run-dir /absolute/local/run --desktop-reviewed
```

程序校验原项目和副本哈希，备份后只复制允许文件，并在原项目重跑固定检查。异常时读取 summary 和 original-backup，桌面 Codex 决定恢复或修复并说明结果。不要自动提交、合并或发布，按用户明确授权处理。

## 用量记录

每次外部请求开始与返回都会立即写入 run-dir/usage-ledger.jsonl；summary 的 `external_usage` 给出实际请求数、输入、缓存输入、输出和推理子项。失败也保留；无 usage、请求中断或未结束时总用量标为 unknown / null，已确认部分在 `known_totals`，不得解释为免费。缓存属于输入，推理属于输出，不再加一次。

Codex 的用量须从当前会话记录或独立运行的 `turn.completed.usage` 采集，不能用外部执行窗口代替完整 Codex 任务。独立协调器需在候选任务结束后读取，包含规划、交接、验收和最终回答。记录 Token 不等于 Pro 计费权重或周额度；缺少子代理记录时明确覆盖范围。周用量百分比是账号共享的粗粒度观测，不能反推单任务 Token。

用户要求测用量时，读取 [采集命令和范围说明](references/measurement.md)，再调用无模型请求的 `measure-start` / `measure-end`。

## 数据与边界

- Key 存在本机 `~/Library/Application Support/CodexModelBridge/profile.json`，权限600。不发给 OpenAI或写入项目日志，但作为认证信息发送到配置的 API 地址；改地址需重新输入 Key。
- 本地 Python 调用独立供应商计费，不更改 Codex 认证和默认模型。任务拆分、交接、阅读结果、视觉验收仍使用 Codex 额度，不承诺固定省额比例。
- 外部模型只操作副本，不能直接调用桌面插件或访问用户凭据。`run_command` 经沙箱执行，无网络；不可用就失败。
- runs 可能含代码和检查输出，不随发布包分发。Key 不进聊天、Git、截图；只分享干净源码与 ZIP。
