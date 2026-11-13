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
    assert data.view(str, sample='s2') == '-5'
    with pytest.raises(ValueError, match='has no axis'):
        data.view(str, sample='s2', mag='extra')


def test_nested_coordinate_views():
    data = Artifact.import_data('AssignmentScores', {'s1/a1': '4'})
    assert data.view(dict, student='s1') == {'a1': '4'}
    assert data.view(str, student='s1', assignment='a1') == '4'
    with pytest.raises(ValueError, match='has no axis'):
        data.view(str, assignment='a1', student='s1')


def test_for_each_bindings():
    pattern = ForEach['student', 'assignment']
    bare = ForEach['student']
    matched = ForEach['student'::]
    expanded = ForEach['student':'student':'matched']
    assert repr(bare) == "ForEach['student']"
    assert repr(matched) == "ForEach['student':'student':'matched']"
    assert bare.bindings == matched.bindings == ('student',)
    assert bare.coverage == matched.coverage == ('matched',)
    assert bare.normalize() == ForEach['student':] == matched == expanded
    assert matched.as_unbound() == bare
    lookup = {bare: 'axis', expanded: 'pattern'}
    assert lookup[bare] == 'axis'
    assert lookup[matched.normalize()] == 'pattern'
    explicit = ForEach['student':'learner']
    assert explicit.bindings == ('learner',)
    assert explicit.bindings == ForEach['sample':'learner'].bindings
    assert explicit != ForEach['student':'other']
    assert explicit.as_unbound() == ForEach['student']
    mixed = ForEach['student':'learner', 'assignment']
    assert repr(mixed) == (
        "ForEach['student':'learner':'matched', 'assignment']"
    )
    normalized = mixed.normalize()
    assert normalized.entries == (slice('student', 'learner', 'matched'),
                                  slice('assignment', 'assignment', 'matched'))
    assert normalized.normalize() == normalized
    assert normalized.as_unbound() == pattern
    with pytest.raises(ValueError, match='unique bindings'):
        ForEach['student':'assignment', 'assignment'].normalize()
    with pytest.raises(TypeError, match='bindings must be strings'):
        ForEach['student':1]
    with pytest.raises(TypeError, match='Invalid axis name'):
        ForEach['_items']


@pytest.mark.parametrize('pattern', [
    ForEach['sample'::],
    ForEach['sample':'other'],
])
def test_axis_registration_rejects_bound_keys(pattern):
    with pytest.raises(TypeError, match='requires plain axis names'):
        get_dummy_plugin().register_axes(Mapping, {pattern: SampleMappingAxis})


def test_axis_registration_rejects_inconsistent_cell_annotations():
    class BadAxis(SampleMappingAxis):
        @classmethod
        def write(
            cls, chunks: Iterable[tuple[Coord, int]],
        ) -> MappingDirectoryFormat:
            raise AssertionError('Registration should only inspect write')

    with pytest.raises(TypeError, match=r'write.*\(Coord, str\)'):
        get_dummy_plugin().register_axes(Mapping, {ForEach['other']: BadAxis})
