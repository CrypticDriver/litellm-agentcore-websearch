"""Minimal SigV4 MCP client for AgentCore Gateway Web Search.

Standalone (boto3 only) so it can be dropped into any LiteLLM deployment.
"""
import json
import os
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

GATEWAY_URL = os.environ["AGENTCORE_GATEWAY_URL"]  # required
GATEWAY_REGION = os.environ.get("AGENTCORE_GATEWAY_REGION", "us-east-1")

_session = boto3.Session()


def _mcp_call(payload: dict) -> dict:
    body = json.dumps(payload).encode()
    req = AWSRequest(
        method="POST",
        url=GATEWAY_URL,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
    )
    SigV4Auth(_session.get_credentials(), "bedrock-agentcore", GATEWAY_REGION).add_auth(req)
    http_req = urllib.request.Request(GATEWAY_URL, data=body, headers=dict(req.headers), method="POST")
    with urllib.request.urlopen(http_req, timeout=30) as resp:
        raw = resp.read().decode()
    # Gateway may answer JSON or SSE depending on Accept negotiation
    if raw.startswith("event:") or raw.startswith("data:"):
        for line in raw.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise RuntimeError(f"no data line in SSE response: {raw[:200]}")
    return json.loads(raw)


def list_tools() -> list:
    out = _mcp_call({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    return out["result"]["tools"]


def web_search(query: str, max_results: int = 10) -> list:
    """Returns a list of {title, url, date, text} dicts."""
    tool_name = os.environ.get("AGENTCORE_SEARCH_TOOL", "web-search-tool___WebSearch")
    out = _mcp_call({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": tool_name, "arguments": {"query": query, "maxResults": max_results}},
    })
    if "error" in out:
        raise RuntimeError(f"MCP error: {out['error']}")
    content = out["result"]["content"]
    results = []
    for block in content:
        if block.get("type") == "text":
            parsed = json.loads(block["text"])
            items = parsed.get("results", parsed) if isinstance(parsed, dict) else parsed
            for it in items:
                results.append({
                    "title": it.get("title", ""),
                    "url": it.get("url", ""),
                    "date": it.get("publishedDate") or it.get("date"),
                    "text": it.get("text") or it.get("snippet", ""),
                })
    return results


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "list":
        print(json.dumps(list_tools(), indent=2)[:3000])
    else:
        q = sys.argv[1] if len(sys.argv) > 1 else "latest AWS Bedrock AgentCore news"
        for r in web_search(q, 3):
            print(json.dumps(r, ensure_ascii=False)[:300])
