"""Loop variables -> a full-factorial list of StimParams.

Port of logicLibHelpers.genParamCombos. Order differs from ndgrid's
column-major flattening; the set of combinations is identical.
"""
from __future__ import annotations

import itertools
from dataclasses import replace

from .settings import LoopVar, StimParams


def gen_param_combos(base: StimParams,
                      loops: dict[str, LoopVar]) -> list[StimParams]:
    """Every combination of the loop values over `base`. Names that aren't
    StimParams fields are skipped."""
    names = [n for n, lv in loops.items()
             if lv.values and n in StimParams.__dataclass_fields__]
    if not names:
        return [base]
    value_lists = [loops[n].values for n in names]
    return [replace(base, **dict(zip(names, combo)))
            for combo in itertools.product(*value_lists)]
