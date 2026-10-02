# 独立模型 runner 预检

实际运行 `codex-cli 0.160.0`，显式 `gpt-6-astra`、`model_reasoning_effort="max"`、ephemeral 新会话。stdout.jsonl 的 READY 请求返回真实 usage；没有用 JSON bytes 替代 token。

mcp/ 首次 pilot 的模型 turn 虽结束，但 MCP 调用被“requires approval / never policy”拦截，不能作为有效工具结果。未执行任何案例，不计入 A/B。

按用户已授权的本地只读评测，第二次只在子进程命令行给本次白名单 TraceWeave server 设置 `default_tools_approval_mode="approve"`，保留 shell read-only sandbox；没有编辑用户 config.toml。该配置项见 [OpenAI 官方配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)。mcp-approved/ 的 pilot 成功调用一次 get_diagnostic_snapshot，并有原始响应、导入模块 SHA 和 usage。无案例答案进入 pilot。

两组都使用同一评测服务器白名单与权限。A 隐藏并阻止 include_dependencies，B 暴露该可选参数。已核对在白名单非目标工具中没有调用修改后的 npi_dynamic 路径；A 的 driver 查询不触发 dynamic step，保持 P0 输出。产品代码共同基线为 651a91c，P1 为 010e6df；正式实验仍需独立冻结并运行。
