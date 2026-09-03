"""Trial expansion: loop variables -> a full-factorial list of StimParams.

Port of logicLibHelpers.genParamCombos (MATLAB ndgrid) without MATLAB structs.
Trial order differs from ndgrid's column-major flattening, but the SET of
combinations is identical, which is what a full-factorial design needs.
"""
from __future__ import annotations

import itertools
from dataclasses import replace

from .settings import LoopVar, StimParams


def gen_param_combos(base: StimParams,
                      loops: dict[str, LoopVar]) -> list[StimParams]:
    """Every combination of the loop variables' values, applied over `base`.

    A loop name that doesn't match a StimParams field is skipped rather than
    raising — mirrors MATLAB's dynamic struct-field assignment, which had no
    such check at all, but without silently corrupting a trial with a
    misspelled field.
    """
    names = [n for n, lv in loops.items()
             if lv.values and n in StimParams.__dataclass_fields__]
    if not names:
        return [base]
    value_lists = [loops[n].values for n in names]
    return [replace(base, **dict(zip(names, combo)))
            for combo in itertools.product(*value_lists)]
