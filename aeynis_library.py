#!/usr/bin/env python3
"""
Aeynis Library - File management for Aeynis's Configuration Space

Provides read, write, review, and list operations on a dedicated local directory
(the 'Aeynis Library') with a configurable size limit (default 50GB).

Supported formats:
  Read:  PDF, TXT, HTML, Markdown, RTF
  Write: Markdown (primary), TXT, HTML, ODT (via LibreOffice headless)

Process resilience: all file operations use safe open/close patterns with
timeouts and locks to prevent crashes on corrupted or locked files.
"""

import fcntl
import hashlib
import html
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_LIBRARY_ROOT = os.path.expanduser("~/AeynisLibrary")
DEFAULT_SIZE_LIMIT_GB = 50
LOCK_TIMEOUT_SECONDS = 5
MAX_READ_SIZE_MB = 100  # Skip files larger than this


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dir_size_bytes(path: str) -> int:
    """Walk a directory tree and return total size in bytes."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def _safe_filename(name: str) -> str:
    """Sanitize a filename: keep alphanumeric, hyphens, underscores, dots."""
    name = re.sub(r'[^\w\-. ]', '', name)
    name = re.sub(r'\s+', '_', name.strip())
    return name or "untitled"


def _acquire_lock(fp, timeout: int = LOCK_TIMEOUT_SECONDS) -> bool:
    """Try to acquire an exclusive lock on an open file descriptor."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (IOError, OSError):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)


def _release_lock(fp):
    try:
        fcntl.flock(fp, fcntl.LOCK_UN)
    except (IOError, OSError):
        pass


# ---------------------------------------------------------------------------
# PDF extraction
# ---------------------------------------------------------------------------

def _ocr_pdf(filepath: str) -> str:
    """OCR a scanned PDF using pdftoppm + tesseract."""
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Convert PDF pages to images
            subprocess.run(
                ["pdftoppm", "-png", "-r", "300", filepath,
                 os.path.join(tmpdir, "page")],
                capture_output=True, timeout=120, check=True,
            )
            # OCR each page image in order
            pages = []
            for img in sorted(os.listdir(tmpdir)):
                if not img.endswith(".png"):
                    continue
                img_path = os.path.join(tmpdir, img)
                result = subprocess.run(
                    ["tesseract", img_path, "stdout"],
                    capture_output=True, text=True, timeout=60,
                )
                if result.returncode == 0 and result.stdout.strip():
                    page_num = len(pages) + 1
                    pages.append(f"--- Page {page_num} ---\n{result.stdout.strip()}")
            if pages:
                logger.info(f"OCR extracted {len(pages)} pages from {os.path.basename(filepath)}")
                return "\n\n".join(pages)
    except FileNotFoundError:
        logger.warning("pdftoppm or tesseract not installed - cannot OCR scanned PDF")
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
        logger.warning(f"OCR failed for {filepath}: {e}")
    return ""


def _extract_pdf_text(filepath: str) -> str:
    """Extract text from a PDF using pdftotext, PyPDF2, or OCR for scanned pages."""
    # Try pdftotext (poppler-utils) first - fastest and most accurate
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", filepath, "-"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback: PyPDF2
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(filepath)
        pages = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            pages.append(f"--- Page {i + 1} ---\n{text}")
        combined = "\n\n".join(pages)
        # Check if we actually got meaningful text (scanned PDFs return near-empty)
        text_chars = sum(1 for c in combined if c.isalpha())
        logger.info(f"PyPDF2 extracted {len(reader.pages)} pages, {text_chars} alpha chars from {os.path.basename(filepath)}")
        if text_chars > 50:
            return combined
        else:
            logger.info(f"PyPDF2 text too sparse ({text_chars} chars) - likely scanned PDF")
    except Exception as e:
        logger.warning(f"PyPDF2 fallback failed for {filepath}: {e}")

    # Last resort: OCR for scanned/image-based PDFs
    logger.info(f"No text layer found in {os.path.basename(filepath)}, attempting OCR...")
    ocr_text = _ocr_pdf(filepath)
    if ocr_text:
        return ocr_text

    return f"[Could not extract text from PDF: {os.path.basename(filepath)}. If scanned, install tesseract-ocr: sudo apt install tesseract-ocr]"


# ---------------------------------------------------------------------------
# HTML extraction
# ---------------------------------------------------------------------------

def _extract_html_text(filepath: str) -> str:
    """Extract readable text from an HTML file."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()

        # Try BeautifulSoup if available
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(raw, "html.parser")
            # Remove script/style elements
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            return soup.get_text(separator="\n", strip=True)
        except ImportError:
            pass

        # Crude fallback: strip tags
        text = re.sub(r'<script[^>]*>.*?</script>', '', raw, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = html.unescape(text)
        return re.sub(r'\s+', ' ', text).strip()

    except Exception as e:
        return f"[Could not extract text from HTML: {e}]"


# ---------------------------------------------------------------------------
# LibreOffice document creation
# ---------------------------------------------------------------------------

def _markdown_to_odt(md_path: str, out_dir: str) -> Optional[str]:
    """Convert a Markdown file to ODT using pandoc or LibreOffice."""
    base = Path(md_path).stem
    odt_path = os.path.join(out_dir, f"{base}.odt")

    # Try pandoc first
    try:
        subprocess.run(
            ["pandoc", md_path, "-o", odt_path],
            capture_output=True, timeout=30, check=True
        )
        return odt_path
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass

    # Try LibreOffice headless
    try:
        subprocess.run(
            ["libreoffice", "--headless", "--convert-to", "odt",
             "--outdir", out_dir, md_path],
            capture_output=True, timeout=60, check=True
        )
        if os.path.exists(odt_path):
            return odt_path
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass

    return None


# ---------------------------------------------------------------------------
# Main Library class
# ---------------------------------------------------------------------------

class AeynisLibrary:
    """
    Manages Aeynis's personal file library ('Configuration Space').

    Provides:
      - list_files()   : enumerate library contents
      - read_file()    : extract text from PDF / TXT / HTML / MD
      - write_file()   : create or overwrite a document
      - review_file()  : create a review/annotation of an existing file
      - get_file_info(): metadata about a single file
      - delete_file()  : remove a file
      - usage()        : disk usage vs. quota
    """

    def __init__(self, root: str = DEFAULT_LIBRARY_ROOT,
                 size_limit_gb: float = DEFAULT_SIZE_LIMIT_GB):
        self.root = os.path.abspath(root)
        self.size_limit_bytes = int(size_limit_gb * (1024 ** 3))

        # Create the directory structure
        os.makedirs(self.root, exist_ok=True)
        os.makedirs(os.path.join(self.root, "reviews"), exist_ok=True)
        os.makedirs(os.path.join(self.root, "originals"), exist_ok=True)
        os.makedirs(os.path.join(self.root, "imports"), exist_ok=True)

        logger.info(f"AeynisLibrary initialized at {self.root} "
                     f"(limit: {size_limit_gb} GB)")

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def _resolve_path(self, filename: str, subdir: str = "") -> str:
        """Resolve a filename to a full path inside the library.
        Prevents path traversal attacks.

        Tries the exact basename first (for files placed manually), then
        falls back to the sanitized name (for files created via write/import).
        """
        basename = os.path.basename(filename)
        base = os.path.join(self.root, subdir) if subdir else self.root
        root_abs = os.path.abspath(self.root)

        # Try exact filename first (handles files placed manually with spaces/parens)
        exact = os.path.abspath(os.path.join(base, basename))
        if exact.startswith(root_abs) and os.path.exists(exact):
            return exact

        # Fall back to sanitized filename
        safe = _safe_filename(basename)
        full = os.path.abspath(os.path.join(base, safe))
        if not full.startswith(root_abs):
            raise ValueError("Path traversal detected")
        return full

    def _check_quota(self, additional_bytes: int = 0) -> bool:
        """Return True if adding additional_bytes would stay under quota."""
        current = _dir_size_bytes(self.root)
        return (current + additional_bytes) <= self.size_limit_bytes

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def usage(self) -> Dict:
        """Return disk usage information."""
        used = _dir_size_bytes(self.root)
        return {
            "used_bytes": used,
            "used_human": self._human_size(used),
            "limit_bytes": self.size_limit_bytes,
            "limit_human": self._human_size(self.size_limit_bytes),
            "available_bytes": max(0, self.size_limit_bytes - used),
            "available_human": self._human_size(max(0, self.size_limit_bytes - used)),
            "percent_used": round(used / self.size_limit_bytes * 100, 2) if self.size_limit_bytes else 0,
        }

    def list_files(self, subdir: str = "") -> List[Dict]:
        """List files in the library (or a subdirectory)."""
        target = os.path.join(self.root, subdir) if subdir else self.root
        if not os.path.isdir(target):
            return []

        files = []
        for entry in sorted(os.scandir(target), key=lambda e: e.name):
            if entry.is_file():
                stat = entry.stat()
                files.append({
                    "name": entry.name,
                    "path": os.path.relpath(entry.path, self.root),
                    "size_bytes": stat.st_size,
                    "size_human": self._human_size(stat.st_size),
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "type": mimetypes.guess_type(entry.name)[0] or "unknown",
                })
            elif entry.is_dir():
                files.append({
                    "name": entry.name + "/",
                    "path": os.path.relpath(entry.path, self.root) + "/",
                    "size_bytes": 0,
                    "size_human": "--",
                    "modified": datetime.fromtimestamp(entry.stat().st_mtime).isoformat(),
                    "type": "directory",
                })
        return files

    def read_file(self, filename: str, subdir: str = "") -> Dict:
        """
        Read and extract text content from a file in the library.
        Supports: PDF, TXT, MD, HTML, RTF.
        Returns dict with 'content', 'filename', 'format', 'size'.
        """
        filepath = self._resolve_path(filename, subdir)
        if not os.path.isfile(filepath):
            return {"error": f"File not found: {filename}", "success": False}

        stat = os.stat(filepath)
        if stat.st_size > MAX_READ_SIZE_MB * 1024 * 1024:
            return {"error": f"File too large ({self._human_size(stat.st_size)}). "
                             f"Max: {MAX_READ_SIZE_MB} MB", "success": False}

        ext = Path(filepath).suffix.lower()

        try:
            # Acquire advisory lock to avoid reading while another process writes
            fh = open(filepath, "rb")
            if not _acquire_lock(fh):
                fh.close()
                return {"error": f"File is locked: {filename}", "success": False}

            try:
                if ext == ".pdf":
                    content = _extract_pdf_text(filepath)
                elif ext in (".html", ".htm"):
                    fh.close()  # re-open as text
                    content = _extract_html_text(filepath)
                    fh = open(filepath, "rb")  # re-acquire for lock release
                elif ext in (".txt", ".md", ".markdown", ".rst", ".rtf", ".csv",
                             ".json", ".xml", ".log"):
                    content = fh.read().decode("utf-8", errors="replace")
                else:
                    content = f"[Unsupported format: {ext}. Supported: PDF, TXT, MD, HTML]"
            finally:
                _release_lock(fh)
                fh.close()

            return {
                "success": True,
                "filename": filename,
                "format": ext.lstrip("."),
                "size_bytes": stat.st_size,
                "size_human": self._human_size(stat.st_size),
                "content": content,
            }

        except Exception as e:
            logger.error(f"Error reading {filename}: {e}")
            return {"error": f"Failed to read {filename}: {e}", "success": False}

    def write_file(self, filename: str, content: str,
                   subdir: str = "originals",
                   fmt: str = "md",
                   convert_to_odt: bool = False) -> Dict:
        """
        Write a document to the library.

        Args:
            filename: Name for the file (extension added if missing)
            content:  Text content to write
            subdir:   Subdirectory ('originals', 'reviews', or '')
            fmt:      Format - 'md', 'txt', or 'html'
            convert_to_odt: Also produce an ODT copy via LibreOffice/pandoc

        Returns dict with 'success', 'path', optionally 'odt_path'.
        """
        # Ensure extension
        if not filename.endswith(f".{fmt}"):
            filename = f"{filename}.{fmt}"

        filepath = self._resolve_path(filename, subdir)
        content_bytes = content.encode("utf-8")

        if not self._check_quota(len(content_bytes)):
            usage = self.usage()
            return {
                "error": f"Library quota exceeded. "
                         f"Used: {usage['used_human']} / {usage['limit_human']}",
                "success": False,
            }

        try:
            os.makedirs(os.path.dirname(filepath), exist_ok=True)

            # Write atomically via temp file
            fd, tmp_path = tempfile.mkstemp(
                dir=os.path.dirname(filepath), suffix=f".{fmt}")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(content_bytes)
                os.replace(tmp_path, filepath)
            except Exception:
                os.unlink(tmp_path)
                raise

            result = {
                "success": True,
                "filename": filename,
                "path": os.path.relpath(filepath, self.root),
                "size_human": self._human_size(len(content_bytes)),
                "written_at": datetime.now().isoformat(),
            }

            # Optional ODT conversion
            if convert_to_odt and fmt == "md":
                odt_path = _markdown_to_odt(filepath, os.path.dirname(filepath))
                if odt_path:
                    result["odt_path"] = os.path.relpath(odt_path, self.root)
                    result["odt_available"] = True
                else:
                    result["odt_available"] = False
                    result["odt_note"] = "pandoc/libreoffice not available for conversion"

            logger.info(f"Wrote {filename} to {filepath}")
            return result

        except Exception as e:
            logger.error(f"Error writing {filename}: {e}")
            return {"error": f"Failed to write {filename}: {e}", "success": False}

    def review_file(self, source_filename: str, review_content: str,
                    source_subdir: str = "",
                    reviewer: str = "Aeynis") -> Dict:
        """
        Create a review/annotation document for an existing file.

        The review is saved in the 'reviews/' subdirectory with a reference
        back to the original file.
        """
        source_path = self._resolve_path(source_filename, source_subdir)
        if not os.path.isfile(source_path):
            return {"error": f"Source file not found: {source_filename}",
                    "success": False}

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        review_name = f"review_{Path(source_filename).stem}_{timestamp}.md"

        # Build review document
        header = (
            f"# Review: {source_filename}\n\n"
            f"**Reviewed by:** {reviewer}\n"
            f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"**Source:** {source_filename}\n\n"
            f"---\n\n"
        )

        return self.write_file(
            filename=review_name,
            content=header + review_content,
            subdir="reviews",
            fmt="md"
        )

    def get_file_info(self, filename: str, subdir: str = "") -> Dict:
        """Get metadata about a file without reading its full content."""
        filepath = self._resolve_path(filename, subdir)
        if not os.path.isfile(filepath):
            return {"error": f"File not found: {filename}", "success": False}

        stat = os.stat(filepath)
        ext = Path(filepath).suffix.lower()

        # Quick checksum for small files
        checksum = None
        if stat.st_size < 10 * 1024 * 1024:  # Under 10MB
            try:
                with open(filepath, "rb") as f:
                    checksum = hashlib.sha256(f.read()).hexdigest()[:16]
            except Exception:
                pass

        return {
            "success": True,
            "filename": filename,
            "path": os.path.relpath(filepath, self.root),
            "format": ext.lstrip("."),
            "size_bytes": stat.st_size,
            "size_human": self._human_size(stat.st_size),
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "created": datetime.fromtimestamp(stat.st_ctime).isoformat(),
            "mime_type": mimetypes.guess_type(filepath)[0] or "unknown",
            "checksum_sha256_short": checksum,
        }

    def delete_file(self, filename: str, subdir: str = "") -> Dict:
        """Delete a file from the library."""
        filepath = self._resolve_path(filename, subdir)
        if not os.path.isfile(filepath):
            return {"error": f"File not found: {filename}", "success": False}

        try:
            os.remove(filepath)
            logger.info(f"Deleted {filename} from library")
            return {"success": True, "deleted": filename}
        except Exception as e:
            return {"error": f"Failed to delete {filename}: {e}", "success": False}

    def import_file(self, source_path: str) -> Dict:
        """Copy an external file into the library's imports/ folder."""
        if not os.path.isfile(source_path):
            return {"error": f"Source not found: {source_path}", "success": False}

        size = os.path.getsize(source_path)
        if not self._check_quota(size):
            return {"error": "Library quota exceeded", "success": False}

        dest_name = _safe_filename(os.path.basename(source_path))
        dest_path = os.path.join(self.root, "imports", dest_name)

        # Avoid overwriting - append number if needed
        if os.path.exists(dest_path):
            stem = Path(dest_name).stem
            ext = Path(dest_name).suffix
            counter = 1
            while os.path.exists(dest_path):
                dest_path = os.path.join(self.root, "imports",
                                         f"{stem}_{counter}{ext}")
                counter += 1

        try:
            shutil.copy2(source_path, dest_path)
            return {
                "success": True,
                "filename": os.path.basename(dest_path),
                "path": os.path.relpath(dest_path, self.root),
                "size_human": self._human_size(size),
            }
        except Exception as e:
            return {"error": f"Import failed: {e}", "success": False}

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _human_size(nbytes: int) -> str:
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if abs(nbytes) < 1024:
                return f"{nbytes:.1f} {unit}"
            nbytes /= 1024
        return f"{nbytes:.1f} PB"
