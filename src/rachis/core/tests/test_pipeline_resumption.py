# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import os
import tempfile
import unittest

import pandas as pd

import rachis
from rachis.core.cache import Cache
from rachis.core.testing.type import IntSequence1, SingleInt
from rachis.core.testing.util import get_dummy_plugin, PipelineError
from rachis.core.archive import Archiver
from rachis.sdk.result import Artifact
from rachis.sdk.parallel_config import ParallelConfig


def _load_alias_uuid(result):
    archive = Archiver.get_archive(result._archiver.path)
    return archive.load_action_yaml()['action']['alias-of']


def _load_nested_alias_uuid(result, cache):
    alias_uuid = _load_alias_uuid(result)
    aliased_result = rachis.sdk.Result.load(
        os.path.join(cache.data, alias_uuid))
    return _load_alias_uuid(aliased_result)


def _load_alias_uuids(collection):
    uuids = []

    for result in collection.values():
        uuids.append(_load_alias_uuid(result))

    return uuids


def _load_nested_alias_uuids(collection, cache):
    alias_uuids = _load_alias_uuids(collection)

    # load_alias_uuids is expecting a dictionary, so just make a dictionary
    # here so it gets what it wants
    alias_results = {}
    for idx, alias_uuid in enumerate(alias_uuids):
        alias_results[idx] = \
            rachis.sdk.Result.load(os.path.join(cache.data, alias_uuid))

    return _load_alias_uuids(alias_results)


class TestPipelineResumption(unittest.TestCase):
    def setUp(self):
        # Get our pipeline
        self.plugin = get_dummy_plugin()
        self.pipeline = self.plugin.pipelines['resumable_varied_pipeline']
        self.nested_pipeline = \
            self.plugin.pipelines['resumable_nested_varied_pipeline']

        # Create temp test dir
        self.test_dir = tempfile.TemporaryDirectory(prefix='rachis-test-temp-')

        # Create cache and pool
        self.cache = Cache(os.path.join(self.test_dir.name, 'cache'))
        self.pool = self.cache.create_pool('pool')

        # Create artifacts
        self.ints1 = {'1': Artifact.import_data(SingleInt, 0),
                      '2': Artifact.import_data(SingleInt, 1)}
        self.ints1_2 = {'3': Artifact.import_data(SingleInt, 1),
                        '4': Artifact.import_data(SingleInt, 2)}
        self.ints2 = [Artifact.import_data(IntSequence1, [0, 1, 2]),
                      Artifact.import_data(IntSequence1, [3, 4, 5])]
        self.int1 = Artifact.import_data(SingleInt, 42)
        self.int2 = Artifact.import_data(SingleInt, 43)

        # Create metadata
        df1 = pd.DataFrame({'a': ['1', '2', '3']},
                           index=pd.Index(['0', '1', '2'], name='feature ID'))
        self.md1 = rachis.Metadata(df1)
        df2 = pd.DataFrame({'b': ['4', '5', '6']},
                           index=pd.Index(['0', '1', '2'], name='feature ID'))
        self.md2 = rachis.Metadata(df2)

    def tearDown(self):
        """Remove our cache and all that from last test
        """
        self.test_dir.cleanup()

    def test_resumable_pipeline_no_pool(self):
        with self.cache:
            with self.assertRaises(PipelineError) as e:
                self.pipeline(
                    self.ints1, self.ints2, self.md1, self.int1, 'Hi',
                    fail=True)

            ints1_uuids, ints2_uuids, int1_uuid, list_uuids, dict_uuids, \
                identity_uuid, viz_uuid = e.exception.uuids

            ints1_ret, ints2_ret, int1_ret, list_ret, dict_ret, identity_ret, \
                viz_ret = self.pipeline(
                    self.ints1, self.ints2, self.md1, self.int1, 'Hi')

            complete_ints1_uuids = _load_alias_uuids(ints1_ret)
            complete_ints2_uuids = _load_alias_uuids(ints2_ret)
            complete_int1_uuid = _load_alias_uuid(int1_ret)
            complete_list_uuids = _load_alias_uuids(list_ret)
            complete_dict_uuids = _load_alias_uuids(dict_ret)
            complete_identity_uuid = _load_alias_uuid(identity_ret)
            complete_viz_uuid = _load_alias_uuid(viz_ret)

            # Nothing should have been recycled because we didn't use a pool
            self.assertNotEqual(ints1_uuids, complete_ints1_uuids)
            self.assertNotEqual(ints2_uuids, complete_ints2_uuids)
            self.assertNotEqual(int1_uuid, complete_int1_uuid)
            self.assertNotEqual(list_uuids, complete_list_uuids)
            self.assertNotEqual(dict_uuids, complete_dict_uuids)
            self.assertNotEqual(identity_uuid, complete_identity_uuid)
            self.assertNotEqual(viz_uuid, complete_viz_uuid)

    def test_resumable_pipeline(self):
        with self.pool:
            with self.assertRaises(PipelineError) as e:
                self.pipeline(
                    self.ints1, self.ints2, self.md1, self.int1, 'Hi',
                    fail=True)

            ints1_uuids, ints2_uuids, int1_uuid, list_uuids, dict_uuids, \
                identity_uuid, viz_uuid = e.exception.uuids

            ints1_ret, ints2_ret, int1_ret, list_ret, dict_ret, \
                identity_ret, viz_ret = self.pipeline(
                    self.ints1, self.ints2, self.md1, self.int1, 'Hi')

            complete_ints1_uuids = _load_alias_uuids(ints1_ret)
            complete_ints2_uuids = _load_alias_uuids(ints2_ret)
            complete_int1_uuid = _load_alias_uuid(int1_ret)
            complete_list_uuids = _load_alias_uuids(list_ret)
            complete_dict_uuids = _load_alias_uuids(dict_ret)
            complete_identity_uuid = _load_alias_uuid(identity_ret)
            complete_viz_uuid = _load_alias_uuid(viz_ret)

            # Assert that the artifacts returned by the completed run of
            # the pipeline are aliases of the artifacts created by the
            # first failed run
            self.assertEqual(ints1_uuids, complete_ints1_uuids)
            self.assertEqual(ints2_uuids, complete_ints2_uuids)
            self.assertEqual(int1_uuid, complete_int1_uuid)
            self.assertEqual(list_uuids, complete_list_uuids)
            self.assertEqual(dict_uuids, complete_dict_uuids)
            self.assertEqual(identity_uuid, complete_identity_uuid)
            self.assertEqual(viz_uuid, complete_viz_uuid)

    def test_resumable_pipeline_parallel(self):
        with self.pool:
            with self.assertRaises(PipelineError) as e:
                with ParallelConfig():
                    future = self.pipeline.parallel(
                        self.ints1, self.ints2, self.md1, self.int1, 'Hi',
                        fail=True)
                    future._result()

            ints1_uuids, ints2_uuids, int1_uuid, list_uuids, dict_uuids, \
                identity_uuid, viz_uuid = e.exception.uuids

            with ParallelConfig():
                future = self.pipeline.parallel(
                    self.ints1, self.ints2, self.md1, self.int1, 'Hi')
                ints1_ret, ints2_ret, int1_ret, list_ret, dict_ret, \
                    identity_ret, viz_ret = future._result()

            complete_ints1_uuids = _load_alias_uuids(ints1_ret)
            complete_ints2_uuids = _load_alias_uuids(ints2_ret)
            complete_int1_uuid = _load_alias_uuid(int1_ret)
            complete_list_uuids = _load_alias_uuids(list_ret)
            complete_dict_uuids = _load_alias_uuids(dict_ret)
            complete_identity_uuid = _load_alias_uuid(identity_ret)
            complete_viz_uuid = _load_alias_uuid(viz_ret)

            # Assert that the artifacts returned by the completed run of
            # the pipeline are aliases of the artifacts created by the
            # first failed run
            self.assertEqual(ints1_uuids, complete_ints1_uuids)
            self.assertEqual(ints2_uuids, complete_ints2_uuids)
            self.assertEqual(int1_uuid, complete_int1_uuid)
            self.assertEqual(list_uuids, complete_list_uuids)
            self.assertEqual(dict_uuids, complete_dict_uuids)
            self.assertEqual(identity_uuid, complete_identity_uuid)
            self.assertEqual(viz_uuid, complete_viz_uuid)

    def test_resumable_pipeline_artifact_varies(self):
        with self.pool:
            with self.assertRaises(PipelineError) as e:
                self.pipeline(
                    self.ints1, self.ints2, self.md1, self.int1, 'Hi',
                    fail=True)

            ints1_uuids, ints2_uuids, int1_uuid, list_uuids, dict_uuids, \
                identity_uuid, viz_uuid = e.exception.uuids

            # Pass int2 instead of int1
            ints1_ret, ints2_ret, int1_ret, list_ret, dict_ret, \
                identity_ret, viz_ret = self.pipeline(
                    self.ints1, self.ints2, self.md1, self.int2, 'Hi')

            complete_ints1_uuids = _load_alias_uuids(ints1_ret)
            complete_ints2_uuids = _load_alias_uuids(ints2_ret)
            complete_int1_uuid = _load_alias_uuid(int1_ret)
            complete_list_uuids = _load_alias_uuids(list_ret)
            complete_dict_uuids = _load_alias_uuids(dict_ret)
            complete_identity_uuid = _load_alias_uuid(identity_ret)
            complete_viz_uuid = _load_alias_uuid(viz_ret)

            # Assert that the artifacts returned by the completed pipeline that
            # are implicated by the changed input are not aliases while the
            # others are
            self.assertNotEqual(ints1_uuids, complete_ints1_uuids)
            self.assertNotEqual(ints2_uuids, complete_ints2_uuids)
            self.assertNotEqual(int1_uuid, complete_int1_uuid)
            self.assertNotEqual(list_uuids, complete_list_uuids)
            self.assertEqual(dict_uuids, complete_dict_uuids)
            self.assertEqual(identity_uuid, complete_identity_uuid)
            self.assertEqual(viz_uuid, complete_viz_uuid)

    def test_resumable_pipeline_artifact_varies_parallel(self):
        with self.pool:
            with self.assertRaises(PipelineError) as e:
                with ParallelConfig():
                    future = self.pipeline.parallel(
                        self.ints1, self.ints2, self.md1, self.int1, 'Hi',
                        fail=True)
                    future._result()

            ints1_uuids, ints2_uuids, int1_uuid, list_uuids, dict_uuids, \
                identity_uuid, viz_uuid = e.exception.uuids

            # Pass int2 instead of int1
            with ParallelConfig():
                future = self.pipeline.parallel(
                    self.ints1, self.ints2, self.md1, self.int2, 'Hi')
                ints1_ret, ints2_ret, int1_ret, list_ret, dict_ret, \
                    identity_ret, viz_ret = future._result()

            complete_ints1_uuids = _load_alias_uuids(ints1_ret)
            complete_ints2_uuids = _load_alias_uuids(ints2_ret)
            complete_int1_uuid = _load_alias_uuid(int1_ret)
            complete_list_uuids = _load_alias_uuids(list_ret)
            complete_dict_uuids = _load_alias_uuids(dict_ret)
            complete_identity_uuid = _load_alias_uuid(identity_ret)
            complete_viz_uuid = _load_alias_uuid(viz_ret)

            # Assert that the artifacts returned by the completed pipeline that
            # are implicated by the changed input are not aliases while the
            # others are
            self.assertNotEqual(ints1_uuids, complete_ints1_uuids)
            self.assertNotEqual(ints2_uuids, complete_ints2_uuids)
            self.assertNotEqual(int1_uuid, complete_int1_uuid)
            self.assertNotEqual(list_uuids, complete_list_uuids)
            self.assertEqual(dict_uuids, complete_dict_uuids)
            self.assertEqual(identity_uuid, complete_identity_uuid)
            self.assertEqual(viz_uuid, complete_viz_uuid)

    def test_resumable_pipeline_collection_varies(self):
        with self.pool:
            with self.assertRaises(PipelineError) as e:
                self.pipeline(
                    self.ints1, self.ints2, self.md1, self.int1, 'Hi',
                    fail=True)

            ints1_uuids, ints2_uuids, int1_uuid, list_uuids, dict_uuids, \
                identity_uuid, viz_uuid = e.exception.uuids

            # Pass ints1_2 instead of ints1
            ints1_ret, ints2_ret, int1_ret, list_ret, dict_ret, \
                identity_ret, viz_ret = self.pipeline(
                    self.ints1_2, self.ints2, self.md1, self.int1, 'Hi')

            complete_ints1_uuids = _load_alias_uuids(ints1_ret)
            complete_ints2_uuids = _load_alias_uuids(ints2_ret)
            complete_int1_uuid = _load_alias_uuid(int1_ret)
            complete_list_uuids = _load_alias_uuids(list_ret)
            complete_dict_uuids = _load_alias_uuids(dict_ret)
            complete_identity_uuid = _load_alias_uuid(identity_ret)
            complete_viz_uuid = _load_alias_uuid(viz_ret)

            # Assert that the artifacts returned by the completed pipeline that
            # are implicated by the changed input are not aliases while the
            # others are
            self.assertNotEqual(ints1_uuids, complete_ints1_uuids)
            self.assertNotEqual(ints2_uuids, complete_ints2_uuids)
            self.assertNotEqual(int1_uuid, complete_int1_uuid)
            self.assertNotEqual(list_uuids, complete_list_uuids)
            self.assertNotEqual(dict_uuids, complete_dict_uuids)
            self.assertEqual(identity_uuid, complete_identity_uuid)
            self.assertEqual(viz_uuid, complete_viz_uuid)

    def test_resumable_pipeline_collection_varies_parallel(self):
        with self.pool:
            with self.assertRaises(PipelineError) as e:
                with ParallelConfig():
                    future = self.pipeline.parallel(
                        self.ints1, self.ints2, self.md1, self.int1, 'Hi',
                        fail=True)
                    future._result()

            ints1_uuids, ints2_uuids, int1_uuid, list_uuids, dict_uuids, \
                identity_uuid, viz_uuid = e.exception.uuids

            # Pass ints1_2 instead of ints1
            with ParallelConfig():
                future = self.pipeline.parallel(
                    self.ints1_2, self.ints2, self.md1, self.int2, 'Hi')
                ints1_ret, ints2_ret, int1_ret, list_ret, dict_ret, \
                    identity_ret, viz_ret = future._result()

            complete_ints1_uuids = _load_alias_uuids(ints1_ret)
            complete_ints2_uuids = _load_alias_uuids(ints2_ret)
            complete_int1_uuid = _load_alias_uuid(int1_ret)
            complete_list_uuids = _load_alias_uuids(list_ret)
            complete_dict_uuids = _load_alias_uuids(dict_ret)
            complete_identity_uuid = _load_alias_uuid(identity_ret)
            complete_viz_uuid = _load_alias_uuid(viz_ret)

            # Assert that the artifacts returned by the completed pipeline that
            # are implicated by the changed input are not aliases while the
            # others are
            self.assertNotEqual(ints1_uuids, complete_ints1_uuids)
            self.assertNotEqual(ints2_uuids, complete_ints2_uuids)
            self.assertNotEqual(int1_uuid, complete_int1_uuid)
            self.assertNotEqual(list_uuids, complete_list_uuids)
            self.assertNotEqual(dict_uuids, complete_dict_uuids)
            self.assertEqual(identity_uuid, complete_identity_uuid)
            self.assertEqual(viz_uuid, complete_viz_uuid)

    def test_resumable_pipeline_str_varies(self):
        with self.pool:
            with self.assertRaises(PipelineError) as e:
                self.pipeline(
                    self.ints1, self.ints2, self.md1, self.int1, 'Hi',
                    fail=True)

            ints1_uuids, ints2_uuids, int1_uuid, list_uuids, dict_uuids, \
                identity_uuid, viz_uuid = e.exception.uuids

            # Pass in Bye instead of Hi
            ints1_ret, ints2_ret, int1_ret, list_ret, dict_ret, \
                identity_ret, viz_ret = self.pipeline(
                    self.ints1, self.ints2, self.md1, self.int1, 'Bye')

            complete_ints1_uuids = _load_alias_uuids(ints1_ret)
            complete_ints2_uuids = _load_alias_uuids(ints2_ret)
            complete_int1_uuid = _load_alias_uuid(int1_ret)
            complete_list_uuids = _load_alias_uuids(list_ret)
            complete_dict_uuids = _load_alias_uuids(dict_ret)
            complete_identity_uuid = _load_alias_uuid(identity_ret)
            complete_viz_uuid = _load_alias_uuid(viz_ret)

            # Assert that the artifacts returned by the completed pipeline that
            # are implicated by the changed input are not aliases while the
            # others are
            self.assertNotEqual(ints1_uuids, complete_ints1_uuids)
            self.assertNotEqual(ints2_uuids, complete_ints2_uuids)
            self.assertNotEqual(int1_uuid, complete_int1_uuid)
            self.assertNotEqual(list_uuids, complete_list_uuids)
            self.assertEqual(dict_uuids, complete_dict_uuids)
            self.assertEqual(identity_uuid, complete_identity_uuid)
            self.assertEqual(viz_uuid, complete_viz_uuid)

    def test_resumable_pipeline_str_varies_parallel(self):
        with self.pool:
            with self.assertRaises(PipelineError) as e:
                with ParallelConfig():
                    future = self.pipeline.parallel(
                        self.ints1, self.ints2, self.md1, self.int1, 'Hi',
                        fail=True)
                    future._result()

            ints1_uuids, ints2_uuids, int1_uuid, list_uuids, dict_uuids, \
                identity_uuid, viz_uuid = e.exception.uuids

            # Pass in Bye instead of Hi
            with ParallelConfig():
                future = self.pipeline.parallel(
                    self.ints1, self.ints2, self.md1, self.int1, 'Bye')
                ints1_ret, ints2_ret, int1_ret, list_ret, dict_ret, \
                    identity_ret, viz_ret = future._result()

            complete_ints1_uuids = _load_alias_uuids(ints1_ret)
            complete_ints2_uuids = _load_alias_uuids(ints2_ret)
            complete_int1_uuid = _load_alias_uuid(int1_ret)
            complete_list_uuids = _load_alias_uuids(list_ret)
            complete_dict_uuids = _load_alias_uuids(dict_ret)
            complete_identity_uuid = _load_alias_uuid(identity_ret)
            complete_viz_uuid = _load_alias_uuid(viz_ret)

            # Assert that the artifacts returned by the completed pipeline that
            # are implicated by the changed input are not aliases while the
            # others are
            self.assertNotEqual(ints1_uuids, complete_ints1_uuids)
            self.assertNotEqual(ints2_uuids, complete_ints2_uuids)
            self.assertNotEqual(int1_uuid, complete_int1_uuid)
            self.assertNotEqual(list_uuids, complete_list_uuids)
            self.assertEqual(dict_uuids, complete_dict_uuids)
            self.assertEqual(identity_uuid, complete_identity_uuid)
            self.assertEqual(viz_uuid, complete_viz_uuid)

    def test_resumable_pipeline_md_varies(self):
        with self.pool:
            with self.assertRaises(PipelineError) as e:
                self.pipeline(
                    self.ints1, self.ints2, self.md1, self.int1, 'Hi',
                    fail=True)

            ints1_uuids, ints2_uuids, int1_uuid, list_uuids, dict_uuids, \
                identity_uuid, viz_uuid = e.exception.uuids

            # Pass in md2 instead of md1
            ints1_ret, ints2_ret, int1_ret, list_ret, dict_ret, \
                identity_ret, viz_ret = self.pipeline(
                    self.ints1, self.ints2, self.md2, self.int1, 'Hi')

            complete_ints1_uuids = _load_alias_uuids(ints1_ret)
            complete_ints2_uuids = _load_alias_uuids(ints2_ret)
            complete_int1_uuid = _load_alias_uuid(int1_ret)
            complete_list_uuids = _load_alias_uuids(list_ret)
            complete_dict_uuids = _load_alias_uuids(dict_ret)
            complete_identity_uuid = _load_alias_uuid(identity_ret)
            complete_viz_uuid = _load_alias_uuid(viz_ret)

            # Assert that the artifacts returned by the completed pipeline that
            # are implicated by the changed input are not aliases while the
            # others are
            self.assertEqual(ints1_uuids, complete_ints1_uuids)
            self.assertEqual(ints2_uuids, complete_ints2_uuids)
            self.assertEqual(int1_uuid, complete_int1_uuid)
            self.assertEqual(list_uuids, complete_list_uuids)
            self.assertEqual(dict_uuids, complete_dict_uuids)
            self.assertNotEqual(identity_uuid, complete_identity_uuid)
            self.assertEqual(viz_uuid, complete_viz_uuid)

    def test_resumable_pipeline_md_varies_parallel(self):
        with self.pool:
            with self.assertRaises(PipelineError) as e:
                with ParallelConfig():
                    future = self.pipeline.parallel(
                        self.ints1, self.ints2, self.md1, self.int1, 'Hi',
                        fail=True)
                    future._result()

            ints1_uuids, ints2_uuids, int1_uuid, list_uuids, dict_uuids, \
                identity_uuid, viz_uuid = e.exception.uuids

            # Pass in md2 instead of md1
            with ParallelConfig():
                future = self.pipeline.parallel(
                    self.ints1, self.ints2, self.md2, self.int1, 'Hi')
                ints1_ret, ints2_ret, int1_ret, list_ret, dict_ret, \
                    identity_ret, viz_ret = future._result()

            complete_ints1_uuids = _load_alias_uuids(ints1_ret)
            complete_ints2_uuids = _load_alias_uuids(ints2_ret)
            complete_int1_uuid = _load_alias_uuid(int1_ret)
            complete_list_uuids = _load_alias_uuids(list_ret)
            complete_dict_uuids = _load_alias_uuids(dict_ret)
            complete_identity_uuid = _load_alias_uuid(identity_ret)
            complete_viz_uuid = _load_alias_uuid(viz_ret)

            # Assert that the artifacts returned by the completed pipeline that
            # are implicated by the changed input are not aliases while the
            # others are
            self.assertEqual(ints1_uuids, complete_ints1_uuids)
            self.assertEqual(ints2_uuids, complete_ints2_uuids)
            self.assertEqual(int1_uuid, complete_int1_uuid)
            self.assertEqual(list_uuids, complete_list_uuids)
            self.assertEqual(dict_uuids, complete_dict_uuids)
            self.assertNotEqual(identity_uuid, complete_identity_uuid)
            self.assertEqual(viz_uuid, complete_viz_uuid)

    def test_nested_resumable_pipeline(self):
        with self.pool:
            with self.assertRaises(PipelineError) as e:
                self.nested_pipeline(
                    self.ints1, self.ints2, self.md1, self.int1, 'Hi',
                    fail=True)

            ints1_uuids, ints2_uuids, int1_uuid, list_uuids, dict_uuids, \
                identity_uuid, viz_uuid = e.exception.uuids

            # We now run the not nested version. This will be able to reuse the
            # returns from varied_method
            ints1_ret, ints2_ret, int1_ret, list_ret, dict_ret, \
                identity_ret, viz_ret = self.nested_pipeline(
                    self.ints1, self.ints2, self.md1, self.int1, 'Hi')

            complete_ints1_uuids = _load_nested_alias_uuids(
                ints1_ret, self.cache)
            complete_ints2_uuids = _load_nested_alias_uuids(
                ints2_ret, self.cache)
            complete_int1_uuid = _load_nested_alias_uuid(int1_ret, self.cache)
            complete_list_uuids = _load_alias_uuids(list_ret)
            complete_dict_uuids = _load_alias_uuids(dict_ret)
            complete_identity_uuid = _load_alias_uuid(identity_ret)
            complete_viz_uuid = _load_alias_uuid(viz_ret)

            # Assert that the artifacts returned by the completed run of the
            # pipeline are aliases of the artifacts created by the first failed
            # run
            self.assertEqual(ints1_uuids, complete_ints1_uuids)
            self.assertEqual(ints2_uuids, complete_ints2_uuids)
            self.assertEqual(int1_uuid, complete_int1_uuid)
            self.assertEqual(list_uuids, complete_list_uuids)
            self.assertEqual(dict_uuids, complete_dict_uuids)
            self.assertEqual(identity_uuid, complete_identity_uuid)
            self.assertEqual(viz_uuid, complete_viz_uuid)

    def test_nested_resumable_pipeline_parallel(self):
        with self.pool:
            with self.assertRaises(PipelineError) as e:
                with ParallelConfig():
                    future = self.nested_pipeline.parallel(
                        self.ints1, self.ints2, self.md1, self.int1, 'Hi',
                        fail=True)
                    future._result()

            ints1_uuids, ints2_uuids, int1_uuid, list_uuids, dict_uuids, \
                identity_uuid, viz_uuid = e.exception.uuids

            with ParallelConfig():
                future = self.nested_pipeline.parallel(
                        self.ints1, self.ints2, self.md1, self.int1, 'Hi')
                ints1_ret, ints2_ret, int1_ret, list_ret, dict_ret, \
                    identity_ret, viz_ret = future._result()

            complete_ints1_uuids = _load_nested_alias_uuids(
                ints1_ret, self.cache)
            complete_ints2_uuids = _load_nested_alias_uuids(
                ints2_ret, self.cache)
            complete_int1_uuid = _load_nested_alias_uuid(int1_ret, self.cache)
            complete_list_uuids = _load_alias_uuids(list_ret)
            complete_dict_uuids = _load_alias_uuids(dict_ret)
            complete_identity_uuid = _load_alias_uuid(identity_ret)
            complete_viz_uuid = _load_alias_uuid(viz_ret)

            # Assert that the artifacts returned by the completed run of the
            # pipeline are aliases of the artifacts created by the first failed
            # run
            self.assertEqual(ints1_uuids, complete_ints1_uuids)
            self.assertEqual(ints2_uuids, complete_ints2_uuids)
            self.assertEqual(int1_uuid, complete_int1_uuid)
            self.assertEqual(list_uuids, complete_list_uuids)
            self.assertEqual(dict_uuids, complete_dict_uuids)
            self.assertEqual(identity_uuid, complete_identity_uuid)
            self.assertEqual(viz_uuid, complete_viz_uuid)

    def test_resumable_pipeline_default_args(self):
        with self.pool:
            with self.assertRaises(PipelineError) as e:
                self.pipeline(
                    self.ints1, self.ints2, self.md1,
                    fail=True)

            ints1_uuids, ints2_uuids, int1_uuid, list_uuids, dict_uuids, \
                identity_uuid, viz_uuid = e.exception.uuids

            ints1_ret, ints2_ret, int1_ret, list_ret, dict_ret, \
                identity_ret, viz_ret = self.pipeline(
                    self.ints1, self.ints2, self.md1)

            complete_ints1_uuids = _load_alias_uuids(ints1_ret)
            complete_ints2_uuids = _load_alias_uuids(ints2_ret)
            complete_int1_uuid = _load_alias_uuid(int1_ret)
            complete_list_uuids = _load_alias_uuids(list_ret)
            complete_dict_uuids = _load_alias_uuids(dict_ret)
            complete_identity_uuid = _load_alias_uuid(identity_ret)
            complete_viz_uuid = _load_alias_uuid(viz_ret)

            # Assert that the artifacts returned by the completed run of
            # the pipeline are aliases of the artifacts created by the
            # first failed run
            self.assertEqual(ints1_uuids, complete_ints1_uuids)
            self.assertEqual(ints2_uuids, complete_ints2_uuids)
            self.assertEqual(int1_uuid, complete_int1_uuid)
            self.assertEqual(list_uuids, complete_list_uuids)
            self.assertEqual(dict_uuids, complete_dict_uuids)
            self.assertEqual(identity_uuid, complete_identity_uuid)
            self.assertEqual(viz_uuid, complete_viz_uuid)

    def test_resumable_pipeline_default_args_parallel(self):
        with self.pool:
            with self.assertRaises(PipelineError) as e:
                with ParallelConfig():
                    future = self.pipeline.parallel(
                        self.ints1, self.ints2, self.md1,
                        fail=True)
                    future._result()

            ints1_uuids, ints2_uuids, int1_uuid, list_uuids, dict_uuids, \
                identity_uuid, viz_uuid = e.exception.uuids

            with ParallelConfig():
                future = self.pipeline.parallel(
                    self.ints1, self.ints2, self.md1)
                ints1_ret, ints2_ret, int1_ret, list_ret, dict_ret, \
                    identity_ret, viz_ret = future._result()

            complete_ints1_uuids = _load_alias_uuids(ints1_ret)
            complete_ints2_uuids = _load_alias_uuids(ints2_ret)
            complete_int1_uuid = _load_alias_uuid(int1_ret)
            complete_list_uuids = _load_alias_uuids(list_ret)
            complete_dict_uuids = _load_alias_uuids(dict_ret)
            complete_identity_uuid = _load_alias_uuid(identity_ret)
            complete_viz_uuid = _load_alias_uuid(viz_ret)

            # Assert that the artifacts returned by the completed run of
            # the pipeline are aliases of the artifacts created by the
            # first failed run
            self.assertEqual(ints1_uuids, complete_ints1_uuids)
            self.assertEqual(ints2_uuids, complete_ints2_uuids)
            self.assertEqual(int1_uuid, complete_int1_uuid)
            self.assertEqual(list_uuids, complete_list_uuids)
            self.assertEqual(dict_uuids, complete_dict_uuids)
            self.assertEqual(identity_uuid, complete_identity_uuid)
            self.assertEqual(viz_uuid, complete_viz_uuid)

    def test_nested_resumable_pipeline_default_args(self):
        with self.pool:
            with self.assertRaises(PipelineError) as e:
                self.nested_pipeline(
                    self.ints1, self.ints2, self.md1, fail=True)

            ints1_uuids, ints2_uuids, int1_uuid, list_uuids, dict_uuids, \
                identity_uuid, viz_uuid = e.exception.uuids

            # We now run the not nested version. This will be able to reuse the
            # returns from varied_method
            ints1_ret, ints2_ret, int1_ret, list_ret, dict_ret, \
                identity_ret, viz_ret = self.nested_pipeline(
                    self.ints1, self.ints2, self.md1)

            complete_ints1_uuids = _load_nested_alias_uuids(
                ints1_ret, self.cache)
            complete_ints2_uuids = _load_nested_alias_uuids(
                ints2_ret, self.cache)
            complete_int1_uuid = _load_nested_alias_uuid(int1_ret, self.cache)
            complete_list_uuids = _load_alias_uuids(list_ret)
            complete_dict_uuids = _load_alias_uuids(dict_ret)
            complete_identity_uuid = _load_alias_uuid(identity_ret)
            complete_viz_uuid = _load_alias_uuid(viz_ret)

            # Assert that the artifacts returned by the completed run of the
            # pipeline are aliases of the artifacts created by the first failed
            # run
            self.assertEqual(ints1_uuids, complete_ints1_uuids)
            self.assertEqual(ints2_uuids, complete_ints2_uuids)
            self.assertEqual(int1_uuid, complete_int1_uuid)
            self.assertEqual(list_uuids, complete_list_uuids)
            self.assertEqual(dict_uuids, complete_dict_uuids)
            self.assertEqual(identity_uuid, complete_identity_uuid)
            self.assertEqual(viz_uuid, complete_viz_uuid)

    def test_nested_resumable_pipeline_parallel_default_args(self):
        with self.pool:
            with self.assertRaises(PipelineError) as e:
                with ParallelConfig():
                    future = self.nested_pipeline.parallel(
                        self.ints1, self.ints2, self.md1, fail=True)
                    future._result()

            ints1_uuids, ints2_uuids, int1_uuid, list_uuids, dict_uuids, \
                identity_uuid, viz_uuid = e.exception.uuids

            with ParallelConfig():
                future = self.nested_pipeline.parallel(
                        self.ints1, self.ints2, self.md1)
                ints1_ret, ints2_ret, int1_ret, list_ret, dict_ret, \
                    identity_ret, viz_ret = future._result()

            complete_ints1_uuids = _load_nested_alias_uuids(
                ints1_ret, self.cache)
            complete_ints2_uuids = _load_nested_alias_uuids(
                ints2_ret, self.cache)
            complete_int1_uuid = _load_nested_alias_uuid(int1_ret, self.cache)
            complete_list_uuids = _load_alias_uuids(list_ret)
            complete_dict_uuids = _load_alias_uuids(dict_ret)
            complete_identity_uuid = _load_alias_uuid(identity_ret)
            complete_viz_uuid = _load_alias_uuid(viz_ret)

            # Assert that the artifacts returned by the completed run of the
            # pipeline are aliases of the artifacts created by the first failed
            # run
            self.assertEqual(ints1_uuids, complete_ints1_uuids)
            self.assertEqual(ints2_uuids, complete_ints2_uuids)
            self.assertEqual(int1_uuid, complete_int1_uuid)
            self.assertEqual(list_uuids, complete_list_uuids)
            self.assertEqual(dict_uuids, complete_dict_uuids)
            self.assertEqual(identity_uuid, complete_identity_uuid)
            self.assertEqual(viz_uuid, complete_viz_uuid)

    def test_mixing_cached_and_new_artifacts(self):
        """ This was previously causing issues
        """
        self.pipeline = self.plugin.pipelines['mix_arts_and_proxies']

        with self.cache:
            with ParallelConfig():
                with self.assertRaises(ValueError):
                    self.pipeline.parallel(fail=True)._result()

                self.pipeline.parallel()._result()

    def test_incomplete_collection(self):
        pipeline = self.plugin.pipelines['resumable_pipeline']
        int_list = list(self.ints1.values())

        with self.pool:
            with self.assertRaises(PipelineError) as e:
                pipeline(int_list, self.ints1, fail=True)

            int_list_uuids, int_dict_uuids = e.exception.uuids
            # Nuke an element of the list
            os.remove(os.path.join(self.pool.path, int_list_uuids[0]))

            # We now expect to get this warning and recreate the list because
            # it is incomplete
            with self.assertWarnsRegex(Warning, 'Incomplete collection'):
                int_list_ret, int_dict_ret = pipeline(int_list, self.ints1)

            complete_int_list_uuids = _load_alias_uuids(int_list_ret)
            complete_int_dict_uuids = _load_alias_uuids(int_dict_ret)

            # Assert that the list uuids are not equal because we recreated it
            self.assertNotEqual(int_list_uuids, complete_int_list_uuids)
            # The dict should have been recycled
            self.assertEqual(int_dict_uuids, complete_int_dict_uuids)

    def test_capture_holder_value_passed(self):
        '''
        Assert that when passing the a value into a CaptureHolder we do recycle
        the cached Result due to the value of the seed matching.
        '''
        pipeline = self.plugin.pipelines['resumable_random_seed_pipeline']

        with self.pool:
            with self.assertRaises(PipelineError) as e:
                pipeline(fail=True, random_seed=42)

            random_int_uuid_failed, = e.exception.uuids

            random_int, = pipeline(random_seed=42)
            random_int_uuid_succeeded = _load_alias_uuid(random_int)

            self.assertEqual(
                random_int_uuid_failed, random_int_uuid_succeeded
            )

    def test_capture_holder_no_value_passed(self):
        '''
        Assert that when passing the auto value into a CaptureHolder we do not
        recycle the cached Result due to the value of the seed not matching.

        Note
        ----
        This test has a 1/sys.maxsize chance of failing due to the
        CaptureHolder in resumable_random_seed_pipeline getting the same random
        value in both calls.
        '''
        pipeline = self.plugin.pipelines['resumable_random_seed_pipeline']

        with self.pool:
            with self.assertRaises(PipelineError) as e:
                pipeline(fail=True)

            random_int_uuid_failed, = e.exception.uuids

            random_int, = pipeline()
            random_int_uuid_succeeded = _load_alias_uuid(random_int)

            self.assertNotEqual(
                random_int_uuid_failed, random_int_uuid_succeeded
            )
