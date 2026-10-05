# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import pandas as pd
import numpy as np


def _validate_no_missing(series: pd.Series):
    if series.isna().any():
        msg = (
            "Missing values are not allowed in series/column "
            f"(name={series.name}) when using scheme 'no-missing'."
        )
        raise ValueError(msg)


_MISSING_ENUMS = {
    'INSDC:missing': (
        'not applicable', 'missing', 'not collected', 'not provided',
        'restricted access'
    ),
    'blank': (),
    'no-missing': (),
}
_MISSING_VALIDATORS = {
    'no-missing': _validate_no_missing
}
DEFAULT_MISSING = 'blank'


def encode_and_get_missing_mask(
    series: pd.Series, enumeration: str
) -> tuple[pd.Series, pd.Series]:
    if type(enumeration) is not str:
        TypeError("Wrong type for `enumeration`, expected string")

    if enumeration not in _MISSING_ENUMS:
        raise ValueError(
            f"Unknown enumeration: {enumeration}, "
            f"(available: {list(_MISSING_ENUMS.keys())})."
        )

    if enumeration in _MISSING_VALIDATORS:
        validator = _MISSING_VALIDATORS[enumeration]
        validator(series)

    to_encode = _MISSING_ENUMS[enumeration]
    encoded = series.where(~series.isin(to_encode), np.nan)
    missing_mask = series.where(series.isin(to_encode), np.nan)

    if pd.api.types.is_object_dtype(series.dtype) and encoded.isna().all():
        # float64 by default, return to categorical
        encoded = encoded.astype(object)
    else:
        encoded = encoded.infer_objects()

    return encoded, missing_mask


def decode_from_missing_mask(
    series: pd.Series, missing_mask: pd.Series
) -> pd.Series:
    return series.where(missing_mask.isna(), missing_mask)
