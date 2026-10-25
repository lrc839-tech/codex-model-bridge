# Codex Model Bridge

**桌面 Codex 负责规划和最终决定，按需把执行、固定检查后的独立初审交给用户自己的模型 API。**

默认继续纯 Codex。每次明确调用 `$codex-model-bridge` 才启用混合模式，不替换 Codex 默认模型，不修改 Codex 登录或模型配置。首版 macOS，MIT 开源。

## 下载和安装

从 [Releases](https://github.com/lrc839-tech/codex-model-bridge/releases/latest) 下载 `codex-model-bridge-v0.1.0-mac.zip`，也可把同一个 ZIP 发到微信群。

1. 解压 ZIP，运行 `install.command`。
2. 自动检查本机 Python 3.9+ 和桌面 Codex 的 CLI，复制完整 Skill 到 `~/.agents/skills/codex-model-bridge`。
3. 安装器打开本机配置页，填三个字段，测试后保存。
4. 在桌面 Codex 新聊天中输入 `$codex-model-bridge`，并描述任务。如果列表未刷新，重启桌面 Codex。

需要已安装桌面 Codex 和可用的 [Python 3](https://www.python.org/downloads/macos/)。本工具只用 Python 标准库，不需要 npm、pip、管理员权限或另外的 OpenAI API Key。首版不自动下载安装 Python；缺失时安装器给出提示。已在 Apple Silicon Mac 验证，Intel Mac 的实机安装尚未验证。Windows 尚不支持。

macOS 可能要求对下载的 `.command` 脚本“右键 → 打开”。也可在终端进入解压目录运行 `bash install.command`。首次安装保留终端窗口供本地设置页使用；服务空闲30分钟退出。后续随时运行 `configure.command` 重新打开。`uninstall.command` 只移除本工具安装的 Skill，保留个人配置与历史记录。升级会先备份旧 Skill。

**为什么先用 ZIP？** 这是 Skill 和本地辅助工具，ZIP 能直接检查源码、转发和标准安装。DMG 只是另一种封装，不会额外提供桌面应用能力；首版没有签名的独立原生 App。不要把源码 ZIP 的一键安装描述成零前置依赖。

## 三个字段在哪填

安装后自动打开 `127.0.0.1` 上的本机网页，也可以双击 `configure.command` 打开。

| 字段 | 填写内容 |
| --- | --- |
| API 链接 | 服务商提供的 **Chat Completions 兼容接口基础地址**；也接受完整 `/chat/completions` 地址 |
| API Key | 自己在对应服务商申请的 Key，以密码框输入 |
| 模型 | 可从接口拉取列表，也可以手动输入模型 ID；豆包也可能是 `ep-...` 推理接入点 |

![本机三字段配置页（演示数据）](docs/configuration.png)

页面预设 DeepSeek、千问、豆包、Kimi、智谱和自定义服务。预设只帮助填地址，不限制模型名字。换 API 地址后必须重新填 Key，避免把旧 Key 发给另一家服务。

| 供应商 | 基础地址示例 | 官方文档 |
| --- | --- | --- |
| DeepSeek | `https://api.deepseek.com/v1` | [接口与思考模式](https://api-docs.deepseek.com/guides/thinking_mode/) |
| 千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | [Chat Completions](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions) |
| 豆包 | `https://ark.cn-beijing.volces.com/api/v3` | [OpenAI 兼容接口](https://docs.volcengine.com/docs/ark/compatible-with-openai-sdk?lang=en) |
| Kimi | `https://api.moonshot.cn/v1` | [模型及参数](https://platform.kimi.com/docs/api/models-overview) |
| 智谱 | `https://open.bigmodel.cn/api/paas/v4` | [OpenAI 兼容接口](https://docs.bigmodel.cn/cn/guide/develop/openai/introduction) |

请以自己控制台提供的地址为准。千问可能使用工作空间、区域或套餐专用地址；豆包模型权限和接入点取决于账号。需要支持 **Chat Completions + 函数工具调用** 的文本模型；省额档另需 JSON 对象输出。多模态、图像、语音、仅思考或其他协议不在首版保证范围。未知模型不强加私有思考参数；部分新模型无法关闭思考，费用也会不同。

“测试连接”会验证实际工具调用往返，会使用你所选供应商的 API 额度。拉取模型列表失败或不支持，不等于手动模型一定不能用。一个品牌接入成功也不代表该品牌每个模型均兼容。

## 如何选择纯 Codex 或混合

- **纯 Codex**：正常使用桌面 Codex，不调用本 Skill。
- **混合省额档**：输入 `$codex-model-bridge 本次用混合省额模式，帮我完成……`。桌面 Codex 制定小任务和固定验收，外部模型执行、控制器运行检查、全新请求独立复审；失败最多修复两次，然后交回桌面 Codex。
- **混合严格档**：明确要求 `strict`。外部模型只做有限执行，桌面 Codex 完整复核和验收。

最终决定和整体工作流仍由当前桌面 Codex 负责。只有它审阅后才将允许的文件从副本集成回原项目。省额档集成后重跑固定检查，有原文件备份。检查失败、范围越界、证据变化、超时或复审不确定时，不自动宣称完成。

省额档旨在减少桌面 Codex 反复读工具日志、调试和初审的上下文。**没有固定省额比例保证**，拆任务、交接、最终审阅仍会使用 Codex 额度，外部服务独立计费。`model_stages` 计阶段，不是 API 请求数或费用上限。

## 数据和执行边界

配置只在 `~/Library/Application Support/CodexModelBridge/profile.json`，文件权限600、目录700，不放进 Skill、项目、GitHub 或分享 ZIP。设置页不回显已保存的 Key，不使用浏览器持久存储，只绑定本机回环地址，校验 Host、会话和 CSRF。Key 会作为认证头发送给你填写的 API 地址；远程地址必须 HTTPS，拒绝重定向。

桌面 Codex 选入合同的文件内容会发送到该 API。安装本工具不代表所有项目都适合外发，请只选必要文件。外部模型可用的工具仅有选定文件的读写与隔离命令；终端命令不带 API Key，必须通过 Codex CLI macOS 沙箱，命令无网络权限。沙箱不可用就失败，不退回裸终端。

首版只支持小型 UTF-8 文件任务：最多64文件，单个2MiB、总计16MiB。依赖完整大型构建环境、二进制、任意目录复制、删除文件、生产操作和自动发布不在执行范围。本工具不是远程代理服务，也没有自动切换供应商或无限重试。

## 标准 Skill / 插件安装

仓库中的 `skills/codex-model-bridge` 是完整、自包含的 Skill，可以整体复制到自己的 Skills 目录，或用 Codex 的 skill-installer 从此 GitHub 路径安装。不要只复制 SKILL.md。首次运行配置命令设置自己的三字段。

也提供标准插件清单和 marketplace，可在支持以下命令的新版 Codex CLI 中使用：

```sh
codex plugin marketplace add lrc839-tech/codex-model-bridge
codex plugin add codex-model-bridge@codex-model-bridge
```

通过标准 Skill 或插件管理器安装时，直接在桌面 Codex 调用本 Skill 打开配置，卸载也用对应管理器；ZIP 的 configure/uninstall 入口针对本安装器的记录。

选择 ZIP、手动 Skill 或插件其中一种即可，避免重复出现同名 Skill。插件元数据遵循 [官方格式](https://developers.openai.com/plugins/build/plugins)。不同桌面版本的插件入口可能不同；当前首版以 Mac ZIP 安装方式验收。

## 开发与验证

```sh
PYTHONPATH=skills/codex-model-bridge python3 -m unittest discover -s tests -v
python3 scripts/build_release.py
python3 examples/prepare_demo.py
python3 skills/codex-model-bridge/scripts/bridge.py status
python3 skills/codex-model-bridge/scripts/bridge.py configure
```

`prepare_demo.py` 只创建一次性演示工作区和8项固定测试，不调用 API。由桌面 Codex 检查合同后，才执行 `bridge.py run --mixed --task-file <生成的task.json>`。运行记录在本机配置目录的 `runs`，不会随发布包分发。

发布包采用文件白名单、个人绝对路径和疑似 Key 检查，生成可复现 ZIP、`SHA256SUMS` 及包内逐文件哈希。GitHub Actions 对标签 `v*` 构建 Release。验收范围见 [验证记录](docs/verification.md)。

本项目为独立社区工具，不代表 OpenAI 或各模型服务商官方产品。使用自己的服务账号，并遵守相应服务条款。报告问题时不要附 API Key 或原始私密运行记录。
