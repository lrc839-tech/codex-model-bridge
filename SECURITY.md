# Security

不要在 GitHub Issue、截图、聊天、测试夹具或 PR 中提交 API Key、profile.json 或私密运行材料。报告可复现问题时先替换为假 Key 和合成文件。

已保存的配置和运行记录位于用户本机的 Application Support，而非源码目录。设置页只绑定127.0.0.1，验证Host/会话/CSRF，API不返回保存的Key。接口适配器验证HTTPS并拒绝重定向。命令工具依赖当前Codex CLI的macOS沙箱，失败不降级。

本工具不提供抵御拥有当前用户文件权限的本机恶意程序的安全边界。外部模型输出需要桌面Codex最终审阅；固定检查和独立模型初审不能证明所有语义正确。
