---
name: codex-model-bridge
description: "用户明确选择混合模式时，让当前桌面 Codex 规划和最终决策，通过本地工具将有限文件子任务的执行与初检交给用户配置的 DeepSeek、千问、豆包、Kimi、智谱或兼容模型。用户要求纯 Codex、普通问答、凭据处理和发布操作时不自动委派。"
---

# Codex Model Bridge

当前桌面 Codex 始终负责整体工作流规划、任务拆分、风险判断和最终决定。默认纯 Codex；仅在用户明确调用本 Skill 或说明本次使用混合模式时执行。`--mixed` 是显式选择的记录，不代表可以自己替用户切换模式。纯 Codex 不运行本工具，不调用外部模型。

## 使用前

1. 运行本 Skill 的 `scripts/bridge.py status`，它不调用模型。以本文件所在目录定位脚本，不写死电脑用户名或项目路径。标准库 Python 3.9+；安装器会在 `.bridge-install.json` 保存可用解释器和 Codex CLI 路径，可以读取此安装元数据。
2. 尚未配置时运行 `scripts/bridge.py configure`，打开本机三字段设置页。让用户在页面输入 API 地址、Key、模型；不要要求粘贴到聊天，不读取 `profile.json`、钥匙串或旧工具凭据。可告诉用户打开安装包中的 `configure.command`。配置服务是持续进程，取得 URL 后继续工作，不等待服务退出。
3. 用户点“测试连接”可验证模型工具调用往返；只有测试会消耗供应商 API 额度。安装和 status 不消耗模型 API。模型需要支持 Chat Completions 和函数工具。无法确认具体模型时让用户在本机页面测试；不要用另一个模型自动兜底。

## 决定任务范围

优先一次明确子任务，减少桌面 Codex 反复搬运上下文。复制的文本会发送到用户指定服务。只选任务必须的 UTF-8 文件，不带凭据、聊天历史、整个项目或无关商业材料。需要用户授权的外发，先取得授权；不要把“已经配置 Key”视为对任意材料外发的授权。

省额档 `saver` 用于低风险、验收标准明确、可以固定本地检查的文件改动。流程是外部模型执行 → 控制器运行不可修改的固定检查 → 新的无工具、无执行历史请求复审 → 最多两次修复并重测 → 精简交接桌面 Codex。复审使用配置页选择的同一个模型，无需第四项模型配置。

风险不明确、认证、支付、安全、生产发布、破坏性数据库改动、关键业务规则，以及没有可验证标准的修改，交由桌面 Codex 自己处理，或选择 `strict` 将有限执行交给外部模型、由桌面 Codex 完整验收。严格档不声称外部初检通过。用户要求纯 Codex 时直接自行完成。

## 固定合同

由桌面 Codex 在执行之前生成任务 JSON；任务和测试不交给执行者来制定。示例见 `references/task-example.json`，其中 workspace 需要替换成实际绝对路径。

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

必须确保检查真实执行了验收内容。按实际测试数填写成功证据，不照抄示例。验收文件必须是只读输入，不能列入 writable_files。可创建新输出，但不能自动集成删除；不复制目录、二进制或符号链接；单文件最多2MiB、总计16MiB、最多64文件。第一版适合小型独立文本任务，依赖整个 node_modules 或完整构建环境的任务请由桌面 Codex 处理。

## 执行与交接

使用已检测的 Python 解释器运行：

```sh
python3 <skill_dir>/scripts/bridge.py run --mixed --mode saver --task-file /absolute/task.json --timeout 300
```

可先加 `--dry-run` 检查合同与复制范围，不读取凭据、不调用模型。严格档使用 `--mode strict`。默认最多两次修复，`--max-repairs 0` 可以关闭修复。总时间最多1800秒，每个执行阶段最多16轮模型请求，无网络命令只能运行在 Codex CLI 的 macOS 沙箱中。每阶段可能包含多次 API 请求；不要把 model_stages 当成请求次数或费用上限。

正常 stdout 仅提供精简交接。不要把全部执行日志重新读入上下文。遇到失败、未验证项、异常范围、证据变化时，读取交接中指向的详细证据，缩小任务或接手处理，不无上限重试。

结果 `needs_desktop_review` 时，桌面 Codex 检查文件差异、固定检查实际结果和独立复审结论；最终验收始终由当前桌面 Codex 负责。省额档可以按风险有选择地抽查，不能仅复述模型的“通过”。严格档应自行运行或核对必要验证。确认可集成后执行：

```sh
python3 <skill_dir>/scripts/bridge.py apply --run-dir /absolute/local/run --desktop-reviewed
```

此操作校验原项目和副本哈希，备份原文件，然后复制允许文件；省额档会在原项目中再跑固定检查。异常时看 summary 和 original-backup，由桌面 Codex 处理并向用户说明。不要自动提交、合并或发布；这些由桌面 Codex 按用户明确授权处理。

## 数据与边界

- Key 保存在本机 `~/Library/Application Support/CodexModelBridge/profile.json`，权限600。不会发给 OpenAI或写进项目日志，但会作为认证信息发给用户所选 API 地址。修改地址必须重新输入 Key。
- API 请求来自本地 Python，使用独立供应商的计费，不绕过或更改 Codex 的账号认证/默认模型配置。模型输出与检查摘要仍可能占用桌面 Codex 的额度；不承诺固定省额比例。
- 工具仅处理副本；外部模型不能直接执行终端、调用桌面工具或访问用户凭据。`run_command` 必须通过 Codex CLI 沙箱，无法启用则失败退出，不退化到裸终端。
- 本地 runs 可能包含代码与检查输出，不随分享包分发。Key不进聊天、不进 Git、不进截图。只分享源码和构建的干净 ZIP。
