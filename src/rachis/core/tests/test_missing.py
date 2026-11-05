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
    def check_roundtrip(self, real_value, dtype, series_dtype=None):
        notna_exp = [real_value]
        series = pd.Series(notna_exp + self.missing_terms,
                           dtype=series_dtype)

        encoded, mask = encode_and_get_missing_mask(series, self.enum)
        missing = decode_from_missing_mask(encoded, mask)

        # encoded = series_encode_missing(series, self.enum)
        # missing = series_extract_missing(encoded)

        self.assertEqual(encoded.dtype, dtype)

        # the non-null side of the series
        self.assertEqual(list(encoded[encoded.notna()]), notna_exp)

        # the null end (but in the orginal vocabulary)
        # pdt.assert_series_equal(missing, series[1:].astype(object))
        pdt.assert_series_equal(missing, series)

    def test_roundtrip_float(self):
        self.check_roundtrip(0.05, float)

    def test_roundtrip_string(self):
        self.check_roundtrip('hello', CATEGORICAL_DTYPE,
                             series_dtype=CATEGORICAL_DTYPE)

    def test_roundtrip_int(self):
        self.check_roundtrip(42, float)

    def test_roundtrip_bool(self):
        self.check_roundtrip(True, object)

    def test_roundtrip_all_missing_object(self):
        expected = [None, float('nan')] + self.missing_terms
        series = pd.Series(expected, dtype=object)

        # encoded = series_encode_missing(series, self.enum)
        # missing = series_extract_missing(encoded)
        encoded, mask = encode_and_get_missing_mask(series, self.enum)
        missing = decode_from_missing_mask(encoded, mask)

        self.assertEqual(encoded.dtype, object)
        pdt.assert_series_equal(missing, series)

    def test_roundtrip_all_missing_string(self):
        expected = [None, float('nan')] + self.missing_terms
        series = pd.Series(expected, dtype=CATEGORICAL_DTYPE)

        encoded, mask = encode_and_get_missing_mask(series, self.enum)
        missing = decode_from_missing_mask(encoded, mask)

        self.assertEqual(encoded.dtype, CATEGORICAL_DTYPE)
        pdt.assert_series_equal(missing, series)


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
        expected = [None, float('nan')] + self.missing_terms
        series = pd.Series(expected, dtype=float)

        encoded, mask = encode_and_get_missing_mask(series, self.enum)
        missing = decode_from_missing_mask(encoded, mask)

        self.assertEqual(encoded.dtype, float)
        pdt.assert_series_equal(missing, series)


class TestError(RoundTripMixin, unittest.TestCase):
    def setUp(self):
        self.enum = 'no-missing'
        self.missing_terms = []

    # no missing values, so bool and int are not object and float
    def test_roundtrip_bool(self):
        self.check_roundtrip(True, bool)

    def test_roundtrip_int(self):
        self.check_roundtrip(42, int)

    def test_roundtrip_all_missing_object(self):
        with self.assertRaisesRegex(ValueError, 'Missing values.*name=None'):
            super().test_roundtrip_all_missing_object()

    def test_roundtrip_all_missing_string(self):
        with self.assertRaisesRegex(ValueError, 'Missing values.*name=None'):
            super().test_roundtrip_all_missing_string()
