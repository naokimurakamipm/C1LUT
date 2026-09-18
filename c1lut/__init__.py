"""C1LUT — measurable CUBE LUT → ICC pipeline for Capture One.

Design goals (see spec):
- Base ICC injection concept kept; Camera RGB is the pipeline origin.
- ICC PCS / tag semantics strictly consistent with the base profile header.
- No implicit heuristics: every non-standard correction is opt-in.
- High-precision (float) evaluation of the base profile — no 8-bit sampling.
- Independent ΔE2000 validation of the generated ICC against the reference path.
"""

__version__ = "2.0.0"

APP_NAME = "C1LUT"
