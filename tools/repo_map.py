"""repo_map tool - tree-sitter parsed, PageRank-ranked code map.

Parses every source file under the workspace with tree-sitter, extracts
definitions and references, builds a file-level directed graph, ranks files
with a single-iteration PageRank pass, and greedily fills a token budget
with the highest-ranked file signatures.

Tool schema:
    repo_map(budget: int = 4096) -> { map: str, files: int, languages: list[str] }
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from agent.build_workspace import resolve_workspace
from tools.registry import registry

logger = logging.getLogger("hermes.repo_map")


def _safe_size(path: Path) -> int:
    """File size in bytes, 0 when unreadable."""
    try:
        return path.stat().st_size
    except OSError:
        return 0

# Approximate tokens per character (conservative estimate for code).
_CHARS_PER_TOKEN = 3.5

# Directories to skip during walk.
_SKIP_DIRS = frozenset({
    ".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build",
    "target", ".next", ".turbo", "vendor", ".burooj", "coverage",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "env",
})

# Extensions we attempt to parse.
_LANGUAGE_MAP: dict[str, str] = {
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".py": "python",
    ".go": "go",
    ".rs": "rust",
}

# tree-sitter node types that represent definitions, by language.
_DEF_TYPES: dict[str, set[str]] = {
    "typescript": {
        "function_declaration", "class_declaration", "method_definition",
        "interface_declaration", "type_alias_declaration", "enum_declaration",
        "export_statement",
    },
    "tsx": {
        "function_declaration", "class_declaration", "method_definition",
        "interface_declaration", "type_alias_declaration", "enum_declaration",
        "export_statement",
    },
    "javascript": {
        "function_declaration", "class_declaration", "method_definition",
        "export_statement",
    },
    "python": {
        "function_definition", "class_definition",
    },
    "go": {
        "function_declaration", "method_declaration", "type_declaration",
    },
    "rust": {
        "function_item", "impl_item", "struct_item", "enum_item",
        "trait_item", "type_item",
    },
}

# Node types that represent imports/references.
_REF_TYPES: dict[str, set[str]] = {
    "typescript": {"import_statement", "import_clause"},
    "tsx": {"import_statement", "import_clause"},
    "javascript": {"import_statement", "import_clause"},
    "python": {"import_statement", "import_from_statement"},
    "go": {"import_declaration"},
    "rust": {"use_declaration"},
}


class TreeSitterUnavailable(RuntimeError):
    """Raised when tree-sitter is missing. repo_map cannot do its job without it."""


_PARSER_CACHE: dict[str, Any] = {}


def tree_sitter_available() -> bool:
    """Whether the tree-sitter language pack can be imported."""
    try:
        import tree_sitter_language_pack  # noqa: F401
    except ImportError:
        return False
    return True


def _get_parser(language: str) -> Any:
    """Get a tree-sitter parser for the given language, cached per process.

    Raises :class:`TreeSitterUnavailable` when the language pack is missing.
    Returning None here (the original behaviour) made every file unparseable,
    which turned repo_map into an alphabetical filename listing that still
    reported success. A code map that silently contains no code is worse than
    an error, because the model treats it as ground truth.
    """
    if language in _PARSER_CACHE:
        return _PARSER_CACHE[language]

    try:
        import tree_sitter_language_pack as tslp
    except ImportError as exc:
        raise TreeSitterUnavailable(
            "repo_map needs tree-sitter. Install it with: "
            "pip install tree-sitter tree-sitter-language-pack"
        ) from exc

    try:
        parser = tslp.get_parser(language)
    except Exception:
        parser = None  # unsupported language, degrade to a filename entry
    _PARSER_CACHE[language] = parser
    return parser


def _extract_name(node: Any) -> Optional[str]:
    """Extract the name identifier from a definition node."""
    # Walk immediate children for a name/identifier node.
    for child in node.children:
        if child.type in ("identifier", "type_identifier", "property_identifier"):
            return child.text.decode("utf-8") if isinstance(child.text, bytes) else child.text
    # For export statements, look deeper.
    if node.type == "export_statement":
        for child in node.children:
            if child.type in _DEF_TYPES.get("typescript", set()) | _DEF_TYPES.get("javascript", set()):
                return _extract_name(child)
    return None


def _extract_signature(node: Any, lines: list[str], max_lines: int = 2) -> str:
    """Extract a declaration signature from a definition node.

    Takes pre-split *lines* rather than the source bytes: decoding and
    splitting the whole file once per definition made this O(defs x filesize),
    which was most of the runtime on large files.

    Emits the declaration, not the first N lines of the body. The point of the
    map is which symbols exist and what they take, so spending the token budget
    on implementation lines is exactly backwards.
    """
    start = node.start_point[0]
    if start >= len(lines):
        return ""

    # Collect lines until the parameter list closes, so a signature split
    # across lines stays intact.
    text = lines[start].rstrip()
    for offset in range(1, max_lines + 1):
        if text.count("(") <= text.count(")"):
            break
        if start + offset >= len(lines):
            break
        text = f"{text} {lines[start + offset].strip()}"

    # Cut the body at the brace that opens it: the first '{' appearing outside
    # the parameter list. A '{' inside the parameters (a destructured argument,
    # an inline object type) must not be mistaken for the body, which is what
    # made `function Button({ label }: { label: string })` come out mangled.
    depth = 0
    for i, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "{" and depth <= 0:
            text = text[:i]
            break

    text = text.rstrip()
    if text.endswith("=>"):
        text = text[:-2].rstrip()
    return text if len(text) <= 200 else text[:197] + "..."


def _extract_import_source(node: Any) -> Optional[str]:
    """Extract the module/file path from an import statement node."""
    for child in node.children:
        if child.type == "string" or child.type == "string_literal":
            text = child.text.decode("utf-8") if isinstance(child.text, bytes) else child.text
            return text.strip("'\"")
        # Python: dotted_name in from X import Y
        if child.type == "dotted_name":
            return child.text.decode("utf-8") if isinstance(child.text, bytes) else child.text
        # Recurse into module_name for Python
        if child.type == "module_name":
            return child.text.decode("utf-8") if isinstance(child.text, bytes) else child.text
    return None


_TS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def _strip_json_comments(text: str) -> str:
    """Remove // and /* */ comments so tsconfig.json parses as JSON.

    tsconfig is JSONC. Next.js ships one with comments in it.
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"(?m)^\s*//.*$", "", text)
    return re.sub(r",(\s*[}\]])", r"\1", text)


def _load_path_aliases(workspace: Path) -> dict[str, list[str]]:
    """Read compilerOptions.paths from tsconfig/jsconfig.

    Without this the graph had no edges at all on the project's own pinned
    stack: every Next.js import looks like ``@/components/button``, none of
    which is relative, so nothing resolved and PageRank ranked every file
    identically.
    """
    for name in ("tsconfig.json", "jsconfig.json"):
        config_path = workspace / name
        if not config_path.is_file():
            continue
        try:
            data = json.loads(_strip_json_comments(config_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("Could not parse %s: %s", config_path, exc)
            continue
        options = data.get("compilerOptions") or {}
        paths = options.get("paths") or {}
        base_url = options.get("baseUrl") or "."
        if not isinstance(paths, dict):
            continue
        resolved: dict[str, list[str]] = {}
        for pattern, targets in paths.items():
            if isinstance(targets, list):
                resolved[pattern] = [
                    str(Path(base_url) / t) for t in targets if isinstance(t, str)
                ]
        if resolved:
            return resolved
    return {}


def _candidate_files(base: Path, ext: str) -> list[Path]:
    """Every file path an import specifier could mean."""
    # Append the extension rather than replacing it: with_suffix turned
    # './auth.service' into 'auth.ts' by treating '.service' as the suffix.
    tries = [base]
    tries += [base.with_name(base.name + e) for e in (ext, *_TS_EXTENSIONS, ".py")]
    tries += [base / f"index{e}" for e in (ext, *_TS_EXTENSIONS)]
    tries += [base / "__init__.py"]
    return tries


def _resolve_import_to_file(
    import_source: str,
    from_file: Path,
    workspace: Path,
    ext: str,
    aliases: Optional[dict[str, list[str]]] = None,
) -> Optional[Path]:
    """Attempt to resolve an import source string to a file in the workspace."""
    if not import_source:
        return None

    # Relative imports (./foo, ../bar)
    if import_source.startswith("."):
        for candidate in _candidate_files(from_file.parent / import_source, ext):
            if candidate.is_file():
                return candidate.resolve()
        return None

    # TypeScript path aliases (@/components/button, ~/lib/utils, ...)
    for pattern, targets in (aliases or {}).items():
        prefix = pattern.rstrip("*")
        if not import_source.startswith(prefix):
            continue
        remainder = import_source[len(prefix):]
        for target in targets:
            base = workspace / target.rstrip("*").rstrip("/") / remainder
            for candidate in _candidate_files(base, ext):
                if candidate.is_file():
                    return candidate.resolve()

    # Python dotted imports
    if ext == ".py":
        parts = import_source.replace(".", "/")
        for try_path in (workspace / f"{parts}.py", workspace / parts / "__init__.py"):
            if try_path.is_file():
                return try_path.resolve()

    return None


# Maximum files to parse. Beyond this we still list files but skip parsing.
_MAX_PARSE_FILES = 500


def _walk_source_files(workspace: Path) -> list[tuple[Path, str]]:
    """Walk workspace and yield (path, language) for parseable source files."""
    results: list[tuple[Path, str]] = []
    for dirpath, dirnames, filenames in os.walk(workspace):
        # Prune skip dirs in-place.
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            language = _LANGUAGE_MAP.get(ext)
            if language:
                results.append((Path(dirpath) / fname, language))
    return results


def _pagerank_single_iter(
    graph: dict[str, set[str]], damping: float = 0.85
) -> dict[str, float]:
    """Single-iteration PageRank over a directed graph.

    Keys are file paths (as strings). Values in graph are sets of
    files that the key file references (outgoing edges).
    """
    nodes = set(graph.keys())
    for targets in graph.values():
        nodes.update(targets)

    n = len(nodes)
    if n == 0:
        return {}

    # Initialize uniform.
    rank: dict[str, float] = {node: 1.0 / n for node in nodes}

    # Build reverse graph (who points to me).
    incoming: dict[str, set[str]] = defaultdict(set)
    out_degree: dict[str, int] = {}
    for src, targets in graph.items():
        out_degree[src] = len(targets)
        for tgt in targets:
            incoming[tgt].add(src)

    # Single iteration.
    new_rank: dict[str, float] = {}
    for node in nodes:
        contrib = sum(
            rank[src] / max(out_degree.get(src, 1), 1)
            for src in incoming.get(node, set())
        )
        new_rank[node] = (1 - damping) / n + damping * contrib

    return new_rank


def _workspace_fingerprint(source_files: list[tuple[Path, str]]) -> str:
    """Cheap digest of the file set and their mtimes.

    Parsing a large repo costs seconds. Build mode calls repo_map repeatedly
    across a session and the tree usually has not moved, so a fingerprint made
    of (path, mtime_ns, size) turns every repeat call into a dict lookup, and
    any real edit invalidates it.
    """
    hasher = hashlib.blake2b(digest_size=16)
    for path, _language in source_files:
        try:
            st = path.stat()
            hasher.update(f"{path}:{st.st_mtime_ns}:{st.st_size}".encode())
        except OSError:
            hasher.update(f"{path}:missing".encode())
    return hasher.hexdigest()


# workspace -> (fingerprint, parsed result without budget applied)
_MAP_CACHE: dict[str, tuple[str, dict[str, Any]]] = {}


def repo_map(workspace: Path, budget: int = 4096) -> dict[str, Any]:
    """Generate a token-budgeted repository map.

    Parameters
    ----------
    workspace : Path
        The workspace root to map.
    budget : int
        Maximum token budget for the output map string.

    Returns
    -------
    dict with keys:
        map : str - the formatted map
        files : int - total source files found
        languages : list[str] - languages detected
    """
    if not tree_sitter_available():
        raise TreeSitterUnavailable(
            "repo_map needs tree-sitter. Install it with: "
            "pip install tree-sitter tree-sitter-language-pack"
        )

    # Resolve up front. Import targets are resolved to absolute paths, so a
    # relative workspace made every relative_to() call raise and silently
    # dropped every edge in the graph.
    workspace = Path(workspace).expanduser().resolve()

    source_files = _walk_source_files(workspace)
    if not source_files:
        return {"map": "(empty workspace - no source files found)", "files": 0, "languages": []}

    # Parse the biggest files first when the cap bites: file order from
    # os.walk is filesystem-arbitrary, so the 500-file limit was truncating by
    # accident rather than by importance.
    source_files.sort(key=lambda pair: -_safe_size(pair[0]))

    cache_key = str(workspace.resolve())
    fingerprint = _workspace_fingerprint(source_files)
    cached = _MAP_CACHE.get(cache_key)
    if cached is not None and cached[0] == fingerprint:
        return _render_map(cached[1], budget)

    aliases = _load_path_aliases(workspace)
    languages_found: set[str] = set()
    # file_path_str -> list of (name, signature)
    file_defs: dict[str, list[tuple[str, str]]] = {}
    # file_path_str -> set of file_path_str (outgoing edges)
    graph: dict[str, set[str]] = defaultdict(set)
    # Track files we couldn't parse (listed without signatures).
    unparsed_files: list[str] = []

    # Cap parsing to _MAX_PARSE_FILES for performance. Remaining files are
    # listed without signatures but still participate in the graph and count.
    parse_limit = min(len(source_files), _MAX_PARSE_FILES)

    for idx, (file_path, language) in enumerate(source_files):
        rel_path = str(file_path.relative_to(workspace))
        languages_found.add(language)

        # Beyond parse limit, just list the file without parsing.
        if idx >= parse_limit:
            unparsed_files.append(rel_path)
            file_defs[rel_path] = []
            graph[rel_path] = set()
            continue

        parser = _get_parser(language)
        if parser is None:
            unparsed_files.append(rel_path)
            file_defs[rel_path] = []
            graph[rel_path] = set()
            continue

        try:
            source_bytes = file_path.read_bytes()
        except OSError:
            unparsed_files.append(rel_path)
            file_defs[rel_path] = []
            graph[rel_path] = set()
            continue

        try:
            tree = parser.parse(source_bytes)
        except Exception:
            unparsed_files.append(rel_path)
            file_defs[rel_path] = []
            graph[rel_path] = set()
            continue

        defs: list[tuple[str, str]] = []
        def_types = _DEF_TYPES.get(language, set())
        ref_types = _REF_TYPES.get(language, set())
        source_lines = source_bytes.decode("utf-8", errors="replace").splitlines()

        # Iterative walk. The original was recursive despite a comment saying
        # otherwise, so a minified or generated file raised an uncaught
        # RecursionError. It also deduplicated on id(node), which is unsound:
        # py-tree-sitter materializes fresh node objects on each .children
        # access, so a freed node's id could be reused and silently skip a live
        # subtree. A tree needs no visited set at all.
        stack: list[Any] = [tree.root_node]
        while stack:
            node = stack.pop()

            if node.type in def_types:
                # An export_statement wrapping a real declaration would
                # otherwise be reported twice.
                is_redundant_export = node.type == "export_statement" and any(
                    c.type in def_types and c.type != "export_statement"
                    for c in node.children
                )
                if not is_redundant_export:
                    name = _extract_name(node)
                    if name:
                        defs.append((name, _extract_signature(node, source_lines)))

            if node.type in ref_types:
                import_src = _extract_import_source(node)
                if import_src:
                    target = _resolve_import_to_file(
                        import_src, file_path, workspace, file_path.suffix, aliases
                    )
                    if target is not None:
                        try:
                            graph[rel_path].add(str(target.relative_to(workspace)))
                        except ValueError:
                            pass

            stack.extend(node.children)

        file_defs[rel_path] = defs
        if rel_path not in graph:
            graph[rel_path] = set()

    # PageRank to rank files.
    ranks = _pagerank_single_iter(graph)

    # Sort by rank (highest first), breaking ties alphabetically.
    all_files = list(file_defs.keys())
    all_files.sort(key=lambda f: (-ranks.get(f, 0.0), f))

    parsed = {
        "ranked_files": all_files,
        "file_defs": file_defs,
        "files": len(source_files),
        "parsed": min(len(source_files), parse_limit),
        "languages": sorted(languages_found),
        "edges": sum(len(v) for v in graph.values()),
    }
    _MAP_CACHE[cache_key] = (fingerprint, parsed)
    return _render_map(parsed, budget)


def _render_map(parsed: dict[str, Any], budget: int) -> dict[str, Any]:
    """Format a parsed workspace into a budgeted map string.

    Separate from parsing so a cached parse can serve any budget.
    """
    max_chars = int(budget * _CHARS_PER_TOKEN)
    file_defs: dict[str, list[tuple[str, str]]] = parsed["file_defs"]
    output_parts: list[str] = []
    chars_used = 0
    omitted = 0

    for rel_path in parsed["ranked_files"]:
        section = f"## {rel_path}\n"
        for _name, sig in file_defs.get(rel_path, []):
            if sig:
                section += f"  {sig}\n"

        section_len = len(section)
        if chars_used + section_len > max_chars:
            # Keep filling with cheaper entries rather than stopping dead: a
            # single large file used to truncate the whole rest of the map.
            bare = f"## {rel_path}\n"
            if chars_used + len(bare) <= max_chars:
                output_parts.append(bare)
                chars_used += len(bare)
            else:
                omitted += 1
            continue
        output_parts.append(section)
        chars_used += section_len

    map_str = "".join(output_parts).rstrip()
    if omitted:
        map_str += f"\n\n({omitted} more file(s) omitted for budget)"
    if not map_str:
        map_str = "(no definitions extracted)"

    return {
        "map": map_str,
        "files": parsed["files"],
        "parsed": parsed["parsed"],
        "languages": parsed["languages"],
        "edges": parsed["edges"],
    }


# ── Tool registration ───────────────────────────────────────────────────────

REPO_MAP_SCHEMA = {
    "name": "repo_map",
    "description": (
        "Produce a ranked, token-budgeted map of the workspace: which files "
        "exist, what they define, and which ones matter most (ranked by how "
        "much the rest of the codebase imports them). Deterministic, no "
        "embeddings. Call this first when opening an unfamiliar codebase, "
        "before reading individual files."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "budget": {
                "type": "integer",
                "description": "Approximate token budget for the map. Default 4096.",
                "default": 4096,
            },
        },
        "required": [],
    },
}


def handle_repo_map(args: dict[str, Any], **kwargs: Any) -> str:
    """Model-facing entry point for repo_map."""
    budget = args.get("budget", 4096)
    if not isinstance(budget, int) or isinstance(budget, bool) or budget < 256:
        return json.dumps({"error": "'budget' must be an integer of at least 256."})
    try:
        result = repo_map(resolve_workspace(), budget=budget)
    except TreeSitterUnavailable as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        logger.exception("repo_map failed")
        return json.dumps({"error": f"repo_map crashed: {type(exc).__name__}: {exc}"})
    return json.dumps(result, indent=2)


registry.register(
    name="repo_map",
    toolset="burooj_build",
    schema=REPO_MAP_SCHEMA,
    handler=handle_repo_map,
    emoji="🗺️",
)
