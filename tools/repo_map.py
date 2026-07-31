"""repo_map tool - tree-sitter parsed, PageRank-ranked code map.

Parses every source file under the workspace with tree-sitter, extracts
definitions and references, builds a file-level directed graph, ranks files
with a single-iteration PageRank pass, and greedily fills a token budget
with the highest-ranked file signatures.

Tool schema:
    repo_map(budget: int = 4096) -> { map: str, files: int, languages: list[str] }
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("hermes.repo_map")

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


def _get_parser(language: str) -> Any:
    """Get a tree-sitter parser for the given language.

    Returns None if tree-sitter or the language pack is not available.
    """
    try:
        import tree_sitter_language_pack as tslp
    except ImportError:
        return None

    try:
        return tslp.get_parser(language)
    except Exception:
        return None


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


def _extract_signature(node: Any, source_bytes: bytes, max_lines: int = 3) -> str:
    """Extract a short signature from a definition node."""
    start = node.start_point[0]
    end = min(node.start_point[0] + max_lines, node.end_point[0] + 1)
    lines = source_bytes.decode("utf-8", errors="replace").splitlines()
    sig_lines = lines[start:end]
    sig = "\n".join(sig_lines)
    if end < node.end_point[0] + 1:
        sig += "\n    ..."
    return sig


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


def _resolve_import_to_file(
    import_source: str, from_file: Path, workspace: Path, ext: str
) -> Optional[Path]:
    """Attempt to resolve an import source string to a file in the workspace."""
    if not import_source:
        return None

    # Relative imports (./foo, ../bar)
    if import_source.startswith("."):
        base = from_file.parent
        # Try with various extensions
        for try_ext in (ext, ".ts", ".tsx", ".js", ".jsx", ".py"):
            candidate = (base / import_source).with_suffix(try_ext)
            if candidate.exists():
                return candidate.resolve()
            # Try index file
            index = base / import_source / f"index{try_ext}"
            if index.exists():
                return index.resolve()
        return None

    # Python dotted imports
    if ext == ".py":
        parts = import_source.replace(".", "/")
        for try_path in (workspace / f"{parts}.py", workspace / parts / "__init__.py"):
            if try_path.exists():
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
    source_files = _walk_source_files(workspace)
    if not source_files:
        return {"map": "(empty workspace - no source files found)", "files": 0, "languages": []}

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

        # Walk the tree (iterative to avoid deep recursion).
        visited = set()

        def _walk(node: Any) -> None:
            node_id = id(node)
            if node_id in visited:
                return
            visited.add(node_id)

            if node.type in def_types:
                # Skip export_statement if its child is also a def type
                # (to avoid duplicates).
                if node.type == "export_statement":
                    has_inner_def = any(
                        c.type in def_types and c.type != "export_statement"
                        for c in node.children
                    )
                    if not has_inner_def:
                        name = _extract_name(node)
                        sig = _extract_signature(node, source_bytes)
                        if name:
                            defs.append((name, sig))
                else:
                    name = _extract_name(node)
                    sig = _extract_signature(node, source_bytes)
                    if name:
                        defs.append((name, sig))

            if node.type in ref_types:
                import_src = _extract_import_source(node)
                if import_src:
                    ext = file_path.suffix
                    target = _resolve_import_to_file(import_src, file_path, workspace, ext)
                    if target is not None:
                        try:
                            target_rel = str(target.relative_to(workspace))
                            graph[rel_path].add(target_rel)
                        except ValueError:
                            pass

            for child in node.children:
                _walk(child)

        _walk(tree.root_node)
        file_defs[rel_path] = defs
        if rel_path not in graph:
            graph[rel_path] = set()

    # PageRank to rank files.
    ranks = _pagerank_single_iter(graph)

    # Sort by rank (highest first), breaking ties alphabetically.
    all_files = list(file_defs.keys())
    all_files.sort(key=lambda f: (-ranks.get(f, 0.0), f))

    # Greedily fill the budget.
    max_chars = int(budget * _CHARS_PER_TOKEN)
    output_parts: list[str] = []
    chars_used = 0

    for rel_path in all_files:
        defs = file_defs[rel_path]
        if defs:
            section = f"## {rel_path}\n"
            for name, sig in defs:
                section += f"  {sig}\n"
        else:
            section = f"## {rel_path}\n"

        section_len = len(section)
        if chars_used + section_len > max_chars:
            # If we haven't added anything yet, add at least the filename.
            if not output_parts:
                output_parts.append(f"## {rel_path}\n  (truncated)\n")
            break
        output_parts.append(section)
        chars_used += section_len

    map_str = "".join(output_parts).rstrip()
    if not map_str:
        map_str = "(no definitions extracted)"

    return {
        "map": map_str,
        "files": len(source_files),
        "languages": sorted(languages_found),
    }
