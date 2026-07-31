"""Sanad knowledge tools for Burooj Build and Design modes.

Registers sanad_search and sanad_get_chunk as model-callable tools so that
Build and Design modes can retrieve company standards, policies, and SOPs
as evidence during work. The architecture spec (section 6, phase B4) requires
these tools in both toolsets so company standards become retrievable evidence.

The tools proxy to the Sanad HTTP API (default http://127.0.0.1:8787).
They are service-gated: if Sanad is not running, the check_fn fails and
the tools are silently excluded from the schema sent to the model.
"""

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Dict

from tools.registry import registry

logger = logging.getLogger("hermes.sanad_tools")

_SANAD_BASE_URL_ENV = "SANAD_BASE_URL"
_SANAD_DEFAULT_URL = "http://127.0.0.1:8787"


def _sanad_base_url() -> str:
    return os.environ.get(_SANAD_BASE_URL_ENV, _SANAD_DEFAULT_URL)


def check_sanad_available() -> bool:
    """Return True if the Sanad API is reachable."""
    url = f"{_sanad_base_url()}/health"
    try:
        req = urllib.request.Request(url, method="GET")
        resp = urllib.request.urlopen(req, timeout=3)
        if resp.status == 200:
            data = json.loads(resp.read())
            return data.get("status") == "ok"
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError):
        pass
    return False


# --- sanad_search ---

SANAD_SEARCH_SCHEMA = {
    "name": "sanad_search",
    "description": (
        "Search the company knowledge base (Sanad) for policies, SOPs, product docs, "
        "and standards. Returns source-backed chunks with citations. Use this before "
        "asserting company facts. If results are empty, say you lack evidence."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query. Be specific: include topic, department, or document name if known.",
            },
            "top_k": {
                "type": "integer",
                "description": "Number of results to return (default 8, max 24).",
                "default": 8,
            },
        },
        "required": ["query"],
    },
}


def handle_sanad_search(args: Dict[str, Any], **kwargs: Any) -> str:
    """Execute a Sanad search and return formatted results."""
    query = args.get("query", "")
    top_k = min(args.get("top_k", 8), 24)

    if not query.strip():
        return json.dumps({"error": "query is required"})

    body = {
        "query": query,
        "principal": {"user_id": "burooj_agent", "roles": ["standard"], "departments": []},
        "top_k": top_k,
        "candidate_limit": top_k * 3,
    }

    url = f"{_sanad_base_url()}/v1/search"
    try:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=15)
        result = json.loads(resp.read())
        return json.dumps(result, indent=2)
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        return json.dumps({"error": f"Sanad returned HTTP {exc.code}", "detail": error_body})
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return json.dumps({"error": f"Cannot reach Sanad: {exc}"})


# --- sanad_get_chunk ---

SANAD_GET_CHUNK_SCHEMA = {
    "name": "sanad_get_chunk",
    "description": (
        "Fetch a single knowledge chunk by ID to expand a citation or read the full "
        "text of a retrieved result. Use after sanad_search to get more context."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "chunk_id": {
                "type": "string",
                "description": "The chunk ID from a sanad_search result.",
            },
        },
        "required": ["chunk_id"],
    },
}


def handle_sanad_get_chunk(args: Dict[str, Any], **kwargs: Any) -> str:
    """Fetch a single chunk from Sanad by ID."""
    chunk_id = args.get("chunk_id", "")
    if not chunk_id.strip():
        return json.dumps({"error": "chunk_id is required"})

    url = f"{_sanad_base_url()}/v1/chunks/{chunk_id}"
    try:
        req = urllib.request.Request(url, method="GET")
        resp = urllib.request.urlopen(req, timeout=10)
        result = json.loads(resp.read())
        return json.dumps(result, indent=2)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return json.dumps({"error": f"Chunk not found: {chunk_id}"})
        error_body = exc.read().decode("utf-8", errors="replace")
        return json.dumps({"error": f"Sanad returned HTTP {exc.code}", "detail": error_body})
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return json.dumps({"error": f"Cannot reach Sanad: {exc}"})


# --- Registration ---

registry.register(
    name="sanad_search",
    toolset="sanad",
    schema=SANAD_SEARCH_SCHEMA,
    handler=handle_sanad_search,
    check_fn=check_sanad_available,
    emoji="📚",
)

registry.register(
    name="sanad_get_chunk",
    toolset="sanad",
    schema=SANAD_GET_CHUNK_SCHEMA,
    handler=handle_sanad_get_chunk,
    check_fn=check_sanad_available,
    emoji="📄",
)
