# Contributing / 参与贡献

欢迎提交能复现的日期解析、页面选择器和断点恢复问题，或改进安装说明。

1. Fork 仓库并在分支上修改。
2. Python 脚本通过 `uv run` 运行；浏览器功能使用 Windows + Edge。
3. 运行相关验证：

```powershell
uv run .\tests\test_unit.py
uv run .\tests\test_browser_fixtures.py
```

浏览器测试使用临时 profile 和本地 HTML，不需要真实账号。不将真实公众号采集接入自动测试。

修改输出字段时保持 v1/v2 输入可读，更新 `references/output-schema.md`。修复故障时加入能展示外部行为的最小测试，避免只检查实现字符串。

报告问题请给出运行环境、命令（必要时脱敏）、状态和错误。不要上传登录 profile、Cookie、二维码或未脱敏的截图。分享 HTML 样本时去掉账号信息，只留下复现所需结构。

English: Please include a minimal reproduction, environment and observed status. Run the relevant tests; use local fixtures rather than live accounts. Do not upload session profiles, cookies or login QR codes. Keep output changes documented and older input formats readable.
