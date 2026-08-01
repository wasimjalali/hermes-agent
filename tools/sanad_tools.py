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
import urllib.parse
import urllib.request
from typing import Any, Dict

from tools.registry import registry

logger = logging.getLogger("hermes.sanad_tools")

_SANAD_BASE_URL_ENV = "SANAD_BASE_URL"
_SANAD_DEFAULT_URL = "http://127.0.0.1:8787"

# Cap on a single tool result. A broad query against a large corpus could
# otherwise return megabytes of chunk text and blow the context window.
_MAX_RESULT_CHARS = 24000


def _truncate(text: str, limit: int = _MAX_RESULT_CHARS) -> str:
    """Trim an oversized response, saying so rather than silently cutting."""
    if len(text) <= limit:
        return text
    return (
        text[:limit]
        + f"\n\n... [truncated at {limit} chars. Narrow the query or lower top_k.]"
    )


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


def _current_principal() -> Dict[str, Any]:
    """Build the Sanad principal for the caller.

    Sanad filters by permission *before* the chunks reach the model, so who is
    asking is the whole point. Hardcoding one synthetic identity made every
    Build and Design session search as the same fixed user, which left the ACL
    layer running and semantically meaningless.

    The gateway publishes the session's identity through these variables. When
    none is set, fall back to the least-privileged principal rather than a
    broadly-scoped one: under-fetching is a visible "not enough evidence",
    over-fetching is a silent disclosure.
    """
    user_id = os.environ.get("SANAD_PRINCIPAL_USER_ID", "").strip()
    roles_raw = os.environ.get("SANAD_PRINCIPAL_ROLES", "").strip()
    departments_raw = os.environ.get("SANAD_PRINCIPAL_DEPARTMENTS", "").strip()

    if not user_id:
        logger.debug("No SANAD_PRINCIPAL_USER_ID set; using least-privileged principal")
        return {"user_id": "anonymous", "roles": [], "departments": []}

    return {
        "user_id": user_id,
        "roles": [r.strip() for r in roles_raw.split(",") if r.strip()],
        "departments": [d.strip() for d in departments_raw.split(",") if d.strip()],
    }


def _coerce_top_k(raw: Any, default: int = 8, maximum: int = 24) -> int:
    """Clamp a model-supplied top_k into range without raising.

    ``min(args.get("top_k", 8), 24)`` raised TypeError the moment the model
    sent a string or null, and let a negative value through unchecked.
    """
    if isinstance(raw, bool) or raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, maximum))


def handle_sanad_search(args: Dict[str, Any], **kwargs: Any) -> str:
    """Execute a Sanad search and return formatted results."""
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return json.dumps({"error": "query is required and must be a non-empty string"})
    top_k = _coerce_top_k(args.get("top_k"))

    body = {
        "query": query,
        "principal": _current_principal(),
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
        return _truncate(json.dumps(result, indent=2))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")[:2000]
        return json.dumps({"error": f"Sanad returned HTTP {exc.code}", "detail": error_body})
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError) as exc:
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
    chunk_id = args.get("chunk_id")
    if not isinstance(chunk_id, str) or not chunk_id.strip():
        return json.dumps({"error": "chunk_id is required and must be a non-empty string"})

    # Percent-encode with an empty safe set. The id comes from the model, and
    # raw interpolation let a value like '../../admin' walk to a different
    # Sanad endpoint than the one this tool is scoped to.
    quoted = urllib.parse.quote(chunk_id.strip(), safe="")
    url = f"{_sanad_base_url()}/v1/chunks/{quoted}"
    try:
        req = urllib.request.Request(url, method="GET")
        resp = urllib.request.urlopen(req, timeout=10)
        result = json.loads(resp.read())
        return _truncate(json.dumps(result, indent=2))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return json.dumps({"error": f"Chunk not found: {chunk_id}"})
        error_body = exc.read().decode("utf-8", errors="replace")[:2000]
        return json.dumps({"error": f"Sanad returned HTTP {exc.code}", "detail": error_body})
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError) as exc:
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
