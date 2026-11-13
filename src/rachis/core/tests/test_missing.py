# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import unittest

import pandas as pd
import pandas.testing as pdt

from rachis.core.missing import (
    encode_and_get_missing_mask, decode_from_missing_mask
)
from rachis.metadata.base import CATEGORICAL_DTYPE


class RoundTripMixin:
    def check_roundtrip(self, non_missing_values, dtype, series_dtype=None,
                        *, extra_missing=()):
        series = pd.Series(
            [*non_missing_values, *extra_missing, *self.missing_terms],
            dtype=series_dtype)

        encoded, mask = encode_and_get_missing_mask(series, self.enum)
        decoded = decode_from_missing_mask(encoded, mask)

        self.assertEqual(encoded.dtype, dtype)
        self.assertEqual(encoded.dropna().tolist(), non_missing_values)
        pdt.assert_series_equal(decoded, series)

    def test_roundtrip_float(self):
        self.check_roundtrip([0.05], float)

    def test_roundtrip_string(self):
        self.check_roundtrip(['hello'], CATEGORICAL_DTYPE,
                             series_dtype=CATEGORICAL_DTYPE)

    def test_roundtrip_int(self):
        self.check_roundtrip([42], float)

    def test_roundtrip_bool(self):
        self.check_roundtrip([True], object)

    def test_roundtrip_all_missing_object(self):
        self.check_roundtrip([], object, series_dtype=object,
                             extra_missing=(None, float('nan')))

    def test_roundtrip_all_missing_string(self):
        self.check_roundtrip([], CATEGORICAL_DTYPE,
                             series_dtype=CATEGORICAL_DTYPE,
                             extra_missing=(None, float('nan')))


class TestISNDC(RoundTripMixin, unittest.TestCase):
    def setUp(self):
        self.enum = 'INSDC:missing'
        self.missing_terms = ['not applicable', 'missing', 'not collected',
                              'not provided', 'restricted access']


class TestOmitted(RoundTripMixin, unittest.TestCase):
    def setUp(self):
        self.enum = 'blank'
        self.missing_terms = [None, float('nan')]

    # test_roundtrip_all_missing_float is not possible with other schemes
    def test_roundtrip_all_missing_float(self):
        self.check_roundtrip([], float, series_dtype=float,
                             extra_missing=(None, float('nan')))


class TestError(RoundTripMixin, unittest.TestCase):
    def setUp(self):
        self.enum = 'no-missing'
        self.missing_terms = []

    # no missing values, so bool and int are not object and float
    def test_roundtrip_bool(self):
        self.check_roundtrip([True], bool)

    def test_roundtrip_int(self):
        self.check_roundtrip([42], int)

    def test_roundtrip_all_missing_object(self):
        with self.assertRaisesRegex(ValueError, 'Missing values.*name=None'):
            super().test_roundtrip_all_missing_object()

    def test_roundtrip_all_missing_string(self):
        with self.assertRaisesRegex(ValueError, 'Missing values.*name=None'):
            super().test_roundtrip_all_missing_string()
