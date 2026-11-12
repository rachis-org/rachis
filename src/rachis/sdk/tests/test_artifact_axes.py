# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from rachis import Artifact, Coord, ForEach
from rachis.core.testing.util import get_dummy_plugin


def test_registered_axis_and_coordinate_views():
    plugin = get_dummy_plugin()
    data = Artifact.import_data('Mapping', {'s1': '4', 's2': '-5'})
    assert plugin.axes[data.type, ForEach['sample']].cell_type is str
    assert data.axes() == [('sample',)]
    assert list(data.coords('sample')) == [Coord(sample='s1'),
                                            Coord(sample='s2')]
    assert data.view(str, sample='s2', mag='ignored') == '-5'
