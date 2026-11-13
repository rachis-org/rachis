# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from collections.abc import Iterable

import pytest

from rachis import Artifact, Coord, ForEach
from rachis.core.testing.format import MappingDirectoryFormat
from rachis.core.testing.mapped_axis import SampleMappingAxis
from rachis.core.testing.type import Mapping
from rachis.core.testing.util import get_dummy_plugin


def test_registered_axis_and_coordinate_views():
    plugin = get_dummy_plugin()
    data = Artifact.import_data('Mapping', {'s1': '4', 's2': '-5'})
    assert plugin.axes[data.type, ForEach['sample']] is SampleMappingAxis
    assert data.axes() == [('sample',)]
    assert list(data.coords('sample')) == [Coord(sample='s1'),
                                            Coord(sample='s2')]
    assert data.view(str, sample='s2', mag='ignored') == '-5'


def test_nested_coordinate_views():
    data = Artifact.import_data('AssignmentScores', {'s1/a1': '4'})
    assert data.view(dict, student='s1') == {'a1': '4'}
    assert data.view(str, student='s1', assignment='a1') == '4'


def test_for_each_bindings():
    pattern = ForEach['student', 'assignment']
    assert pattern.coverage == ('matched', 'matched')
    assert pattern.bindings[0] == ForEach['student'].bindings[0]
    matched = ForEach['student'::'matched']
    assert matched.coverage == ('matched',)
    assert matched.declares_coverage
    assert matched != matched.as_unbound()
    binding = ForEach.variable()
    explicit = ForEach['student':binding]
    assert explicit.bindings[0] is binding
    assert explicit != ForEach['student':ForEach.variable()]
    assert explicit.as_unbound() == ForEach['student']
    with pytest.raises(ValueError, match='unique variables'):
        ForEach['student':binding, 'assignment':binding]
    with pytest.raises(TypeError, match='Invalid axis name'):
        ForEach['_items']


def test_axis_registration_rejects_inconsistent_cell_annotations():
    class BadAxis(SampleMappingAxis):
        @classmethod
        def write(
            cls, chunks: Iterable[tuple[Coord, int]],
        ) -> MappingDirectoryFormat:
            raise AssertionError('Registration should only inspect write')

    with pytest.raises(TypeError, match=r'write.*\(Coord, str\)'):
        get_dummy_plugin().register_axes(Mapping, {ForEach['other']: BadAxis})
