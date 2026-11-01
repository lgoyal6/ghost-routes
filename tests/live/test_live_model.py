"""Real local vision-model inference. Fails (never skips) when the model is unavailable.

Remediation for each failure state is printed in the assertion message:
- server_unavailable: start Ollama (`ollama serve`) or set GHOST_ROUTES_OLLAMA_URL.
- model_not_pulled: run `make model`.
"""

from __future__ import annotations

import io

import pytest

from ghost_routes.config import get_settings
from ghost_routes.documents.pdf import inspect_pdf, render_page, tiles
from ghost_routes.documents.transforms import crop_to_page
from ghost_routes.inference.extract import (
    OUTPUT_SCHEMA,
    build_prompt,
    group_lines,
    validate_output,
    words_in_crop,
)
from ghost_routes.inference.ollama import ModelState, OllamaClient
from ghost_routes.services.extraction import inference_options
from ghost_routes.synthetic.plans import project_q_plan

pytestmark = pytest.mark.live_model
EXPECTED_DIGEST = "5ced39dfa4bac325dc183dd1e4febaa1c46b3ea28bce48896c8e69c1e79611cc"
REMEDY = {ModelState.SERVER_UNAVAILABLE: "start Ollama (`ollama serve`) or set GHOST_ROUTES_OLLAMA_URL",
          ModelState.MODEL_NOT_PULLED: "run `make model`"}


def test_real_inference_on_synthetic_sheet_is_schema_valid_and_anchored() -> None:
    cfg = get_settings()
    cl = OllamaClient(cfg.ollama_url, cfg.model, cfg.inference_timeout_s)
    st = cl.status()
    assert st.state == ModelState.READY, f"{st.state.value}: {st.message}. Remedy: {REMEDY.get(st.state, 'see README')}"
    if cfg.model == "qwen2.5vl:7b":
        assert st.digest == EXPECTED_DIGEST, f"installed digest {st.digest} differs from the documented {EXPECTED_DIGEST}"

    pdf, labels, _ = project_q_plan("A")
    pages, _ = inspect_pdf(pdf, max_bytes=cfg.max_upload_bytes, max_pages=1)
    img = render_page(pdf, 0, cfg.render_dpi)
    # The tile that fully contains every phasing note (from ground truth): text the model must read and cite.
    k = cfg.render_dpi / 72
    notes = [lb.bbox for lb in labels if lb.cls == "note"]

    def holds_notes(t: tuple[int, int, int, int]) -> bool:
        return all(t[0] <= b[0] * k and b[2] * k <= t[2] and t[1] <= b[1] * k and b[3] * k <= t[3] for b in notes)

    rx0, ry0, rx1, ry1 = next(t for t in tiles(img.width, img.height, cfg.crop_px, cfg.crop_overlap_px) if holds_notes(t))
    buf = io.BytesIO()
    img.crop((rx0, ry0, rx1, ry1)).save(buf, format="PNG")
    c2p = crop_to_page(rx0, ry0, cfg.render_dpi)
    lines = group_lines(words_in_crop(pages[0].words, c2p.inverse(), rx1 - rx0, ry1 - ry0))
    assert lines, "the selected tile has no native text"

    res = cl.chat_image(build_prompt(lines, rx1 - rx0, ry1 - ry0), buf.getvalue(), OUTPUT_SCHEMA, inference_options())
    rep = validate_output(res.content, lines, c2p, rx1 - rx0, ry1 - ry0)
    assert rep.parse_error is None, f"model output unusable: {rep.parse_error}; done_reason={res.raw.get('done_reason')}"
    assert rep.observations, "model returned no observations for a tile with phasing notes"
    native = {ln["text"] for ln in lines}
    anchored = [ob for ob in rep.observations if ob.supporting_text]
    assert anchored, "no proposal could be anchored to native text"
    for ob in anchored:
        assert all(part in " ".join(native) for part in ob.supporting_text.split()), ob
    assert any("PHASE 1" in ob.phase_refs for ob in rep.observations), "no supported phase reference on a phase-notes tile"
