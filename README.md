# Claude Code Web Search on Bedrock（LiteLLM 网关侧集成）

Claude Code 的内置 WebSearch 走的是 Anthropic 服务端工具 `web_search_20250305`，Bedrock 不支持，经 LiteLLM 转发时报错。

本方案在 LiteLLM 网关侧透明解决：拦截该工具，改由 **Amazon Bedrock AgentCore Web Search**（AWS 托管网页索引，无需第三方搜索 API key）执行搜索，结果以 Anthropic 原生格式注回。**Claude Code 端零配置、零改动**，引用正常显示。

```
Claude Code ──(web_search_20250305)──▶ LiteLLM proxy
                                         │ websearch interception（LiteLLM 官方机制）
                                         ├──▶ Amazon Bedrock（Claude，工具已替换）
                                         └──▶ AgentCore Gateway /mcp（SigV4）
```

## 原理

LiteLLM v1.81+ 内置 websearch interception：拦截 `web_search_2025xxxx` 工具 → 模型请求搜索时由网关代为执行（agentic loop）→ 结果注回响应。工具替换、循环控制、结果注入全部是 LiteLLM 官方代码。

本仓库只做一件事：子类化官方 `WebSearchInterceptionLogger`，重写 `_execute_search()`，把搜索后端从内置的 Perplexity/Tavily 换成 AgentCore Gateway（SigV4 直调 MCP 端点）。共两个文件、约 100 行。

| 文件 | 作用 |
|---|---|
| `aws_agentcore_callback.py` | 接入 LiteLLM websearch interception 的 callback |
| `aws_agentcore_search.py` | SigV4 MCP 客户端（仅依赖 boto3），可独立运行测试 |

## 第一步：建 AgentCore Gateway（一次性）

已有带 web-search target 的 gateway 可跳过。[官方文档](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-connector-web-search-tool.html)，或用控制台（Bedrock AgentCore → Gateways → target 选 **Web Search** connector）。CLI：

```bash
# 1. 服务角色（仅需信任关系）
aws iam create-role --role-name AgentCoreWebSearchGatewayRole \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"bedrock-agentcore.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

# 2. Gateway（记下返回的 gatewayId / gatewayUrl）
aws bedrock-agentcore-control create-gateway --region us-east-1 \
  --name WebSearchGateway --protocol-type MCP --authorizer-type AWS_IAM \
  --role-arn arn:aws:iam::<ACCOUNT_ID>:role/AgentCoreWebSearchGatewayRole

# 3. Web Search target
aws bedrock-agentcore-control create-gateway-target --region us-east-1 \
  --gateway-identifier <gatewayId> --name web-search-tool \
  --target-configuration '{"mcp":{"connector":{"source":{"connectorId":"web-search","version":"1.1.0"},"configurations":[{"name":"WebSearch","parameterValues":{}}]}}}'
```

> **target 名决定工具名**：`<target名>___WebSearch`。上例即默认值 `web-search-tool___WebSearch`；用了别的 target 名就设 `AGENTCORE_SEARCH_TOOL` 环境变量。

验证：

```bash
export AGENTCORE_GATEWAY_URL=https://<gatewayId>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp
python3 aws_agentcore_search.py "AWS 最新新闻"     # 有结果即通
```

## 第二步：部署到 LiteLLM proxy

目标：让 proxy 容器内出现这两个 py 文件，且 `PYTHONPATH` 指向其目录、config.yaml 注册 callback。按部署方式选一种。

**方式 A：K8s ConfigMap 挂载**（无需改镜像，推荐）

```bash
# 1. 两个 py 文件打成 ConfigMap
kubectl create configmap agentcore-callback \
  --from-file=aws_agentcore_callback.py --from-file=aws_agentcore_search.py
```

```yaml
# 2. Deployment 里挂载到 /app/callbacks 并设环境变量
spec:
  template:
    spec:
      containers:
        - name: litellm
          env:
            - name: PYTHONPATH
              value: /app/callbacks
            - name: AGENTCORE_GATEWAY_URL
              value: https://<gatewayId>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp
          volumeMounts:
            - name: agentcore-callback
              mountPath: /app/callbacks
      volumes:
        - name: agentcore-callback
          configMap: {name: agentcore-callback}
```

config.yaml 本身通常也是 ConfigMap（LiteLLM 官方 chart 即如此），在其 `litellm_settings` 里加一行后一起更新：

```yaml
litellm_settings:
  callbacks: aws_agentcore_callback.aws_agentcore_websearch_handler
```

生效：`kubectl rollout restart deployment/<litellm>`。

**方式 B：自建镜像**

```dockerfile
FROM ghcr.io/berriai/litellm:main-stable
COPY aws_agentcore_callback.py aws_agentcore_search.py /app/callbacks/
ENV PYTHONPATH=/app/callbacks
```

config.yaml 同样加上面那一行 callbacks；`AGENTCORE_GATEWAY_URL` 等运行时再注入。

**方式 C：主机直跑**（VM / systemd）

```bash
mkdir -p /opt/litellm/callbacks && cp aws_agentcore_*.py /opt/litellm/callbacks/
# config.yaml 加 callbacks 一行，然后：
PYTHONPATH=/opt/litellm/callbacks \
AGENTCORE_GATEWAY_URL=https://<gatewayId>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp \
litellm --config config.yaml
```

**环境变量一览**：

| 变量 | 说明 |
|---|---|
| `AGENTCORE_GATEWAY_URL` | **必填**，gateway MCP 端点 |
| `PYTHONPATH` | **必填**，py 文件所在目录（如 `/app/callbacks`） |
| `AGENTCORE_GATEWAY_REGION` | gateway 区域，默认 `us-east-1` |
| `AGENTCORE_SEARCH_TOOL` | 工具名，默认 `web-search-tool___WebSearch` |
| `AGENTCORE_MAX_RESULTS` | 每次搜索条数，默认 `10` |

最后配好 **AWS 凭证**（见下节），重启 proxy。

验证：接入该 proxy 的 Claude Code 里问一个时新问题，应返回带引用的搜索结果。proxy 以 `--detailed_debug` 启动可见 `AgentCoreWebSearch: executing search '...'` 日志。

## AWS 凭证

搜索请求走 boto3 默认凭证链做 SigV4，对应 IAM 身份只需一条权限：

```json
{"Effect": "Allow", "Action": "bedrock-agentcore:InvokeGateway",
 "Resource": "arn:aws:bedrock-agentcore:us-east-1:<ACCOUNT_ID>:gateway/<gatewayId>"}
```

**跑在 AWS 上**（EKS/EC2）：把该权限挂到 IRSA / instance profile 角色，无需任何凭证配置。

**跑在非 AWS 环境**（自建 / 第三方云 K8s）：建一个仅有该权限的专用 IAM 用户，AKSK 以 Secret 注入 pod env：

```yaml
env:
  - name: AWS_ACCESS_KEY_ID
    valueFrom: {secretKeyRef: {name: agentcore-websearch, key: access_key_id}}
  - name: AWS_SECRET_ACCESS_KEY
    valueFrom: {secretKeyRef: {name: agentcore-websearch, key: secret_access_key}}
```

### 与模型凭证共存（重要）

注入的 AKSK 会被 boto3 默认凭证链优先使用。若 Bedrock 模型调用原本依赖别的凭证（如 Bedrock API key），会被这组只能搜索的 AKSK 抢走导致模型 403。解法：在 `model_list` 里显式钉住模型凭证：

```yaml
model_list:
  - model_name: claude-sonnet
    litellm_params:
      model: bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0
      aws_region_name: us-east-1
      aws_bearer_token: os.environ/AWS_BEARER_TOKEN_BEDROCK   # 模型走 Bedrock API key
```

注意：

- Bedrock API key 只能调 Bedrock runtime，**不能**签 gateway，两条链路凭证必须分开。
- 用短期 Bedrock API key（`aws-bedrock-token-generator` 生成）时，签发者身份还需 `bedrock:CallWithBearerToken` 权限；控制台长期 API key 不涉及。
- 专用 AKSK 建议纳入定期轮换，轮换后滚动重启生效。

## 已知限制

| 限制 | 说明 |
|---|---|
| query ≤ 200 字符 | AgentCore 硬限制，已自动截断 |
| 搜索轮次流式转非流 | LiteLLM interception 内置行为；CC 搜索子请求本身非流式，无感知 |
| 单请求最多 3 次搜索 | LiteLLM 安全阀 `max_agentic_loops=3`，可调；超限该轮报错 |
| pip 装 litellm 1.95.0 起不来 | 依赖未锁 fastapi 上限，报 `No module named 'proxy_server'` → `pip install "fastapi==0.118.*"`；官方 Docker 镜像不受影响 |

另：一次搜索请求实际产生 2+ 次 Bedrock 调用（agentic loop 续写），计费按实际次数。

## 后续演进

原生 `bedrock_agentcore` search provider 已提交 LiteLLM 上游（[#34098](https://github.com/BerriAI/litellm/pull/34098)，文档 [litellm-docs#702](https://github.com/BerriAI/litellm-docs/pull/702)）。合入发版后可切换为纯 YAML 配置（`search_tools` + `websearch_interception_params`），删除本仓库两个文件即可，行为一致。
