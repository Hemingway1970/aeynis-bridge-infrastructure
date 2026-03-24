#!/usr/bin/env python3
"""
Aeynis Image Viewer API — Flask Blueprint

Exposes image viewing operations as REST endpoints for the chat UI.

Endpoints:
  GET  /images/folders                  - List available image folders
  POST /images/open                     - Open a folder for viewing
  GET  /images/current                  - Get current image perception
  POST /images/next                     - Advance to next image
  POST /images/prev                     - Go back to previous image
  POST /images/jump                     - Jump to specific image
  POST /images/note                     - Add synthesis note to current image
  POST /images/close                    - Close viewing session
  GET  /images/serve/<path:filepath>    - Serve raw image file for display
  GET  /images/status                   - Current viewer state
"""

import logging
import os
import mimetypes

from flask import Blueprint, request, jsonify, send_from_directory, abort

from image_viewer import ImageViewer, IMAGES_ROOT, SUPPORTED_FORMATS

logger = logging.getLogger("aeynis.image_viewer_api")

images_bp = Blueprint("images", __name__)

# Singleton viewer instance
_viewer: ImageViewer = None


def init_image_viewer(kobold_url: str = "http://localhost:5001",
                      memory_url: str = "http://localhost:8000") -> ImageViewer:
    """Initialize the image viewer singleton."""
    global _viewer
    _viewer = ImageViewer(kobold_url=kobold_url, memory_url=memory_url)

    # Ensure images directory exists
    os.makedirs(IMAGES_ROOT, exist_ok=True)

    logger.info(f"Image viewer initialized. Images root: {IMAGES_ROOT}")
    return _viewer


def get_image_viewer() -> ImageViewer:
    global _viewer
    if _viewer is None:
        _viewer = ImageViewer()
    return _viewer


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

@images_bp.route("/images/folders", methods=["GET"])
def list_folders():
    """List available image folders."""
    viewer = get_image_viewer()
    folders = viewer.list_folders()
    return jsonify({
        "folders": folders,
        "images_root": IMAGES_ROOT,
    })


@images_bp.route("/images/open", methods=["POST"])
def open_folder():
    """Open a folder for image viewing.

    JSON body:
      folder_name: str  - Name of subfolder under ~/AeynisLibrary/images/
                          OR absolute path
    """
    data = request.json or {}
    folder_name = data.get("folder_name", "")

    if not folder_name:
        return jsonify({"error": "folder_name is required"}), 400

    # Resolve path
    if os.path.isabs(folder_name):
        folder_path = folder_name
    else:
        folder_path = os.path.join(IMAGES_ROOT, folder_name)

    # Security: ensure path stays within expected areas
    folder_path = os.path.abspath(folder_path)

    viewer = get_image_viewer()
    result = viewer.open_folder(folder_path)

    if not result.get("success"):
        return jsonify(result), 404

    return jsonify(result)


@images_bp.route("/images/current", methods=["GET"])
def view_current():
    """Get two-pass perception of the current image."""
    viewer = get_image_viewer()

    if not viewer.is_open:
        return jsonify({"error": "No folder open. Use /images/open first."}), 400

    perception = viewer.view_current()
    if not perception:
        return jsonify({"error": "No image available"}), 404

    # Include the chat-formatted version
    perception["chat_block"] = viewer.format_perception_for_chat(perception)

    # Include serving URL for the image
    if viewer.current_filepath:
        rel_path = os.path.relpath(viewer.current_filepath, IMAGES_ROOT)
        perception["serve_url"] = f"/images/serve/{rel_path}"

    return jsonify(perception)


@images_bp.route("/images/next", methods=["POST"])
def next_image():
    """Advance to next image and return its perception."""
    viewer = get_image_viewer()

    if not viewer.is_open:
        return jsonify({"error": "No folder open"}), 400

    if not viewer.next_image():
        return jsonify({
            "error": "Already at last image",
            "position": viewer.position,
            "total": viewer.image_count,
        }), 400

    perception = viewer.view_current()
    if perception:
        perception["chat_block"] = viewer.format_perception_for_chat(perception)
        if viewer.current_filepath:
            rel_path = os.path.relpath(viewer.current_filepath, IMAGES_ROOT)
            perception["serve_url"] = f"/images/serve/{rel_path}"

    return jsonify(perception or {"error": "Perception failed"})


@images_bp.route("/images/prev", methods=["POST"])
def prev_image():
    """Go back to previous image and return its perception."""
    viewer = get_image_viewer()

    if not viewer.is_open:
        return jsonify({"error": "No folder open"}), 400

    if not viewer.prev_image():
        return jsonify({
            "error": "Already at first image",
            "position": viewer.position,
            "total": viewer.image_count,
        }), 400

    perception = viewer.view_current()
    if perception:
        perception["chat_block"] = viewer.format_perception_for_chat(perception)
        if viewer.current_filepath:
            rel_path = os.path.relpath(viewer.current_filepath, IMAGES_ROOT)
            perception["serve_url"] = f"/images/serve/{rel_path}"

    return jsonify(perception or {"error": "Perception failed"})


@images_bp.route("/images/jump", methods=["POST"])
def jump_to_image():
    """Jump to a specific image by index or filename.

    JSON body:
      index:    int (optional) - 0-based image index
      filename: str (optional) - Image filename to jump to
    """
    data = request.json or {}
    viewer = get_image_viewer()

    if not viewer.is_open:
        return jsonify({"error": "No folder open"}), 400

    index = data.get("index")
    filename = data.get("filename")

    if index is not None:
        if not viewer.jump_to(int(index)):
            return jsonify({"error": f"Invalid index: {index}"}), 400
    elif filename:
        if not viewer.jump_to_filename(filename):
            return jsonify({"error": f"Image not found: {filename}"}), 404
    else:
        return jsonify({"error": "Provide index or filename"}), 400

    perception = viewer.view_current()
    if perception:
        perception["chat_block"] = viewer.format_perception_for_chat(perception)
        if viewer.current_filepath:
            rel_path = os.path.relpath(viewer.current_filepath, IMAGES_ROOT)
            perception["serve_url"] = f"/images/serve/{rel_path}"

    return jsonify(perception or {"error": "Perception failed"})


@images_bp.route("/images/note", methods=["POST"])
def add_note():
    """Add a synthesis note to the current image.

    JSON body:
      note: str - Aeynis's observation to attach
    """
    data = request.json or {}
    note = data.get("note", "")

    if not note:
        return jsonify({"error": "note is required"}), 400

    viewer = get_image_viewer()
    if not viewer.is_open:
        return jsonify({"error": "No folder open"}), 400

    success = viewer.add_synthesis_note(note)
    return jsonify({"success": success, "filename": viewer.current_filename})


@images_bp.route("/images/close", methods=["POST"])
def close_session():
    """Close the current viewing session and wipe caches."""
    viewer = get_image_viewer()
    viewer.close_session()
    return jsonify({"success": True})


@images_bp.route("/images/status", methods=["GET"])
def viewer_status():
    """Get current viewer state."""
    viewer = get_image_viewer()
    return jsonify({
        "is_open": viewer.is_open,
        "folder": viewer.folder_name,
        "position": viewer.position,
        "total": viewer.image_count,
        "current_image": viewer.current_filename,
    })


@images_bp.route("/images/serve/<path:filepath>", methods=["GET"])
def serve_image(filepath):
    """Serve a raw image file for display in the browser.

    The filepath is relative to IMAGES_ROOT.
    """
    full = os.path.abspath(os.path.join(IMAGES_ROOT, filepath))

    # Security: ensure path stays within images root
    if not full.startswith(os.path.abspath(IMAGES_ROOT)):
        abort(403)

    if not os.path.isfile(full):
        abort(404)

    # Verify it's a supported image format
    ext = os.path.splitext(full)[1].lower()
    if ext not in SUPPORTED_FORMATS:
        abort(403)

    directory = os.path.dirname(full)
    filename = os.path.basename(full)
    mime, _ = mimetypes.guess_type(filename)

    return send_from_directory(directory, filename, mimetype=mime)
