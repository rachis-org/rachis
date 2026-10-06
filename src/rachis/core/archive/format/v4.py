# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import rachis.core.archive.format.v3 as v3

from rachis.core.cite import Citations


class ArchiveFormat(v3.ArchiveFormat):
    # - Adds a transformers section to action.yaml
    # - Adds citations via the !cite yaml type which references the
    #   /provenance/citations.bib file (this is nested like everything else
    #                                   in the /provenance/artifacts/
    #                                   directories).
    # - environment:framework has been updated to be a nested object,
    #   its schema is identical to a environment:plugins:<entry> object.
    #   Prior to v4, it was only a version string.

    @property
    def citations(self):
        files = []
        files.append(self.provenance_dir / 'citations.bib')

        if (self.provenance_dir / 'artifacts').exists():
            for ancestor in (self.provenance_dir / 'artifacts').iterdir():
                if (ancestor / 'citations.bib').exists():
                    files.append(ancestor / 'citations.bib')

        citations = Citations()
        for f in files:
            citations.update(Citations.load(f))

        return citations
