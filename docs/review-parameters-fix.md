# AI 审查请求参数修复

版本：manager-review-parameters-20261014-r1。

本次针对工具空结果已完整回传、模型仍重复搜索构建文件的故障，调整模型请求参数。没有通过增加检索预算或增加反复调查提示词处理。

## 修改

- Broker 取消对所有原生请求强制覆盖 temperature=0.1，其他模型保留 OpenCode 传来的采样参数，未指定参数交给服务端默认配置。
- 对明确选择 Qwen/vLLM 协议的 Qwen3.8-27B、Qwen3.8-27B-FP8，采用官方采样配置。关闭思考时 temperature=0.7、top_p=0.8、top_k=20、min_p=0、presence_penalty=1.5、repetition_penalty=1；开启思考时 temperature=1、top_p=0.95、presence_penalty=0，其余相同。
- 检索阶段采用 tool_choice=auto，对齐 ChipMate 普通对话自主选择工具的方式。收尾时只开放 StructuredOutput，并通过具名工具选择明确指定结果提交。
- 关闭 Qwen 思考继续发送 chat_template_kwargs.enable_thinking=false，不额外发送 reasoning_effort=none。保持固定 Schema、工具白名单、只读路径边界、历史消息和工具回执校验。

参考：本地 ChipMate Kilo 仓库的 packages/opencode/src/session/llm/native-request.ts、packages/llm/src/protocols/openai-chat.ts、packages/opencode/src/chipmate/provider/provider.ts。ChipMate 普通对话与结构化输出模式参数不同，本次对齐普通检索流程，保留审查结果的结构化提交契约。

官方采样配置：https://huggingface.co/Qwen/Qwen3.8-27B-FP8#best-practices 。

## 验证边界

验证 Broker 实际 HTTP 发出的采样参数、思考参数、工具声明与选择方式，以及网关切换至收尾阶段时完整保留工具历史。交付镜像中使用固定 OpenCode 二进制验证普通提交和预算到达后的具名提交。模型协议替身只验证工程链路，不代表真实内网模型的审查质量或循环已消失。

不宣称本次参数修复能保证任意模型服务始终返回有效审查结果。模型服务忽略工具选择、返回非法结果或不可用时仍保留真实失败；独立收尾路径及批量工具请求超额处理不是本次补丁范围，不能将未完成审查伪装成 PASS。用户目标环境复验时应核对原失败 PR 的结果、轮次、重复请求数和总耗时。

## 部署

交付为包含运行镜像的一键升级包，解压后顶层可见 upgrade.sh、gitea-manager.tar.gz、校验文件和说明。选择 Qwen/vLLM 协议并关闭思考后重新创建审查任务，已有运行任务的冻结配置不会被改写。具体命令见包内 README.md。
