"""Size-tuning trial: which sizes to sweep, and how many pretrial flashes
come first. Sizes are fractions of the region's own width, not fixed pixel
values, so the sweep stays sane across differently sized screens/regions —
same reasoning as circle.py deriving a tuning/contrast circle's diameter
from the region rather than a StimParams field. The aperture geometry itself
is shared (circle.py); this module owns only what is specific to Size.
"""
from __future__ import annotations

SIZE_FRACTIONS = (0.2, 0.4, 0.6, 0.8, 1.0)
N_SIZES = len(SIZE_FRACTIONS)
N_PRETRIALS = 2
