# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from pathlib import Path
import tempfile
import unittest
import uuid
import pytest

import rachis
from rachis import Artifact
from rachis.sdk.result import ChecksumCache
from rachis.core.testing.type import IntSequence1, Mapping
from rachis.core.testing.util import get_dummy_plugin
from rachis.core.util import checksum_native, checksum_python
from rachis.core.archive.format.util import artifact_version
from rachis.core.archive.provenance_lib.tests.testing_utilities import (
    write_zip_file
)


class TestChecksumCache(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = get_dummy_plugin()

        cls.checksum_cache_disabled = unittest.mock.patch(
            'rachis.sdk.result.ChecksumCache.cache_artifact',
            side_effect=lambda artifact: None
        )

    def setUp(self):
        ChecksumCache().cache = {}

    @unittest.mock.patch(
        'rachis.core.util.checksum_python', wraps=checksum_python
    )
    @unittest.mock.patch(
        'rachis.core.util.checksum_native', wraps=checksum_native
    )
    def test_checksum_cache_fewer_checksum_calls(
        self, native_mock, python_mock
    ):
        '''
        Tests that the checksum cache causes the intensive checksum function
        to run fewer times by counting the number of calls to the checksum
        function once when the cache is enabled and once when it is disabled,
        and comparing.
        '''
        def get_call_count():
            return native_mock.call_count + python_mock.call_count

        int_seq = Artifact.import_data(IntSequence1, [1, 2, 3, 4])

        # without cache
        with self.checksum_cache_disabled:
            left, right = self.plugin.actions['split_ints'](int_seq)
            calls_without_cache = get_call_count()

        # with cache
        native_mock.reset_mock()
        python_mock.reset_mock()
        left, right = self.plugin.actions['split_ints'](int_seq)
        calls_with_cache = get_call_count()

        self.assertGreater(calls_without_cache, calls_with_cache)

    @pytest.mark.usefixtures("legacy_tree_cache")
    def test_checksum_cache_equivalent_results(self):
        '''
        Tests that the checksum cache produces the same checksums file
        in output artifacts as would have been produced without caching.
        '''
        some_uuid = uuid.uuid4()
        consistent_uuid = unittest.mock.patch(
            'uuid.uuid4', side_effect=lambda: some_uuid
        )

        int_seq = Artifact.import_data(IntSequence1, [1, 2, 3, 4])

        with consistent_uuid:
            with self.checksum_cache_disabled:
                no_cache_outputs = self.plugin.actions['split_ints'](int_seq)

            # ensure cache has not been used
            checksum_cache = ChecksumCache()
            self.assertEqual(checksum_cache.cache, {})

            cache_outputs = self.plugin.actions['split_ints'](int_seq)

            # ensure cache has been used
            self.assertNotEqual(checksum_cache.cache, {})

            for i in (0, 1):
                self.assertEqual(
                    no_cache_outputs[i].get_checksums(),
                    cache_outputs[i].get_checksums()
                )

    def test_checksum_cache_handles_checksum_algorithm_boundaries(self):
        '''
        Tests that the checksum cache properly handles input artifacts of
        differing checksum algorithm type than the current one. The pre-v7
        artifacts use md5sum as the checksum algorithm, while artifacts of v7
        or later use sha512--this boundary is tested here.

        The checksum validation of the outputs would error if the input md5sums
        had been cached and written to the checksums file, because during
        validation of the output artifacts, each of their files is
        re-checksummed using the checksum algorithm associated with their
        version (sha512 and 7.1, respectively).
        '''
        with artifact_version('6'):
            v6_int_seq = Artifact.import_data(IntSequence1, [1, 2, 3, 4])
            mapping = Artifact.import_data(Mapping, {'a': 42})

        with artifact_version('7.1'):
            outputs = self.plugin.pipelines['typical_pipeline'](
                int_sequence=v6_int_seq, mapping=mapping, do_extra_thing=True
            )

        for output in outputs:
            if isinstance(output, Artifact):
                output.validate_checksums()

    def test_checksum_cache_handles_artifacts_with_no_checksums(self):
        '''
        Tests that artifacts with a format that does not contain checksums are
        handled by the checksum cache.
        '''
        v3_artifact_dir = (
            Path(rachis.__file__).parent / 'core' / 'archive' /
            'provenance_lib' / 'tests' / 'data' / 'concated-ints-v3'
        )

        with tempfile.TemporaryDirectory() as tempdir:
            temp_zf_path = Path(tempdir) / 'v3-artifact.zip'
            write_zip_file(temp_zf_path, v3_artifact_dir)
            v3_artifact = Artifact.load(temp_zf_path)

        ints1, ints2 = self.plugin.actions['split_ints'](v3_artifact)
        ints1.validate_checksums()
        ints2.validate_checksums()
