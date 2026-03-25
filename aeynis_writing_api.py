#!/usr/bin/env python3
"""
Aeynis Writing API - Flask Blueprint

Exposes Aeynis's writing workspace as REST endpoints.

Endpoints:
  GET    /writings/list                  - List all writings
  GET    /writings/read/<identifier>     - Load a specific writing
  POST   /writings/save                  - Save a new writing
  POST   /writings/update                - Append to an existing writing
  DELETE /writings/<identifier>          - Delete a writing
  GET    /writings/search?q=<query>      - Search writings by keyword
"""

import logging
from flask import Blueprint, request, jsonify

from aeynis_writing import AeynisWriting

logger = logging.getLogger(__name__)

writings_bp = Blueprint("writings", __name__)

# Singleton instance (created when blueprint is registered)
_writing_tool: AeynisWriting = None


def init_writing_tool(library) -> AeynisWriting:
    """Initialize the writing tool singleton. Called from the main app."""
    global _writing_tool
    _writing_tool = AeynisWriting(library)
    return _writing_tool


def get_writing_tool() -> AeynisWriting:
    global _writing_tool
    if _writing_tool is None:
        raise RuntimeError("Writing tool not initialized. Call init_writing_tool() first.")
    return _writing_tool


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

@writings_bp.route("/writings/list", methods=["GET"])
def list_writings():
    """List all of Aeynis's writings with metadata."""
    tool = get_writing_tool()
    writings = tool.list_writings()
    return jsonify({
        "writings": writings,
        "count": len(writings),
    })


@writings_bp.route("/writings/read/<path:identifier>", methods=["GET"])
def read_writing(identifier):
    """Load a specific writing by filename or title match."""
    tool = get_writing_tool()
    result = tool.load_writing(identifier)
    if not result.get("success"):
        return jsonify(result), 404
    return jsonify(result)


@writings_bp.route("/writings/save", methods=["POST"])
def save_writing():
    """Save a new writing.

    JSON body:
      title:   str (required)
      content: str (required)
      tags:    list[str] (optional)
    """
    data = request.json or {}
    title = data.get("title")
    content = data.get("content")

    if not title or not content:
        return jsonify({"error": "title and content are required"}), 400

    tool = get_writing_tool()
    result = tool.save_writing(
        title=title,
        content=content,
        tags=data.get("tags"),
    )

    if not result.get("success"):
        return jsonify(result), 500
    return jsonify(result), 201


@writings_bp.route("/writings/update", methods=["POST"])
def update_writing():
    """Append content to an existing writing.

    JSON body:
      identifier:         str (required) - filename or title
      additional_content: str (required) - text to append
    """
    data = request.json or {}
    identifier = data.get("identifier")
    additional = data.get("additional_content")

    if not identifier or not additional:
        return jsonify({"error": "identifier and additional_content are required"}), 400

    tool = get_writing_tool()
    result = tool.update_writing(identifier, additional)
    if not result.get("success"):
        return jsonify(result), 404 if "not found" in result.get("error", "").lower() else 500
    return jsonify(result)


@writings_bp.route("/writings/<path:identifier>", methods=["DELETE"])
def delete_writing(identifier):
    """Delete a writing by filename or title."""
    tool = get_writing_tool()
    result = tool.delete_writing(identifier)
    if not result.get("success"):
        return jsonify(result), 404
    return jsonify(result)


@writings_bp.route("/writings/search", methods=["GET"])
def search_writings():
    """Search writings by keyword."""
    query = request.args.get("q", "")
    if not query:
        return jsonify({"error": "query parameter 'q' is required"}), 400

    tool = get_writing_tool()
    results = tool.search_writings(query)
    return jsonify({
        "query": query,
        "results": results,
        "count": len(results),
    })
