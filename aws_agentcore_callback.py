"""LiteLLM custom callback: web_search interception backed by AgentCore Gateway Web Search.

Subclasses LiteLLM's built-in WebSearchInterceptionLogger and swaps only the
search execution step: instead of litellm.asearch() (Perplexity/Tavily/...),
queries are sent to an Amazon Bedrock AgentCore Gateway web-search target via
SigV4-signed MCP tools/call. Everything else (native web_search_2025xxxx tool
conversion, tool_choice rewrite, agentic loop, result injection) is reused.

config.yaml:
    litellm_settings:
      callbacks: aws_agentcore_callback.aws_agentcore_websearch_handler
      websearch_interception_params:
        enabled_providers: ["bedrock"]

env:
    AGENTCORE_GATEWAY_URL     (required) e.g. https://xxx.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp
    AGENTCORE_GATEWAY_REGION  (default us-east-1)
    AGENTCORE_SEARCH_TOOL     (default web-search-tool___WebSearch)
    AGENTCORE_MAX_RESULTS     (default 10)
"""
import asyncio
import os
from typing import Any, Optional, Tuple

from litellm._logging import verbose_logger
from litellm.integrations.websearch_interception.handler import (
    WebSearchInterceptionLogger,
)
from litellm.integrations.websearch_interception.transformation import (
    WebSearchTransformation,
)
from litellm.llms.base_llm.search.transformation import SearchResponse, SearchResult

import aws_agentcore_search


class AgentCoreWebSearchInterception(WebSearchInterceptionLogger):
    def __init__(self) -> None:
        super().__init__(enabled_providers=["bedrock"])
        self.max_results = int(os.environ.get("AGENTCORE_MAX_RESULTS", "10"))

    async def _execute_search(
        self, query: str, kwargs: Optional[dict[str, Any]] = None
    ) -> Tuple[str, Optional[SearchResponse]]:
        verbose_logger.debug(f"AgentCoreWebSearch: executing search '{query}'")
        try:
            # AgentCore web-search query hard limit is 200 chars
            raw = await asyncio.to_thread(
                aws_agentcore_search.web_search, query[:200], self.max_results
            )
        except Exception as e:
            verbose_logger.error(f"AgentCoreWebSearch: search failed for '{query}': {e}")
            raise

        structured = SearchResponse(
            results=[
                SearchResult(
                    title=r.get("title") or "",
                    url=r.get("url") or "",
                    snippet=r.get("text") or "",
                    date=r.get("date"),
                )
                for r in raw
            ]
        )
        text = WebSearchTransformation.format_search_response(structured)
        verbose_logger.debug(
            f"AgentCoreWebSearch: got {len(structured.results)} results, {len(text)} chars"
        )
        return text, structured


aws_agentcore_websearch_handler = AgentCoreWebSearchInterception()
