"""Size trial: the sizes swept and the pretrial flash count. Sizes are
fractions of the region's width, so the sweep suits any screen."""
from __future__ import annotations

SIZE_FRACTIONS = (0.2, 0.4, 0.6, 0.8, 1.0)
N_SIZES = len(SIZE_FRACTIONS)
N_PRETRIALS = 2
