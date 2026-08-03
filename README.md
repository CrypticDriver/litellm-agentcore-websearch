# Claude Code Web Search on Bedrock — LiteLLM 网关侧集成方案

让 Claude Code 内置的 WebSearch 工具在「Claude Code → LiteLLM → Amazon Bedrock」链路上正常工作。
搜索由 **Amazon Bedrock AgentCore Web Search**（AWS 托管网页索引）执行，在 LiteLLM 网关侧透明完成，**Claude Code 客户端零配置、零改动**。

## 背景

Claude Code 的 WebSearch 会向模型 API 发送 Anthropic 服务端工具 `web_search_20250305`。该工具由 Anthropic API 服务端执行，Amazon Bedrock 不支持，因此经 LiteLLM 转发到 Bedrock 时会报错。

本方案利用 LiteLLM 官方内置的 **websearch interception** 机制（v1.81+，`litellm.integrations.websearch_interception`）：网关拦截 `web_search_2025xxxx` 工具，改写为普通 client tool 交给 Bedrock 上的 Claude；模型发起搜索时由网关服务端代为执行，结果以 Anthropic 原生 `web_search_tool_result` 格式注回响应。工具替换、tool_choice 改写、agentic loop、结果注入全部为 LiteLLM 官方代码；本方案仅将搜索后端从内置的第三方 SaaS（Perplexity/Tavily 等）替换为 AgentCore Web Search——子类化官方 `WebSearchInterceptionLogger`，仅重写 `_execute_search()` 一个方法。

```
Claude Code ──(/v1/messages, web_search_20250305)──▶ LiteLLM proxy
                                                       │ websearch interception（官方机制）
                                                       ├──▶ Amazon Bedrock（Claude，工具已替换）
                                                       └──▶ AgentCore Gateway /mcp（SigV4 签名）
```

## 文件说明

| 文件 | 说明 |
|---|---|
| `aws_agentcore_search.py` | 独立 SigV4 MCP 客户端（仅依赖 boto3，LiteLLM 自带） |
| `aws_agentcore_callback.py` | LiteLLM custom callback，接入官方 websearch interception |
| `litellm_config_snippet.yaml` | config.yaml 需要追加的配置行 |

## 第一步：创建 AgentCore Gateway + Web Search target（一次性）

> 官方文档：https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-connector-web-search-tool.html
> 区域建议 us-east-1（AgentCore Gateway 可与 LiteLLM/Bedrock 所在区域不同，跨区调用正常）。

### 1.1 创建 Gateway 服务角色

Gateway 需要一个可被 AgentCore 服务代入的 IAM 角色（Web Search connector 本身不需要额外权限，角色主要用于信任关系）：

```bash
aws iam create-role --role-name AgentCoreWebSearchGatewayRole \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }'
```

### 1.2 创建 Gateway（AWS_IAM 鉴权）

```bash
aws bedrock-agentcore-control create-gateway \
  --region us-east-1 \
  --name WebSearchGateway \
  --protocol-type MCP \
  --authorizer-type AWS_IAM \
  --role-arn arn:aws:iam::<ACCOUNT_ID>:role/AgentCoreWebSearchGatewayRole
```

记下返回的 `gatewayId` 和 `gatewayUrl`（形如 `https://<gatewayId>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp`）。

### 1.3 添加 Web Search connector target

```bash
aws bedrock-agentcore-control create-gateway-target \
  --region us-east-1 \
  --gateway-identifier <gatewayId> \
  --name web-search-tool \
  --target-configuration '{
    "mcp": {
      "connector": {
        "source": {"connectorId": "web-search", "version": "1.1.0"},
        "configurations": [{"name": "WebSearch", "parameterValues": {}}]
      }
    }
  }'
```

> **target 名称决定 MCP 工具名**：工具名为 `<target名>___WebSearch`。上例 target 名为 `web-search-tool`，工具名即 `web-search-tool___WebSearch`（本方案默认值）。如果用了其他 target 名，部署时需设置 `AGENTCORE_SEARCH_TOOL` 环境变量。
>
> 也可在 Bedrock AgentCore 控制台完成以上步骤：Gateways → Create gateway → 添加 target 时选择 **Web Search** connector。

### 1.4 验证 gateway 可用

用任意有权限的 AWS 身份直接测试（也可用本仓库的模块）：

```bash
export AGENTCORE_GATEWAY_URL=https://<gatewayId>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp
python3 aws_agentcore_search.py list        # 应列出 web-search-tool___WebSearch
python3 aws_agentcore_search.py "今天的AWS新闻"   # 应返回搜索结果
```

## 第二步：部署到现有 LiteLLM proxy

### 2.1 放置文件

将本仓库的 `aws_agentcore_search.py`、`aws_agentcore_callback.py` 放入 proxy 容器/主机的同一目录（例如 `/app/callbacks/`）。

- Docker：`COPY aws_agentcore_*.py /app/callbacks/`
- Kubernetes：ConfigMap 挂载即可

### 2.2 修改 config.yaml

在现有 `litellm_settings` 中追加一行（其余配置不动）：

```yaml
litellm_settings:
  callbacks: aws_agentcore_callback.aws_agentcore_websearch_handler
```

若已有 `callbacks` 列表，追加为其中一项即可。

### 2.3 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `AGENTCORE_GATEWAY_URL` | ✅ | gateway 的 MCP 端点 URL |
| `PYTHONPATH` | ✅ | 包含上述两个 py 文件的目录，如 `/app/callbacks` |
| `AGENTCORE_GATEWAY_REGION` | 默认 `us-east-1` | gateway 所在区域（SigV4 签名用） |
| `AGENTCORE_SEARCH_TOOL` | 默认 `web-search-tool___WebSearch` | target 名不同时需修改 |
| `AGENTCORE_MAX_RESULTS` | 默认 `10` | 每次搜索返回条数 |

### 2.4 AWS 凭证与 IAM 权限

搜索请求以 SigV4 签名调用 gateway，凭证走 **boto3 默认凭证链**（环境变量 → shared config → 容器/实例身份）。无论哪种凭证，其 IAM 身份都只需要这一条权限：

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "bedrock-agentcore:InvokeGateway",
    "Resource": "arn:aws:bedrock-agentcore:us-east-1:<ACCOUNT_ID>:gateway/<gatewayId>"
  }]
}
```

按部署环境二选一：

**A. 跑在 AWS 上（EKS / EC2）**：用托管身份，无需长期密钥。EKS 用 IRSA / Pod Identity，EC2 用 instance profile，把上述策略挂到对应角色即可，不需要配置任何凭证变量。

**B. 跑在非 AWS 环境（自建 / 第三方云 K8s）**：创建一个仅有上述权限的专用 IAM 用户，用其 AKSK 以环境变量注入。

> 注意：Bedrock API key（`AWS_BEARER_TOKEN_BEDROCK`）只能调 Bedrock runtime，**不能**用于 gateway 的 SigV4 签名。即使模型调用已经用 API key 跑通，搜索这条链路也需要单独一组 AKSK。

```bash
# 一次性：创建专用 IAM 用户（权限仅 InvokeGateway，泄露影响面可控）
aws iam create-user --user-name litellm-websearch
aws iam put-user-policy --user-name litellm-websearch \
  --policy-name invoke-websearch-gateway \
  --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"bedrock-agentcore:InvokeGateway","Resource":"arn:aws:bedrock-agentcore:us-east-1:<ACCOUNT_ID>:gateway/<gatewayId>"}]}'
aws iam create-access-key --user-name litellm-websearch   # 记下 AccessKeyId / SecretAccessKey
```

K8s 部署示例（Secret + Deployment env）：

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: agentcore-websearch
type: Opaque
stringData:
  access_key_id: AKIA...
  secret_access_key: "..."
---
# Deployment 的 LiteLLM 容器内追加：
env:
  - name: AWS_ACCESS_KEY_ID
    valueFrom: {secretKeyRef: {name: agentcore-websearch, key: access_key_id}}
  - name: AWS_SECRET_ACCESS_KEY
    valueFrom: {secretKeyRef: {name: agentcore-websearch, key: secret_access_key}}
  - name: AGENTCORE_GATEWAY_URL
    value: https://<gatewayId>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp
  - name: PYTHONPATH
    value: /app/callbacks
```

与模型调用凭证的关系：

- 模型走 **Bedrock API key**（`AWS_BEARER_TOKEN_BEDROCK`）时，注入的 AKSK 可能被 Bedrock SDK 的默认凭证链抢先使用导致模型调用 403。建议在 `model_list` 里把模型钉死在 API key 上：

  ```yaml
  model_list:
    - model_name: claude-sonnet
      litellm_params:
        model: bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0
        aws_region_name: us-east-1
        aws_bearer_token: os.environ/AWS_BEARER_TOKEN_BEDROCK
  ```

- 模型本来就走 AKSK 时，若两组凭证不同，同理在 `model_list` 里用 `aws_access_key_id` / `aws_secret_access_key` 显式指定模型侧凭证，避免混用。

运维提示：专用 AKSK 属长期密钥，建议纳入定期轮换；boto3 从环境读取凭证，轮换后滚动重启 pod 生效。

### 2.5 重启并验证

滚动重启 proxy 后，在任意接入该 proxy 的 Claude Code 里问一个需要联网的时新问题（如"今天纳斯达克指数"），应返回带引用的搜索结果。

网关侧验证：以 `--detailed_debug` 启动时，日志中可见：

```
AgentCoreWebSearch: executing search '...'
AgentCoreWebSearch: got N results, M chars
```

## 已知限制

| 限制 | 影响 | 说明 |
|---|---|---|
| 搜索 query ≤ 200 字符 | 低 | AgentCore 硬限制，callback 已自动截断 |
| 搜索轮次流式转非流 | 低 | LiteLLM interception 机制内置行为；Claude Code 的搜索子请求本身非流式，实测无感知 |
| 单请求搜索次数上限 3 | 低 | LiteLLM 安全阀 `max_agentic_loops=3`（可在 `websearch_interception_params` 调大）；模型连续发起 >3 次搜索会中断该轮 |
| 多轮对话 `tool_use_id` 前缀（litellm 上游 #31569） | 视版本 | 部分版本多轮回放时 Bedrock 拒绝非 `srvtoolu_` 前缀 id；1.93.0 实测单请求正常，如遇到请升级 LiteLLM |

## 后续演进

LiteLLM 上游已有原生 `bedrock_agentcore` search provider 的 PR（[BerriAI/litellm#34098](https://github.com/BerriAI/litellm/pull/34098)，配套文档 litellm-docs#702）在评审中。合入发版后，本方案可平滑切换为纯 YAML 配置（`search_tools: [{search_provider: bedrock_agentcore, ...}]` + `websearch_interception_params`），届时删除这两个 py 文件、改一行配置即可，行为完全一致。

## 验证记录

- LiteLLM 1.93.0 + 真实 Claude Code CLI 端到端验证通过（2026-07-21；2026-08-02 回归通过）
- 覆盖用例：Claude Code 请求形状 / 模型自主搜索 / 混合工具 / 流式 / 越权负例 / 长 query 截断等 8 组
