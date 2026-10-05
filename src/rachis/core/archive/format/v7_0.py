# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import os
import pathlib

from rachis.core.archive.provenance import NoOpProvenanceCapture

import rachis.core.archive.format.v1 as v1
import rachis.core.archive.format.v6 as v6


class ArchiveFormat(v6.ArchiveFormat):
    """QIIME 2 Archive Format Version 7.0

    Versioning Updates
    ------------------
    Semantic Versioning
        Starting with 7.0, version updates now allow for major vs. minor
        version bumps.

    New Features
    ------------
    `annotations`
        This new directory (when present) lives under the root and contains
        Annotations that can be added either via the Python API or the cli.

        Supported Annotation sub-types in 7.0:

            Note
                Can contain inline text or the contents of a file.

        `annotations` directory structure (containing example Notes):

            annotations/
            ├── uuid1/
            │   ├── metadata.yaml
            │   ├── note.txt
            │   ├── checksums.sha512
            ├── uuid2/
            │   ├── metadata.yaml
            │   ├── note.txt
            │   ├── checksums.sha512

        With each uuid representing an individual Annotation object that's been
        attached to the Result object.

        The `metadata.yaml` file within each Annotation sub-directory contains
        the following details for a given Annotation:

            `id`
                The minted uuid4 ID associated with the Annotation.

            `name`
                User-provided name for the Annotation.
                Must be unique per Result.

            `type`
                The type of Annotation. Notes are currently the only supported
                Annotation type in 7.0.

            `created_at`
                datetime an Annotation was created.

            `root_result_uuid`
                The result uuid that the Annotation is attached to.

            `referenced_result_uuid`
                The result uuid that the Annotation is in reference to.
                Self-referential Annotations are currently the only supported
                Annotation type in 7.0.

    New Files
    ---------
    `conda-env.yaml`
        This file lives under `provenance` and contains the dependencies
        present within a user's active conda environment.

    New Fields
    ----------
    `data-size`
        This field has been added within the top-level `metadata.yaml` file
        of a Result. It represents the total file size of all files under
        the `data` directory.

    """
    CONDA_ENV_FILE = 'conda-env.yaml'
    ANNOTATIONS_DIR = 'annotations'
    CHECKSUM_FILE = 'checksums.sha512'
    CHECKSUM_TYPE = CHECKSUM_FILE.split('.')[1]

    # file size converters
    @staticmethod
    def _calculate_directory_size(directory: pathlib.Path) -> int:
        # Recursively calculates total file size in bytes under a directory.
        return sum(
            path.stat().st_size for path in directory.rglob('*')
            if path.is_file()
        )

    @staticmethod
    def _human_readable_size(num, suffix="B"):
        for unit in ["", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"]:
            if abs(num) < 1024.0:
                return f"{num:3.1f} {unit}{suffix}"
            num /= 1024.0
        return f"{num:.1f} Yi{suffix}"

    @classmethod
    def write(cls, archive_record, type, format,
              data_initializer, provenance_capture):
        # Pulling from the most recent write version that doesn't include
        # checksums - this ensures that checksums are only calculated
        # after all requisite files are present.
        v1.ArchiveFormat.write(archive_record, type, format,
                               data_initializer, provenance_capture)

        if not isinstance(provenance_capture, NoOpProvenanceCapture):
            # Write `conda-env.yaml` file
            conda_fp = \
                archive_record.root / cls.PROVENANCE_DIR / cls.CONDA_ENV_FILE
            conda_prefix = os.environ.get('CONDA_PREFIX')

            if conda_prefix:
                conda_meta_dir = pathlib.Path(conda_prefix) / 'conda-meta'

                if conda_meta_dir.exists():
                    dependency_list = \
                        [file.stem for file in conda_meta_dir.iterdir()
                        if file.is_file()]

                    with conda_fp.open(mode='w') as fh:
                        fh.write('dependencies:\n')
                        for unformatted_dep in sorted(dependency_list):
                            dep = '='.join(unformatted_dep.rsplit('-', 2))
                            fh.write(f'- {dep}\n')
            else:
                with conda_fp.open(mode='w') as fh:
                    fh.write('error: no conda environment detected.\n')

            # Add `data-size` field under top-level `metadata.yaml` file
            data_fp = archive_record.root / cls.DATA_DIR
            total_size = cls._calculate_directory_size(data_fp)
            datadir_size = cls._human_readable_size(total_size)

            root_md_fp = archive_record.root / cls.METADATA_FILE
            prov_md_fp = \
                archive_record.root / cls.PROVENANCE_DIR / cls.METADATA_FILE

            for md_fp in [root_md_fp, prov_md_fp]:
                with md_fp.open(mode='a') as fh:
                    fh.write(f'data-size: {datadir_size}')

        # now move temporary annotations dir from within provenance
        # so that we prevent duplication of each result's annotations
        temp_annotations_dir = \
            (archive_record.root / cls.PROVENANCE_DIR /
             provenance_capture.TEMP_ANNOTATIONS_DIR)
        if temp_annotations_dir.exists():
            os.rename(temp_annotations_dir,
                      archive_record.root / cls.ANNOTATIONS_DIR)

        # Write checksums last
        cls.write_checksums(archive_record)

    def __init__(self, archive_record, *args, replay=False):
        super().__init__(archive_record, *args, replay=replay)

        self.annotations_dir = \
            archive_record.root / self.ANNOTATIONS_DIR
