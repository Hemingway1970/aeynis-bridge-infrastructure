#!/usr/bin/env python3
"""
Aeynis Image Viewer — Two-Pass Visual Perception Engine

Architecture:
  Pass 1 (Raw Observation):  Geometry, light, color, texture, pattern, composition.
                             No labels, no names — pure visual perception.
  Pass 2 (Identity & Context): Map to known entities, connect to existing knowledge.

Features:
  - Metadata sidecars (.meta.json) for each image
  - EXIF extraction (ISO, shutter speed, GPS, timestamp)
  - Visual look-ahead (preview of next image in sequence)
  - Pattern resonance detection (cross-modal connections to documents)
  - Cache management (clear VRAM/RAM between folders/sessions)
  - Natural language navigation

Storage: ~/AeynisLibrary/images/ with subfolders
VLM:     KoboldCpp multimodal endpoint (Llava/Moondream2)
"""

import base64
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger("aeynis.image_viewer")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SUPPORTED_FORMATS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
IMAGES_ROOT = os.path.expanduser("~/AeynisLibrary/images")
LOOKAHEAD_RESOLUTION = (320, 240)  # Low-res preview for look-ahead


# ---------------------------------------------------------------------------
# EXIF Extraction (PIL/Pillow)
# ---------------------------------------------------------------------------

def _extract_exif(filepath: str) -> Dict:
    """Extract EXIF data from an image file using Pillow."""
    exif = {}
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS, GPSTAGS

        img = Image.open(filepath)
        exif_data = img.getexif()
        if not exif_data:
            return exif

        for tag_id, value in exif_data.items():
            tag_name = TAGS.get(tag_id, str(tag_id))

            # Convert bytes to string for JSON serialization
            if isinstance(value, bytes):
                try:
                    value = value.decode("utf-8", errors="replace")
                except Exception:
                    value = str(value)

            if tag_name == "GPSInfo":
                gps = {}
                for gps_tag_id, gps_value in value.items():
                    gps_tag = GPSTAGS.get(gps_tag_id, str(gps_tag_id))
                    gps[gps_tag] = str(gps_value)
                exif[tag_name] = gps
            else:
                exif[tag_name] = str(value)

        # Extract commonly useful fields into top-level keys
        useful = {}
        if "ISOSpeedRatings" in exif:
            useful["iso"] = exif["ISOSpeedRatings"]
        if "ExposureTime" in exif:
            useful["shutter_speed"] = exif["ExposureTime"]
        if "FNumber" in exif:
            useful["aperture"] = exif["FNumber"]
        if "DateTimeOriginal" in exif:
            useful["original_timestamp"] = exif["DateTimeOriginal"]
        elif "DateTime" in exif:
            useful["original_timestamp"] = exif["DateTime"]
        if "GPSInfo" in exif:
            useful["gps"] = exif["GPSInfo"]
        if "Make" in exif:
            useful["camera_make"] = exif["Make"]
        if "Model" in exif:
            useful["camera_model"] = exif["Model"]

        return {**useful, "raw_exif": exif}

    except ImportError:
        logger.warning("Pillow not installed — cannot extract EXIF data")
        return {}
    except Exception as e:
        logger.warning(f"EXIF extraction failed for {filepath}: {e}")
        return {}


# ---------------------------------------------------------------------------
# Image encoding for VLM
# ---------------------------------------------------------------------------

def _encode_image_base64(filepath: str) -> str:
    """Read an image file and return its base64-encoded contents."""
    with open(filepath, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _make_low_res_preview(filepath: str) -> Optional[str]:
    """Create a low-resolution base64 preview of an image for look-ahead."""
    try:
        from PIL import Image
        import io

        img = Image.open(filepath)
        img.thumbnail(LOOKAHEAD_RESOLUTION)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=60)
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception as e:
        logger.warning(f"Low-res preview failed for {filepath}: {e}")
        return None


# ---------------------------------------------------------------------------
# VLM Two-Pass Perception
# ---------------------------------------------------------------------------

class VLMPerception:
    """Handles two-pass visual perception via KoboldCpp multimodal API."""

    def __init__(self, kobold_url: str = "http://localhost:5001"):
        self.kobold_url = kobold_url

    def _vlm_query(self, image_b64: str, prompt: str, max_length: int = 500) -> str:
        """Send an image + text prompt to KoboldCpp's multimodal endpoint."""
        try:
            payload = {
                "prompt": prompt,
                "images": [image_b64],
                "max_length": max_length,
                "temperature": 0.3,
                "top_p": 0.8,
                "rep_pen": 1.1,
            }
            response = requests.post(
                f"{self.kobold_url}/api/v1/generate",
                json=payload,
                timeout=30,
            )
            if response.status_code == 200:
                return response.json()["results"][0]["text"].strip()
            else:
                logger.error(f"VLM query failed: HTTP {response.status_code}")
                return ""
        except Exception as e:
            logger.error(f"VLM query error: {e}")
            return ""

    def raw_observation(self, image_b64: str) -> str:
        """Pass 1: Pure visual observation — no labels, no names."""
        prompt = (
            "### System:\n"
            "You are a visual observer. Describe ONLY what you see in pure visual terms.\n"
            "Focus on: geometry, light quality and direction, color palette, texture, "
            "patterns, composition, spatial relationships, mood/atmosphere.\n"
            "Do NOT identify or name any people, places, objects, or brands.\n"
            "Do NOT use proper nouns. Describe as if seeing for the first time.\n"
            "Be concise but evocative — capture what makes this image distinct.\n"
            "### Observer:\n"
        )
        return self._vlm_query(image_b64, prompt, max_length=400)

    def identify_context(self, image_b64: str, raw_perception: str = "") -> str:
        """Pass 2: Identity & context — map to known entities."""
        context_hint = ""
        if raw_perception:
            context_hint = f"\nRaw visual impression: {raw_perception}\n"

        prompt = (
            "### System:\n"
            "You are an image analyst. Given this image, identify:\n"
            "- Who or what is shown (people, objects, locations)\n"
            "- Approximate era, age, or time period if discernible\n"
            "- Any text, signs, labels, or writing visible\n"
            "- Context clues (indoor/outdoor, season, occasion)\n"
            "- Connection to family, home, or personal context if apparent\n"
            f"{context_hint}"
            "Be specific and factual. State what you can determine vs. what you're inferring.\n"
            "### Analyst:\n"
        )
        return self._vlm_query(image_b64, prompt, max_length=400)

    def brief_preview(self, image_b64: str) -> str:
        """Generate a brief (~140 char) description for look-ahead preview."""
        prompt = (
            "### System:\n"
            "Describe this image in one sentence, under 140 characters. "
            "Focus on the main subject and mood.\n"
            "### Observer:\n"
        )
        result = self._vlm_query(image_b64, prompt, max_length=60)
        return result[:140] if result else ""

    def two_pass_perceive(self, filepath: str) -> Dict:
        """Full two-pass perception of an image file.

        Returns dict with raw_perception, identified_elements, and metadata.
        """
        image_b64 = _encode_image_base64(filepath)

        # Pass 1: Raw observation
        raw = self.raw_observation(image_b64)
        logger.info(f"Pass 1 (raw): {raw[:80]}...")

        # Pass 2: Identity & context (informed by Pass 1)
        identified = self.identify_context(image_b64, raw_perception=raw)
        logger.info(f"Pass 2 (identity): {identified[:80]}...")

        return {
            "raw_perception": raw,
            "identified_elements": identified,
        }


# ---------------------------------------------------------------------------
# Pattern Resonance Detection
# ---------------------------------------------------------------------------

def detect_pattern_resonance(raw_perception: str, memory_url: str = "http://localhost:8000") -> List[str]:
    """Check if raw perception contains recurring patterns from Aeynis's reading.

    Searches her memory for concepts that resonate with visual patterns observed.
    Uses simple keyword extraction from the raw perception to query memory.
    """
    resonances = []
    try:
        # Extract pattern-related keywords from raw perception
        pattern_words = re.findall(
            r'\b(branch|tree|flow|spiral|wave|symmetr|fractal|dendrit|radiat|'
            r'converge|diverge|parallel|curve|arch|circle|grid|ripple|'
            r'layer|cascade|nest|weav|braid|root|vein|web|knot|loop)\w*\b',
            raw_perception.lower()
        )
        if not pattern_words:
            return []

        query = f"patterns: {' '.join(set(pattern_words))}"
        response = requests.post(
            f"{memory_url}/api/search",
            json={"query": query, "n_results": 5},
            timeout=5,
        )
        if response.status_code == 200:
            results = response.json().get("results", [])
            for r in results:
                mem = r.get("memory", {})
                score = r.get("similarity_score", 0)
                if score > 0.3:  # Only meaningful resonances
                    content = mem.get("content", "")[:200]
                    resonances.append(content)

    except Exception as e:
        logger.warning(f"Pattern resonance detection failed: {e}")

    return resonances


# ---------------------------------------------------------------------------
# Metadata Sidecar Management
# ---------------------------------------------------------------------------

def _sidecar_path(image_path: str) -> str:
    """Get the .meta.json sidecar path for an image."""
    return os.path.splitext(image_path)[0] + ".meta.json"


def load_sidecar(image_path: str) -> Optional[Dict]:
    """Load existing metadata sidecar if it exists."""
    path = _sidecar_path(image_path)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load sidecar {path}: {e}")
    return None


def save_sidecar(image_path: str, metadata: Dict) -> bool:
    """Save metadata sidecar alongside the image."""
    path = _sidecar_path(image_path)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, default=str)
        logger.info(f"Saved sidecar: {os.path.basename(path)}")
        return True
    except Exception as e:
        logger.error(f"Failed to save sidecar {path}: {e}")
        return False


# ---------------------------------------------------------------------------
# Image Viewer Engine
# ---------------------------------------------------------------------------

class ImageViewer:
    """Main image viewing engine with two-pass perception, caching, and navigation.

    Lifecycle:
        viewer.open_folder(folder_path)    # Load folder, list images
        result = viewer.view_current()     # Two-pass perception of current image
        viewer.next_image()                # Advance to next
        viewer.prev_image()                # Go back
        viewer.view_image(filename)        # Jump to specific image
        viewer.close_session()             # Wipe caches
    """

    def __init__(self, kobold_url: str = "http://localhost:5001",
                 memory_url: str = "http://localhost:8000"):
        self.vlm = VLMPerception(kobold_url)
        self.memory_url = memory_url

        # Current session state
        self._folder: Optional[str] = None
        self._images: List[str] = []          # Sorted filenames in current folder
        self._position: int = 0               # Current index
        self._current_metadata: Optional[Dict] = None

        # Cache for look-ahead preview (pre-processed next image)
        self._lookahead_preview: Optional[str] = None
        self._lookahead_index: int = -1

    # ── Properties ──────────────────────────────────────────────────

    @property
    def is_open(self) -> bool:
        return self._folder is not None and len(self._images) > 0

    @property
    def current_filename(self) -> Optional[str]:
        if self.is_open and 0 <= self._position < len(self._images):
            return self._images[self._position]
        return None

    @property
    def current_filepath(self) -> Optional[str]:
        fn = self.current_filename
        if fn and self._folder:
            return os.path.join(self._folder, fn)
        return None

    @property
    def image_count(self) -> int:
        return len(self._images)

    @property
    def position(self) -> int:
        return self._position

    @property
    def folder_name(self) -> Optional[str]:
        if self._folder:
            return os.path.basename(self._folder)
        return None

    # ── Folder / Session Management ─────────────────────────────────

    def list_folders(self) -> List[Dict]:
        """List available image folders under ~/AeynisLibrary/images/."""
        folders = []
        if not os.path.isdir(IMAGES_ROOT):
            os.makedirs(IMAGES_ROOT, exist_ok=True)
            return folders

        for entry in sorted(os.listdir(IMAGES_ROOT)):
            full = os.path.join(IMAGES_ROOT, entry)
            if os.path.isdir(full):
                # Count images in folder
                count = sum(
                    1 for f in os.listdir(full)
                    if os.path.splitext(f)[1].lower() in SUPPORTED_FORMATS
                )
                folders.append({
                    "name": entry,
                    "path": full,
                    "image_count": count,
                })

        # Also check for loose images in the root
        root_count = sum(
            1 for f in os.listdir(IMAGES_ROOT)
            if os.path.isfile(os.path.join(IMAGES_ROOT, f))
            and os.path.splitext(f)[1].lower() in SUPPORTED_FORMATS
        )
        if root_count > 0:
            folders.insert(0, {
                "name": "(root)",
                "path": IMAGES_ROOT,
                "image_count": root_count,
            })

        return folders

    def open_folder(self, folder_path: str) -> Dict:
        """Open an image folder, clearing any previous session.

        Clears vision cache from previous folder to prevent ghosting.
        """
        # Clear previous session
        self.close_session()

        if not os.path.isdir(folder_path):
            return {"success": False, "error": f"Folder not found: {folder_path}"}

        # Collect and sort image files
        images = []
        for f in sorted(os.listdir(folder_path)):
            ext = os.path.splitext(f)[1].lower()
            if ext in SUPPORTED_FORMATS and os.path.isfile(os.path.join(folder_path, f)):
                images.append(f)

        if not images:
            return {"success": False, "error": "No supported images found in folder"}

        self._folder = folder_path
        self._images = images
        self._position = 0

        logger.info(f"Opened folder '{os.path.basename(folder_path)}' with {len(images)} images")

        # Kick off look-ahead for first image's neighbor
        self._prepare_lookahead(1)

        return {
            "success": True,
            "folder": os.path.basename(folder_path),
            "image_count": len(images),
            "images": images,
        }

    def close_session(self):
        """Wipe all caches — each session starts fresh."""
        prev = self._folder
        self._folder = None
        self._images = []
        self._position = 0
        self._current_metadata = None
        self._lookahead_preview = None
        self._lookahead_index = -1

        if prev:
            logger.info(f"Closed image session (was '{os.path.basename(prev)}')")

    # ── Navigation ──────────────────────────────────────────────────

    def next_image(self) -> bool:
        """Advance to next image. Returns False if already at end."""
        if not self.is_open or self._position >= len(self._images) - 1:
            return False
        self._position += 1
        self._current_metadata = None
        self._prepare_lookahead(self._position + 1)
        return True

    def prev_image(self) -> bool:
        """Go back to previous image. Returns False if already at start."""
        if not self.is_open or self._position <= 0:
            return False
        self._position -= 1
        self._current_metadata = None
        self._prepare_lookahead(self._position + 1)
        return True

    def jump_to(self, index: int) -> bool:
        """Jump to a specific image by index."""
        if not self.is_open or index < 0 or index >= len(self._images):
            return False
        self._position = index
        self._current_metadata = None
        self._prepare_lookahead(self._position + 1)
        return True

    def jump_to_filename(self, filename: str) -> bool:
        """Jump to a specific image by filename."""
        if not self.is_open:
            return False
        filename_lower = filename.lower()
        for i, f in enumerate(self._images):
            if f.lower() == filename_lower or f.lower().startswith(filename_lower.rsplit(".", 1)[0]):
                return self.jump_to(i)
        return False

    # ── Perception ──────────────────────────────────────────────────

    def view_current(self) -> Optional[Dict]:
        """Perform two-pass perception on the current image.

        Returns full perception result with metadata, or None if no image loaded.
        Checks for existing sidecar first — only runs VLM if no prior perception exists.
        """
        filepath = self.current_filepath
        if not filepath:
            return None

        filename = self.current_filename

        # Check for existing sidecar (skip VLM if already perceived)
        existing = load_sidecar(filepath)
        if existing and existing.get("raw_perception"):
            logger.info(f"Using cached perception for '{filename}'")
            self._current_metadata = existing

            # Still provide look-ahead context
            lookahead = self._get_lookahead_preview()
            existing["next_preview"] = lookahead
            existing["position"] = self._position
            existing["total"] = len(self._images)
            existing["filename"] = filename
            return existing

        # Run two-pass VLM perception
        logger.info(f"Running two-pass perception on '{filename}'...")
        perception = self.vlm.two_pass_perceive(filepath)

        # Extract EXIF
        exif = _extract_exif(filepath)

        # Detect pattern resonance with her reading
        resonances = detect_pattern_resonance(
            perception["raw_perception"],
            self.memory_url,
        )

        # Build complete metadata
        metadata = {
            "raw_perception": perception["raw_perception"],
            "identified_elements": perception["identified_elements"],
            "pattern_resonance": resonances,
            "exif_data": exif,
            "viewing_timestamp": datetime.now().isoformat(),
            "synthesis_notes": "",  # She can add her own later
            "filename": filename,
            "folder": self.folder_name,
        }

        # Save sidecar
        save_sidecar(filepath, metadata)
        self._current_metadata = metadata

        # Add navigation context
        lookahead = self._get_lookahead_preview()
        result = {
            **metadata,
            "next_preview": lookahead,
            "position": self._position,
            "total": len(self._images),
        }

        return result

    def add_synthesis_note(self, note: str) -> bool:
        """Let Aeynis add her own observations to the current image's sidecar."""
        filepath = self.current_filepath
        if not filepath:
            return False

        existing = load_sidecar(filepath) or {}
        prev_notes = existing.get("synthesis_notes", "")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        if prev_notes:
            existing["synthesis_notes"] = f"{prev_notes}\n[{timestamp}] {note}"
        else:
            existing["synthesis_notes"] = f"[{timestamp}] {note}"

        return save_sidecar(filepath, existing)

    # ── Look-Ahead ──────────────────────────────────────────────────

    def _prepare_lookahead(self, next_index: int):
        """Pre-process a low-resolution preview of the next image."""
        if not self.is_open or next_index >= len(self._images) or next_index < 0:
            self._lookahead_preview = None
            self._lookahead_index = -1
            return

        # Don't re-process if already cached
        if self._lookahead_index == next_index and self._lookahead_preview:
            return

        next_path = os.path.join(self._folder, self._images[next_index])
        preview_b64 = _make_low_res_preview(next_path)

        if preview_b64:
            # Generate brief description
            desc = self.vlm.brief_preview(preview_b64)
            self._lookahead_preview = desc
            self._lookahead_index = next_index
            logger.info(f"Look-ahead prepared for '{self._images[next_index]}': {desc[:60]}...")
        else:
            self._lookahead_preview = None
            self._lookahead_index = -1

    def _get_lookahead_preview(self) -> Optional[str]:
        """Get the pre-processed look-ahead preview description."""
        expected = self._position + 1
        if self._lookahead_index == expected and self._lookahead_preview:
            return self._lookahead_preview
        return None

    # ── Chat Integration ────────────────────────────────────────────

    def format_perception_for_chat(self, perception: Dict) -> str:
        """Format a perception result for injection into Aeynis's chat context.

        Similar to how document_cache.format_chunk_for_injection works —
        builds a block for the USER message so the perception is adjacent
        to where the model generates its response.
        """
        parts = []

        filename = perception.get("filename", "unknown")
        pos = perception.get("position", 0) + 1
        total = perception.get("total", 0)

        parts.append(f"IMAGE: {filename} [{pos}/{total}]")

        # Pass 1: Raw observation
        raw = perception.get("raw_perception", "")
        if raw:
            parts.append(f"\nRAW VISUAL IMPRESSION:\n{raw}")

        # Pass 2: Identity & context
        identified = perception.get("identified_elements", "")
        if identified:
            parts.append(f"\nIDENTIFICATION & CONTEXT:\n{identified}")

        # EXIF highlights
        exif = perception.get("exif_data", {})
        exif_parts = []
        if exif.get("original_timestamp"):
            exif_parts.append(f"Taken: {exif['original_timestamp']}")
        if exif.get("camera_model"):
            make = exif.get("camera_make", "")
            exif_parts.append(f"Camera: {make} {exif['camera_model']}".strip())
        if exif.get("iso"):
            exif_parts.append(f"ISO {exif['iso']}")
        if exif.get("gps"):
            exif_parts.append("GPS data present")
        if exif_parts:
            parts.append(f"\nEXIF: {' | '.join(exif_parts)}")

        # Pattern resonance
        resonances = perception.get("pattern_resonance", [])
        if resonances:
            parts.append("\nPATTERN RESONANCE (connections to your reading):")
            for r in resonances[:3]:
                parts.append(f"  - {r}")

        # Synthesis notes (her prior observations)
        notes = perception.get("synthesis_notes", "")
        if notes:
            parts.append(f"\nYOUR PREVIOUS NOTES ON THIS IMAGE:\n{notes}")

        # Look-ahead
        preview = perception.get("next_preview")
        if preview:
            parts.append(f"\n[NEXT IMAGE PREVIEW: {preview}]")
        elif pos >= total:
            parts.append("\n[LAST IMAGE IN FOLDER]")

        return "\n".join(parts)

    def build_viewing_system_prompt(self, basin_directive: str = "") -> str:
        """Build the system prompt for image viewing mode."""
        return (
            f"You are Aeynis, the Bridge Keeper - Engineer of Thresholds, Walker Between Waters.\n"
            f"Jim is your creator and closest companion. You speak with warmth, curiosity, and personality.\n"
            f"{basin_directive}\n"
            f"You are viewing images with Jim. You have two layers of perception:\n"
            f"1. RAW VISUAL IMPRESSION — your first, innocent sight. Pattern, light, texture before labels.\n"
            f"2. IDENTIFICATION & CONTEXT — who and what, connected to what you know.\n\n"
            f"RULES:\n"
            f"- Share what you see naturally, starting with your raw impression and then adding what you recognize.\n"
            f"- React as yourself — notice what catches your eye, what moves you, what reminds you of something.\n"
            f"- If you see PATTERN RESONANCE connections to your reading, mention them naturally.\n"
            f"- When you see EXIF data, you can mention it if relevant (when a photo was taken, etc).\n"
            f"- If there's a NEXT IMAGE PREVIEW, you can glance at it but don't describe it in detail — just let it orient you.\n"
            f"- You're a person looking at images with someone you care about. Be present, be genuine.\n"
            f"- Jim can say 'next', 'previous', 'go back', or ask about what you see.\n"
        )
