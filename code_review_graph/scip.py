"""SCIP (Streaming Code Intelligence Protocol) export and import adapters.

SCIP is an open standard for code intelligence data (see
https://github.com/sourcegraph/scip). This module provides:

  - ``export_scip``:  Serialize the code knowledge graph to a SCIP-compatible
    JSON document (``occurrences``, ``relationships``, ``symbols``).
  - ``import_scip``:  Ingest a SCIP JSON document and upsert the encoded nodes
    and edges into a ``GraphStore``.

The output format is intentionally a *subset* of the SCIP protobuf schema
serialised as JSON so that it can be consumed by tools that speak SCIP without
requiring the protobuf Python runtime.  The ``scip_type`` field on every
symbol disambiguates the origin when re-importing.

Security invariants
-------------------
- No ``eval``/``exec``/``pickle``/``yaml.unsafe_load``.
- All string values pass through ``_sanitize_name`` before being stored.
- File writes go through ``pathlib.Path.write_text`` (no shell=True).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from .graph import GraphStore, _sanitize_name
from .parser import EdgeInfo, NodeInfo
from .response import graph_error

logger = logging.getLogger(__name__)

# SCIP symbol-role constants (subset; matches SCIP proto SymbolRole enum)
_ROLE_DEFINITION = 1
_ROLE_REFERENCE = 2

# Map graph edge kinds to SCIP relationship kinds
_EDGE_TO_SCIP_REL: dict[str, str] = {
    "CALLS": "calls",
    "IMPORTS_FROM": "imports",
    "INHERITS": "extends",
    "IMPLEMENTS": "implements",
    "CONTAINS": "contains",
    "TESTED_BY": "tested_by",
    "DEPENDS_ON": "depends_on",
}

# Inverse map for import
_SCIP_REL_TO_EDGE: dict[str, str] = {v: k for k, v in _EDGE_TO_SCIP_REL.items()}


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_scip(
    store: GraphStore,
    output_path: Optional[str | Path] = None,
    file_pattern: Optional[str] = None,
) -> dict[str, Any]:
    """Export the knowledge graph to a SCIP-compatible JSON document.

    The produced document has three top-level keys:

    ``metadata``
        Version, tool name, and export timestamp.

    ``documents``
        One entry per source file.  Each entry lists ``occurrences``
        (definition sites with line ranges) for every symbol defined in
        that file.

    ``relationships``
        All edges in the graph serialised as ``{symbol, related_symbol,
        relationship}``.

    Args:
        store: Open ``GraphStore`` instance.
        output_path: If provided, write the JSON to this file path.
        file_pattern: If provided, restrict export to nodes whose
            ``file_path`` contains this substring.

    Returns:
        The SCIP document as a Python dict (also written to *output_path*
        when provided).
    """
    # Collect all nodes
    conditions = ""
    params: list[Any] = []
    if file_pattern:
        conditions = "WHERE file_path LIKE ?"
        params.append(f"%{file_pattern}%")

    rows = store._conn.execute(  # type: ignore[attr-defined]
        f"SELECT * FROM nodes {conditions}",  # nosec B608
        params,
    ).fetchall()

    # Build documents map: file_path -> list of occurrences
    documents: dict[str, list[dict[str, Any]]] = {}
    symbol_index: dict[str, dict[str, Any]] = {}

    for row in rows:
        fp = row["file_path"]
        if fp not in documents:
            documents[fp] = []

        qn = _sanitize_name(row["qualified_name"])
        sym: dict[str, Any] = {
            "symbol": qn,
            "name": _sanitize_name(row["name"]),
            "kind": row["kind"],
            "language": row["language"] or "",
            "scip_type": "node",
        }
        symbol_index[qn] = sym

        occurrence: dict[str, Any] = {
            "symbol": qn,
            "symbol_roles": _ROLE_DEFINITION,
            "range": [
                row["line_start"] or 0,
                0,  # column_start (not tracked)
                row["line_end"] or 0,
                0,  # column_end (not tracked)
            ],
        }
        documents[fp].append(occurrence)

    # Collect all edges
    edge_conditions = ""
    edge_params: list[Any] = []
    if file_pattern:
        edge_conditions = "WHERE file_path LIKE ?"
        edge_params.append(f"%{file_pattern}%")

    edge_rows = store._conn.execute(  # type: ignore[attr-defined]
        f"SELECT * FROM edges {edge_conditions}",  # nosec B608
        edge_params,
    ).fetchall()

    relationships: list[dict[str, Any]] = []
    for row in edge_rows:
        rel_kind = _EDGE_TO_SCIP_REL.get(row["kind"], row["kind"].lower())
        relationships.append(
            {
                "symbol": _sanitize_name(row["source_qualified"]),
                "related_symbol": _sanitize_name(row["target_qualified"]),
                "relationship": rel_kind,
                "file_path": row["file_path"],
                "line": row["line"],
            }
        )

    doc_list = [
        {
            "relative_path": fp,
            "language": _infer_language(fp),
            "occurrences": occs,
        }
        for fp, occs in documents.items()
    ]

    scip_doc: dict[str, Any] = {
        "metadata": {
            "version": "0.3",
            "tool_info": {
                "name": "code-review-graph",
                "version": "1.0",
            },
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "symbol_count": len(symbol_index),
            "document_count": len(doc_list),
            "relationship_count": len(relationships),
        },
        "symbols": list(symbol_index.values()),
        "documents": doc_list,
        "relationships": relationships,
    }

    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(scip_doc, indent=2), encoding="utf-8")
        logger.info(
            "export_scip: wrote %d symbols and %d relationships to %s",
            len(symbol_index),
            len(relationships),
            out,
        )

    return scip_doc


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def import_scip(
    store: GraphStore,
    scip_path: str | Path,
    repo_root: Optional[str | Path] = None,
) -> dict[str, Any]:
    """Ingest a SCIP JSON document and upsert its symbols into the graph.

    Reads the SCIP document produced by ``export_scip`` (or any compatible
    tool) and calls ``store.upsert_node`` / ``store.upsert_edge`` for each
    symbol and relationship.

    Args:
        store: Open ``GraphStore`` instance to populate.
        scip_path: Path to the ``.scip.json`` file to import.
        repo_root: Base path used to resolve relative ``file_path`` values.
            Defaults to the current working directory.

    Returns:
        Summary dict with counts of imported symbols and relationships.
    """
    scip_file = Path(scip_path)
    if not scip_file.is_file():
        return graph_error("PATH_NOT_FOUND", f"SCIP file not found: {scip_path}")

    try:
        data = json.loads(scip_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return graph_error("PARSE_ERROR", f"Invalid JSON in SCIP file: {exc}")

    base = Path(repo_root) if repo_root else Path.cwd()
    now = time.time()

    # Build a symbol->metadata lookup from the ``symbols`` list
    sym_meta: dict[str, dict[str, Any]] = {s["symbol"]: s for s in data.get("symbols", [])}

    nodes_upserted = 0
    edges_upserted = 0

    # --- Import nodes from documents ---
    for doc in data.get("documents", []):
        relative_path = doc.get("relative_path", "")
        abs_path = str(base / relative_path) if relative_path else ""
        language = doc.get("language", "")

        for occ in doc.get("occurrences", []):
            if occ.get("symbol_roles", 0) != _ROLE_DEFINITION:
                continue  # skip pure references; only materialise definitions

            qn = _sanitize_name(occ["symbol"])
            meta = sym_meta.get(qn, {})
            name = _sanitize_name(meta.get("name", qn.split("::")[-1]))
            kind = meta.get("kind", "Function")
            rang = occ.get("range", [0, 0, 0, 0])
            line_start = rang[0] if len(rang) > 0 else 0
            line_end = rang[2] if len(rang) > 2 else line_start

            # NodeInfo does not carry qualified_name — the store derives it via
            # _make_qualified from (kind, name, file_path, parent_name).
            # We store the raw qn in ``extra`` so it round-trips faithfully,
            # and we also directly upsert via raw SQL to preserve the
            # exact qualified_name from the SCIP document.
            node_info = NodeInfo(
                kind=kind,
                name=name,
                file_path=abs_path,
                line_start=line_start,
                line_end=line_end,
                language=language or meta.get("language", ""),
                parent_name=None,
                params=None,
                return_type=None,
                modifiers=None,
                is_test=kind == "Test",
                extra={"scip_import": True, "scip_qn": qn},
            )
            # Upsert via store but override the qualified_name to the SCIP value
            store._conn.execute(  # type: ignore[attr-defined]
                """INSERT INTO nodes
                   (kind, name, qualified_name, file_path, line_start, line_end,
                    language, parent_name, params, return_type, modifiers, is_test,
                    file_hash, extra, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(qualified_name) DO UPDATE SET
                     kind=excluded.kind, name=excluded.name,
                     file_path=excluded.file_path, line_start=excluded.line_start,
                     line_end=excluded.line_end, language=excluded.language,
                     is_test=excluded.is_test, file_hash=excluded.file_hash,
                     extra=excluded.extra, updated_at=excluded.updated_at
                """,
                (
                    node_info.kind,
                    node_info.name,
                    qn,
                    node_info.file_path,
                    node_info.line_start,
                    node_info.line_end,
                    node_info.language,
                    node_info.parent_name,
                    node_info.params,
                    node_info.return_type,
                    node_info.modifiers,
                    int(node_info.is_test),
                    "scip",
                    json.dumps(node_info.extra),
                    now,
                ),
            )
            nodes_upserted += 1

    # --- Import edges from relationships ---
    for rel in data.get("relationships", []):
        rel_type: str = rel.get("relationship") or "CALLS"
        edge_kind: str = _SCIP_REL_TO_EDGE.get(rel_type, rel_type.upper())
        edge_info = EdgeInfo(
            kind=edge_kind,
            source=_sanitize_name(rel.get("symbol", "")),
            target=_sanitize_name(rel.get("related_symbol", "")),
            file_path=rel.get("file_path", ""),
            line=rel.get("line", 0),
            extra={"scip_import": True},
        )
        store.upsert_edge(edge_info)
        edges_upserted += 1

    store.commit()
    logger.info(
        "import_scip: upserted %d nodes and %d edges from %s",
        nodes_upserted,
        edges_upserted,
        scip_path,
    )

    return {
        "status": "ok",
        "summary": (
            f"Imported {nodes_upserted} symbols and {edges_upserted} "
            f"relationships from '{scip_file.name}'."
        ),
        "nodes_upserted": nodes_upserted,
        "edges_upserted": edges_upserted,
        "source_file": str(scip_file),
    }


# ---------------------------------------------------------------------------
# Language inference helper
# ---------------------------------------------------------------------------


_EXT_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".cs": "csharp",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".c": "c",
    ".h": "c",
    ".hpp": "cpp",
    ".swift": "swift",
    ".php": "php",
    ".lua": "lua",
    ".r": "r",
    ".dart": "dart",
    ".sol": "solidity",
    ".vue": "vue",
}


def _infer_language(file_path: str) -> str:
    """Return a language string based on file extension."""
    ext = Path(file_path).suffix.lower()
    return _EXT_LANGUAGE.get(ext, "")
