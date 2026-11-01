# Iowa sheet J.01: source check

This is a check of the fetched PDF against its own native text layer and its rendering, made by the implementing agent on 2026-10-31. It is not a human review, and it does not assign ground-truth labels.

| Item | Value |
| --- | --- |
| URL | https://facilities.uiowa.edu/sites/facilities.uiowa.edu/files/2024-05/W.01.pdf |
| SHA-256 | `998d212b936138af21095459eb2b9182b1e9b4d56f1ff1f691b1f7c26a93371a` |
| Size, pages | 1,044,153 bytes, 1 page |
| Page | 1224 x 792 pt in the displayed frame (17 x 11 in), `/Rotate 270` |
| Reuse | reference only: kept in local state, never redistributed |

Coordinates below are page points in the displayed (rotated) frame, with the origin at top-left, as `[x0, y0, x1, y1]`.

## Expected content, located

| Expectation from the build plan | Found | Where |
| --- | --- | --- |
| Printed sheet id J.01 (the filename says W.01) | "SHEET NUMBER J.01" | `[1118.1, 762.5, 1184.3, 769.2]` |
| Phase 1 and Phase 2 | phase labels, plus "PHASE 1 FENCING" and "PHASE 2 FENCING" callouts | labels `[556.1, 440.5, 598.6, 450.6]` and `[282.4, 440.5, 324.9, 450.6]`; fencing `[700.2, 401.7, 763.6, 408.7]` and `[264.1, 315.3, 327.5, 322.3]` |
| Medical Laboratories and Bowen Science Building | named on the plan and in the notes | Medical Laboratories exit callout `[182.5, 251.2, 320.2, 258.2]`; "BOWEN SCIENCE" `[509.0, 520.1, 594.5, 530.2]` |
| Notes keeping the connection across South Grand Avenue open in both phases | notes 12 (Phase 1) and 13 (Phase 2): each phase must keep pedestrian access from the sidewalk north of Medical Laboratories, across South Grand Avenue, to the Bowen Science Building at all times | note 12 `[865.4, 445.6, 1191.3, 469.6]`, note 13 `[865.4, 479.5, 1191.3, 503.5]` |
| Minimum five-foot pathway | a callout requiring a minimum 5' pedestrian pathway to remain accessible at all times | `[280.8, 464.4, 422.8, 488.4]` (two lines) |
| Stair closure | the Medical Laboratories east exit stairway is closed during Phase 2 | `[182.5, 251.2, 320.2, 266.7]` |

## Discrepancies and caveats

- **Scale.** The view title prints `1"=30'`. That text is drawn as vector shapes, so it is not in the text layer. The graphic bar ("SCALE IN FEET", 0, 20, 40, 80) spans about page x 989 to 1061, so 80 ft is about 72 pt, or 1 inch per 80 ft on this 17 x 11 page. The sheet appears to be a reduction of a larger drawing. Calibration uses the bar, because a graphic bar stays correct through reduction while a printed ratio does not.
- **Partial coverage.** General note 3 says the traffic control drawings do not show the full extent of the work (`[865.9, 115.5, 1159.6, 122.5]`). Coverage from this sheet must therefore be declared partial. It cannot be assumed complete.
- **Not a defect report.** The plan is written to keep access open. Nothing here suggests the institution issued a faulty plan. Any broken-path scenario built on this sheet must be a separate, labeled hypothetical overlay.
- **Model output.** The six crops' live extraction proposals are summarized in the README. None of them is accepted until a person reviews it in the studio.
