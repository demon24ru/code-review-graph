"""Tool 23-24: SCIP export and import adapters.

Exposes the ``scip.py`` module as MCP-callable functions with the standard
``status/summary/next_actions`` envelope.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..scip import export_scip, import_scip
from ._common import _get_store, graph_error

logger = logging.getLogger(__name__)


def export_scip_func(
    output_path: str | None = None,
    file_pattern: str | None = None,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Export the code knowledge graph to a SCIP-compatible JSON document.

    Serialises all nodes and edges to the SCIP open standard (subset, JSON
    encoding) so that the graph can be consumed by external tools or
    re-imported into another repository's graph.

    Args:
        output_path: Destination file path for the exported JSON.  Defaults to
            ``.code-review-graph/export.scip.json`` inside the repo root.
        file_pattern: Restrict the export to nodes/edges whose file path
            contains this substring.  Omit to export the entire graph.
        repo_root: Repository root path.  Auto-detected if omitted.

    Returns:
        Metadata summary including counts and the output file location.
    """
    store, root = _get_store(repo_root)
    try:
        if output_path is None:
            output_path = str(root / ".code-review-graph" / "export.scip.json")

        doc = export_scip(store, output_path=output_path, file_pattern=file_pattern)
        meta = doc.get("metadata", {})

        return {
            "status": "ok",
            "summary": (
                f"SCIP export complete: {meta.get('symbol_count', 0)} symbols, "
                f"{meta.get('relationship_count', 0)} relationships "
                f"written to '{output_path}'."
            ),
            "output_path": output_path,
            "symbol_count": meta.get("symbol_count", 0),
            "document_count": meta.get("document_count", 0),
            "relationship_count": meta.get("relationship_count", 0),
            "next_actions": ["import_scip_tool", "semantic_search_nodes_tool"],
        }
    except Exception as exc:
        logger.exception("export_scip_func failed")
        return graph_error("SCIP_FAILED", str(exc))
    finally:
        store.close()


def import_scip_func(
    scip_path: str,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Import a SCIP JSON document into the code knowledge graph.

    Reads a SCIP-format file (produced by ``export_scip_tool`` or compatible
    tools) and upserts all symbols and relationships into the graph store.
    Existing nodes with matching qualified names are updated in place.

    Args:
        scip_path: Path to the ``.scip.json`` file to import.  Can be
            absolute or relative to ``repo_root``.
        repo_root: Repository root path.  Auto-detected if omitted.

    Returns:
        Summary with counts of upserted symbols and relationships.
    """
    store, root = _get_store(repo_root)
    try:
        fp = Path(scip_path)
        if not fp.is_absolute():
            fp = root / fp

        result = import_scip(store, scip_path=fp, repo_root=root)
        if result.get("status") == "ok":
            result["next_actions"] = [
                "list_graph_stats_tool",
                "semantic_search_nodes_tool",
                "query_graph_tool",
            ]
            # Community detection, FTS index, flow data, and embeddings are NOT
            # part of the SCIP format — only nodes and edges are imported.
            result["warning"] = (
                "SCIP import restores nodes and edges only. "
                "Community detection, FTS search index, execution flows, and "
                "vector embeddings are NOT included. Run `code-review-graph build` "
                "(or rebuild via MCP) to refresh all derived data."
            )
        return result
    except Exception as exc:
        logger.exception("import_scip_func failed")
        return graph_error("SCIP_FAILED", str(exc))
    finally:
        store.close()
