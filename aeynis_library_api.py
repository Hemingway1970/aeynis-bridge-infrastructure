#!/usr/bin/env python3
"""
Aeynis Library API - Flask Blueprint

Exposes Aeynis Library operations as REST endpoints and serves files
for download/viewing through the web interface.

Endpoints:
  GET  /library/files[?subdir=]       - List files
  GET  /library/read/<filename>       - Read/extract file content as JSON
  POST /library/write                 - Write a new document
  POST /library/review                - Create a review of an existing file
  GET  /library/info/<filename>       - File metadata
  DELETE /library/files/<filename>    - Delete a file
  POST /library/import                - Import an external file
  GET  /library/usage                 - Disk usage / quota
  GET  /library/serve/<path:filepath> - Serve raw file for download/view
"""

import logging
import os
import mimetypes

from flask import Blueprint, request, jsonify, send_from_directory, abort

from aeynis_library import AeynisLibrary

logger = logging.getLogger(__name__)

library_bp = Blueprint("library", __name__)

# Singleton library instance (created when blueprint is registered)
_library: AeynisLibrary = None


def init_library(root: str = None, size_limit_gb: float = 50):
    """Initialize the library singleton. Called from the main app."""
    global _library
    _library = AeynisLibrary(root=root) if root else AeynisLibrary()
    return _library


def get_library() -> AeynisLibrary:
    global _library
    if _library is None:
        _library = AeynisLibrary()
    return _library


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

@library_bp.route("/library/files", methods=["GET"])
def list_files():
    """List files in the library."""
    subdir = request.args.get("subdir", "")
    lib = get_library()
    files = lib.list_files(subdir)
    usage = lib.usage()
    return jsonify({
        "files": files,
        "count": len(files),
        "subdir": subdir,
        "usage": usage,
    })


@library_bp.route("/library/read/<path:filename>", methods=["GET"])
def read_file(filename):
    """Read and extract text content from a file."""
    subdir = request.args.get("subdir", "")
    lib = get_library()
    result = lib.read_file(filename, subdir)
    if not result.get("success"):
        return jsonify(result), 404
    return jsonify(result)


@library_bp.route("/library/write", methods=["POST"])
def write_file():
    """Write a new document to the library.

    JSON body:
      filename:        str (required)
      content:         str (required)
      subdir:          str (default "originals")
      format:          str "md"|"txt"|"html" (default "md")
      convert_to_odt:  bool (default false)
    """
    data = request.json or {}
    filename = data.get("filename")
    content = data.get("content")

    if not filename or content is None:
        return jsonify({"error": "filename and content are required"}), 400

    lib = get_library()
    result = lib.write_file(
        filename=filename,
        content=content,
        subdir=data.get("subdir", "originals"),
        fmt=data.get("format", "md"),
        convert_to_odt=data.get("convert_to_odt", False),
    )

    if not result.get("success"):
        return jsonify(result), 507 if "quota" in result.get("error", "").lower() else 500
    return jsonify(result), 201


@library_bp.route("/library/review", methods=["POST"])
def review_file():
    """Create a review/annotation for an existing file.

    JSON body:
      source_filename: str (required)
      review_content:  str (required)
      source_subdir:   str (default "")
      reviewer:        str (default "Aeynis")
    """
    data = request.json or {}
    source = data.get("source_filename")
    review = data.get("review_content")

    if not source or not review:
        return jsonify({"error": "source_filename and review_content are required"}), 400

    lib = get_library()
    result = lib.review_file(
        source_filename=source,
        review_content=review,
        source_subdir=data.get("source_subdir", ""),
        reviewer=data.get("reviewer", "Aeynis"),
    )

    if not result.get("success"):
        return jsonify(result), 404 if "not found" in result.get("error", "").lower() else 500
    return jsonify(result), 201


@library_bp.route("/library/info/<path:filename>", methods=["GET"])
def file_info(filename):
    """Get metadata about a file."""
    subdir = request.args.get("subdir", "")
    lib = get_library()
    result = lib.get_file_info(filename, subdir)
    if not result.get("success"):
        return jsonify(result), 404
    return jsonify(result)


@library_bp.route("/library/files/<path:filename>", methods=["DELETE"])
def delete_file(filename):
    """Delete a file from the library."""
    subdir = request.args.get("subdir", "")
    lib = get_library()
    result = lib.delete_file(filename, subdir)
    if not result.get("success"):
        return jsonify(result), 404
    return jsonify(result)


@library_bp.route("/library/import", methods=["POST"])
def import_file():
    """Import an external file into the library.

    JSON body:
      source_path: str - absolute path to the file to import
    """
    data = request.json or {}
    source_path = data.get("source_path")
    if not source_path:
        return jsonify({"error": "source_path is required"}), 400

    lib = get_library()
    result = lib.import_file(source_path)
    if not result.get("success"):
        return jsonify(result), 400
    return jsonify(result), 201


@library_bp.route("/library/usage", methods=["GET"])
def usage():
    """Get disk usage and quota information."""
    lib = get_library()
    return jsonify(lib.usage())


@library_bp.route("/library/serve/<path:filepath>", methods=["GET"])
def serve_file(filepath):
    """Serve a file from the library for download or in-browser viewing.

    This is the endpoint that powers clickable links in the chat UI.
    Files are served with appropriate Content-Type headers so browsers
    can display PDFs, text, HTML inline or trigger downloads for others.
    """
    lib = get_library()
    full = os.path.abspath(os.path.join(lib.root, filepath))

    # Security: ensure path stays within library root
    if not full.startswith(os.path.abspath(lib.root)):
        abort(403)

    if not os.path.isfile(full):
        abort(404)

    directory = os.path.dirname(full)
    filename = os.path.basename(full)

    # Determine if we should display inline or force download
    mime, _ = mimetypes.guess_type(filename)
    inline_types = {
        "text/plain", "text/html", "text/markdown", "text/csv",
        "application/pdf", "application/json", "text/xml",
    }

    as_attachment = mime not in inline_types if mime else True

    return send_from_directory(
        directory, filename,
        as_attachment=as_attachment,
        mimetype=mime,
    )
