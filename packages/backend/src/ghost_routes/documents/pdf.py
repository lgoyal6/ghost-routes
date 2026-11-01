"""Bounded PDF inspection, native extraction and rendering.

Untrusted documents are parsed in a separate process with a wall-clock timeout
and a CPU limit, so a pathological file cannot hang the API or worker.
"""

from __future__ import annotations

import io
import json
import os
import resource
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PDF_MAGIC = b"%PDF-"


class DocumentRejected(ValueError):
    """The upload is not an acceptable PDF (type, size, pages, encryption, parse failure)."""


@dataclass(frozen=True)
class PageInfo:
    index: int
    width_pt: float
    height_pt: float
    rotation: int
    words: list[dict[str, Any]]
    vectors: dict[str, Any]


def _limit_cpu(seconds: int) -> None:
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (seconds, seconds + 1))
    except (ValueError, OSError):
        pass


def _extract(data: bytes, max_pages: int, cpu_s: int) -> dict[str, Any]:
    _limit_cpu(cpu_s)
    import pdfplumber
    import pypdfium2 as pdfium

    try:
        doc = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        return {"error": f"unreadable or encrypted PDF: {exc}"}
    n = len(doc)
    if n == 0:
        return {"error": "PDF has no pages"}
    if n > max_pages:
        return {"error": f"PDF has {n} pages; limit is {max_pages}"}
    pages = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for i, page in enumerate(pdf.pages):
            words = []
            for j, w in enumerate(page.extract_words(keep_blank_chars=False, use_text_flow=False, extra_attrs=["size"])):
                words.append({"id": j, "text": w["text"], "x0": round(w["x0"], 3), "top": round(w["top"], 3),
                              "x1": round(w["x1"], 3), "bottom": round(w["bottom"], 3), "upright": bool(w["upright"]),
                              "size": round(float(w.get("size", 0.0)), 2)})
            dashed = 0
            for ln in page.lines + page.curves:
                if ln.get("dash") and ln["dash"][0]:
                    dashed += 1
            pages.append({
                "index": i, "width_pt": float(page.width), "height_pt": float(page.height),
                "rotation": int(page.rotation or 0) % 360, "words": words,
                "vectors": {"lines": len(page.lines), "curves": len(page.curves), "rects": len(page.rects),
                            "images": len(page.images), "dashed": dashed},
            })
    meta = doc.get_metadata_dict() if hasattr(doc, "get_metadata_dict") else {}
    return {"pages": pages, "metadata": {k: str(v) for k, v in (meta or {}).items() if v}}


def inspect_pdf(data: bytes, *, max_bytes: int, max_pages: int, timeout_s: float = 60.0) -> tuple[list[PageInfo], dict[str, str]]:
    """Parse in a child interpreter (``python -m``) with a CPU limit and wall-clock timeout."""
    if not data.startswith(PDF_MAGIC):
        raise DocumentRejected("not a PDF (missing %PDF- header)")
    if len(data) > max_bytes:
        raise DocumentRejected(f"PDF is {len(data)} bytes; limit is {max_bytes}")
    cmd = [sys.executable, "-I", "-m", "ghost_routes.documents.pdf", str(max_pages), str(int(timeout_s))]
    try:
        proc = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout_s, check=False,
                              env={**os.environ, "PYTHONPATH": _package_root()})
    except subprocess.TimeoutExpired as exc:
        raise DocumentRejected(f"parsing exceeded {timeout_s:.0f}s") from exc
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace").strip().splitlines()[-1:] or ["no output"]
        raise DocumentRejected(f"parser exited with status {proc.returncode}: {tail[0][:300]}")
    out = json.loads(proc.stdout)
    if "error" in out:
        raise DocumentRejected(out["error"])
    pages = [PageInfo(p["index"], p["width_pt"], p["height_pt"], p["rotation"], p["words"], p["vectors"]) for p in out["pages"]]
    return pages, out["metadata"]


def _package_root() -> str:
    return str(Path(__file__).resolve().parents[2])


def render_page(data: bytes, index: int, dpi: int):  # type: ignore[no-untyped-def]
    """Render one page (rotation applied) to a PIL RGB image."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(data)
    try:
        return doc[index].render(scale=dpi / 72.0).to_pil().convert("RGB")
    finally:
        doc.close()


def tiles(width: int, height: int, size: int, overlap: int) -> list[tuple[int, int, int, int]]:
    """Cover the render with square tiles; edge tiles are shifted inward, not shrunk."""
    def starts(total: int) -> list[int]:
        if total <= size:
            return [0]
        step = size - overlap
        xs = list(range(0, total - size, step)) + [total - size]
        return sorted(set(xs))
    return [(x, y, min(x + size, width), min(y + size, height)) for y in starts(height) for x in starts(width)]


if __name__ == "__main__":  # child process entry point
    _max_pages, _cpu = int(sys.argv[1]), int(sys.argv[2])
    try:
        result = _extract(sys.stdin.buffer.read(), _max_pages, _cpu)
    except Exception as _exc:  # noqa: BLE001 - reported to the parent as data
        result = {"error": f"{type(_exc).__name__}: {_exc}"}
    sys.stdout.write(json.dumps(result))
