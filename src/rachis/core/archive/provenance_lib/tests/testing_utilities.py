# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------
import os
import pathlib
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import dataclass
import warnings
from zipfile import ZipFile, ZIP_DEFLATED

from ..parse import ProvDAG

import rachis
from rachis import Artifact, Metadata, ResultCollection
from rachis.core.archive import Archiver
from rachis.sdk.plugin_manager import PluginManager


@dataclass
class DummyArtifact:
    name: str
    artifact: Artifact
    archiver: Archiver
    uuid: str
    filepath: str
    dag: ProvDAG
    archive_version: int = 6


class DummyArtifacts:
    def __init__(self):
        self.pm = PluginManager()
        self.dp = self.pm.plugins['dummy-plugin']
        self.tempdir = tempfile.mkdtemp(prefix='rachis-dummy-artifacts-temp-')

        self.datadir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'data'
        )

        from rachis.core.cache import CacheV1
        from rachis.core.cache import _CACHE
        previous = getattr(_CACHE, 'cache', None)
        _CACHE.cache = CacheV1(os.path.join(self.tempdir, 'legacy-cache'))
        try:
            self.init_import_artifacts()
            self.init_action_artifacts()
            self.init_all_version_artifacts()
            self.init_artifact_with_md_in_provenance()
            self.init_fake_action_artifact()
            self.init_no_checksum_dag()
        finally:
            _CACHE.cache = previous

    def init_import_artifacts(self):
        '''
        artifacts with only import in their provenance
        '''
        single_int = Artifact.import_data('SingleInt', 0)
        single_int2 = Artifact.import_data('SingleInt', 7)
        single_int_no_checksum = Artifact.import_data('SingleInt', 0)
        int_seq1 = Artifact.import_data('IntSequence1', [1, 1, 2])
        int_seq2 = Artifact.import_data('IntSequence2', [3, 5])
        mapping1 = Artifact.import_data('Mapping', {'a': 42})
        mapping2 = Artifact.import_data('Mapping', {'c': 8, 'd': 13})

        for name in (
            'single_int', 'single_int2', 'single_int_no_checksum', 'int_seq1',
            'int_seq2', 'mapping1', 'mapping2'
        ):
            artifact = locals()[name]
            fp = os.path.join(self.tempdir, f'{name}.qza')
            artifact.save(fp)
            test_artifact = DummyArtifact(
                name, artifact, artifact._archiver, str(artifact.uuid), fp,
                ProvDAG(fp)
            )
            # This dir is empty on imported artifacts and won't actually be
            # here for real .qzas of imported artifacts. Caused us a bit of a
            # headache https://github.com/rachis-org/rachis/pull/943
            empty_ancestors = artifact._archiver.provenance_dir / 'artifacts'
            if empty_ancestors.exists():
                os.rmdir(empty_ancestors)
            setattr(self, name, test_artifact)

    def init_action_artifacts(self):
        '''
        artifacts that have at least one non-import action in their provenance
        '''
        concat_ints = self.dp.methods['concatenate_ints']
        split_ints = self.dp.methods['split_ints']
        merge_mappings = self.dp.methods['merge_mappings']
        identity_with_metadata = self.dp.actions['identity_with_metadata']
        identity_with_metadata_column = \
            self.dp.actions['identity_with_metadata_column']
        dict_of_ints = self.dp.actions['dict_of_ints']
        optional_artifacts_method = \
            self.dp.actions['optional_artifacts_method']

        concated_ints, = concat_ints(
            self.int_seq1.artifact, self.int_seq1.artifact,
            self.int_seq2.artifact, 7, 13
        )
        other_concated_ints, = concat_ints(
            self.int_seq1.artifact, self.int_seq1.artifact,
            self.int_seq2.artifact, 81, 64
        )
        splitted_ints, _ = split_ints(self.int_seq2.artifact)
        merged_mappings, = merge_mappings(
            self.mapping1.artifact, self.mapping2.artifact
        )

        # artifacts with input artifact viewed as metadata
        int_seq_with_md, = identity_with_metadata(
            self.int_seq1.artifact, self.mapping1.artifact.view(Metadata)
        )
        int_seq_with_md_column, = identity_with_metadata_column(
            self.int_seq1.artifact,
            self.mapping2.artifact.view(Metadata).get_column('c')
        )
        concated_ints_with_md_column, = concat_ints(
            self.int_seq1.artifact, int_seq_with_md_column,
            self.int_seq2.artifact, 69, 2001
        )

        # artifact with input collection
        ints_dict = {
            'int1': self.single_int.artifact,
            'int2': self.single_int2.artifact,
        }
        ints_collection = ResultCollection(ints_dict)
        ints_from_collection, = dict_of_ints(ints_collection)
        int_from_collection = ints_from_collection['int1']

        # artifact with optional inputs left to default None
        int_seq_optional_input, = optional_artifacts_method(
            self.int_seq1.artifact, 8
        )

        # artifact from pipeline
        typical_pipeline = self.dp.pipelines['typical_pipeline']
        _, _, _, pipeline_viz, _ = typical_pipeline(
            self.int_seq1.artifact, self.mapping1.artifact, False
        )

        for name in (
            'concated_ints', 'other_concated_ints', 'splitted_ints',
            'merged_mappings', 'pipeline_viz', 'int_seq_with_md',
            'concated_ints_with_md_column', 'int_from_collection',
            'int_seq_optional_input'
        ):
            artifact = locals()[name]
            if name == 'pipeline_viz':
                ext = '.qzv'
            else:
                ext = '.qza'

            fp = os.path.join(self.tempdir, f'{name}{ext}')
            artifact.save(fp)
            test_artifact = DummyArtifact(
                name, artifact, artifact._archiver, str(artifact.uuid), fp,
                ProvDAG(fp)
            )
            setattr(self, name, test_artifact)

    def init_all_version_artifacts(self):
        '''
        import artifacts for all archive versions (0-6), which are stored
        uncompressed in the test data directory--necessary because we can not
        make artifacts of non-current versions on the fly
        '''
        for version in range(0, 7):
            if version == 0:
                dirname = 'table-v0'
            else:
                dirname = f'concated-ints-v{version}'

            versioned_artifact_dir = os.path.join(self.datadir, dirname)
            temp_zf_path = os.path.join(self.tempdir, 'temp.zip')
            write_zip_file(temp_zf_path, versioned_artifact_dir)

            filename = f'{dirname}.qza'
            fp = os.path.join(self.tempdir, filename)

            if version == 0:
                shutil.copy(temp_zf_path, fp)
                archiver = Archiver.load(temp_zf_path, replay=True)
                artifact = None
            else:
                artifact = Artifact.load(temp_zf_path)
                archiver = artifact._archiver
                artifact.save(fp)

            with warnings.catch_warnings():
                warnings.filterwarnings('ignore', category=UserWarning)
                dag = ProvDAG(fp)

            assert len(dag.terminal_nodes) == 1
            terminal_node, *_ = dag.terminal_nodes
            uuid = terminal_node._uuid

            name = filename.replace('-', '_').replace('.qza', '')
            da = DummyArtifact(
                name, artifact, archiver, uuid, fp, dag, version
            )
            setattr(self, name, da)

    def init_artifact_with_md_in_provenance(self):
        dirname = 'concated-ints-with-md'
        artifact_dir = os.path.join(self.datadir, dirname)
        temp_zf_path = os.path.join(self.tempdir, 'temp.zip')
        write_zip_file(temp_zf_path, artifact_dir)
        filename = f'{dirname}.qza'
        fp = os.path.join(self.tempdir, filename)
        a = Artifact.load(temp_zf_path)
        a.save(fp)

        dag = ProvDAG(fp)
        terminal_node, *_ = dag.terminal_nodes
        uuid = terminal_node._uuid
        name = filename.replace('-', '_').replace('.qza', '')
        da = DummyArtifact(name, a, a._archiver, uuid, fp, dag, 6)
        setattr(self, name, da)

    def init_fake_action_artifact(self):
        dirname = 'fake-action'
        artifact_dir = os.path.join(self.datadir, dirname)
        temp_zf_path = os.path.join(self.tempdir, 'temp.zip')
        write_zip_file(temp_zf_path, artifact_dir)
        filename = f'{dirname}.qza'
        fp = os.path.join(self.tempdir, filename)
        a = Artifact.load(temp_zf_path)
        a.save(fp)

        dag = ProvDAG(fp)
        terminal_node, *_ = dag.terminal_nodes
        uuid = terminal_node._uuid
        name = filename.replace('-', '_').replace('.qza', '')
        da = DummyArtifact(name, a, a._archiver, uuid, fp, dag, 6)
        setattr(self, name, da)

    def init_no_checksum_dag(self):
        '''
        create archive with missing checksums.sha512
        '''
        with warnings.catch_warnings():
            warnings.filterwarnings(
                'ignore',
                message='.*checksums.sha512 file is missing.*',
                category=UserWarning
            )
            os.remove(
                self.single_int_no_checksum.artifact._archiver.path /
                'checksums.sha512')
            self.dag_missing_sha512 = ProvDAG(
                self.single_int_no_checksum.artifact
            )

    @property
    def all_artifact_versions(self):
        return (
            self.table_v0, self.concated_ints_v1,
            self.concated_ints_v2, self.concated_ints_v3,
            self.concated_ints_v4, self.concated_ints_v5, self.concated_ints_v6
        )

    def free(self):
        shutil.rmtree(self.tempdir)


def write_zip_file(zfp, unzipped_dir):
    zf = ZipFile(zfp, 'w', ZIP_DEFLATED)
    for root, dirs, files in os.walk(unzipped_dir):
        for file in files:
            filepath = os.path.join(root, file)
            zf.write(
                filepath, os.path.relpath(filepath, unzipped_dir)
            )
    zf.close()


class CustomAssertions(unittest.TestCase):
    def assertREAppearsOnlyOnce(self, text, only_once, msg=None):
        appears_once_re = \
            (f'(?s)^(?:(?!{only_once}).)*{only_once}(?!.*{only_once}).*$')
        self.assertRegex(text, appears_once_re, msg)


def is_root_provnode_data(fp):
    '''
    a filter predicate which returns metadata, action, citation,
    and VERSION fps with which we can construct a ProvNode
    '''
    # Handle provenance files...
    if 'provenance' in fp and 'artifacts' not in fp:
        if 'action.yaml' in fp or 'citations.bib' in fp:
            return True

    # then handle files available at root, which require a cast
    if pathlib.Path(fp).parts[1] in (
        'VERSION', 'metadata.yaml', 'checksums.sha512'
    ):
        return True


@contextmanager
def monkeypatch_archive_version(patch_version):
    try:
        og_version = Archiver.CURRENT_FORMAT_VERSION
        Archiver.CURRENT_FORMAT_VERSION = patch_version
        yield
    finally:
        Archiver.CURRENT_FORMAT_VERSION = og_version


@contextmanager
def monkeypatch_framework_version(patch_version):
    try:
        og_version = rachis.__version__
        rachis.__version__ = patch_version
        yield
    finally:
        rachis.__version__ = og_version


def write_zip_archive(zfp, unzipped_dir):
    with ZipFile(zfp, 'w') as zf:
        for root, dirs, files in os.walk(unzipped_dir):
            for file in files:
                path = os.path.join(root, file)
                archive_name = os.path.relpath(path, start=unzipped_dir)
                zf.write(path, arcname=archive_name)
