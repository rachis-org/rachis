# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import rachis.core.archive.format.v0 as v0

from rachis.core.archive.provenance import NoOpProvenanceCapture


class ArchiveFormat(v0.ArchiveFormat):
    PROVENANCE_DIR = 'provenance'

    @classmethod
    def write(cls, archive_record, type, format,
              data_initializer, provenance_capture):
        # contents of data dir written first
        super().write(archive_record, type, format,
                      data_initializer, provenance_capture)

        root = archive_record.root

        # now we write the contents of provenance
        prov_dir = root / cls.PROVENANCE_DIR
        if not isinstance(provenance_capture, NoOpProvenanceCapture):
            prov_dir.mkdir()

            provenance_capture.finalize(
                prov_dir, [root / cls.METADATA_FILE, archive_record.version_fp]
            )

    def __init__(self, archive_record, *args, replay=False):
        super().__init__(archive_record, *args, replay=replay)

        self.provenance_dir = archive_record.root / self.PROVENANCE_DIR
