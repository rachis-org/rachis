# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from collections.abc import Iterable
from itertools import islice
from unittest.mock import patch

import pytest

from rachis import Artifact, Coord, ForEach
from rachis.core.archive import Archiver, ImportProvenanceCapture
from rachis.core.axis import CoordinateJoin, coordinate_joins
from rachis.core.testing.format import MappingDirectoryFormat, OldMappingDirFmt
from rachis.core.testing.mapped_axis import SampleMappingAxis
from rachis.core.testing.type import Mapping
from rachis.core.testing.util import get_dummy_plugin
from rachis.plugin import Plugin
from rachis.sdk import PluginManager


def test_coord_intersection():
    sample = Coord(sample='s1')
    feature = Coord(feature='f1')
    combined = Coord(sample='s1', feature='f1')
    assert (sample & feature) == combined
    assert (feature & sample) == Coord(feature='f1', sample='s1')
    assert (Coord() & sample) == sample
    assert (sample & combined) == combined
    assert (combined & sample) == combined
    with pytest.raises(ValueError, match="axis 'sample'"):
        sample & Coord(sample='s2')


def test_for_each_bindings():
    pattern = ForEach['student', 'assignment']
    bare = ForEach['student']
    matched = ForEach['student'::]
    expanded = ForEach['student':'student':'matched']
    assert repr(bare) == "ForEach['student']"
    assert repr(matched) == "ForEach['student':'student':'matched']"
    assert bare.bindings == matched.bindings == ('student',)
    assert bare.coverage == matched.coverage == ('matched',)
    assert bare.coverage_rule == matched.coverage_rule == 'matched'
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
        "ForEach['student':'learner':'matched', "
        "'assignment':'assignment':'matched']"
    )
    normalized = mixed.normalize()
    assert normalized.to_bound_coord(Coord(student='s1', assignment='a1')) == (
        Coord(learner='s1', assignment='a1')
    )
    assert normalized.to_axis_coord(
        Coord(assignment='a1', learner='s1', extra='ignored'),
    ) == Coord(student='s1', assignment='a1')
    assert mixed == normalized
    assert normalized.normalize() == normalized
    assert normalized.as_unbound() == pattern
    with pytest.raises(ValueError, match='unique bindings'):
        ForEach['student':'assignment', 'assignment'].normalize()
    with pytest.raises(TypeError, match='bindings must be strings'):
        ForEach['student':1]
    with pytest.raises(TypeError, match='Invalid axis name'):
        ForEach['_items']


def test_for_each_requires_uniform_coverage():
    pattern = ForEach['student'::'optional', 'assignment'::'optional']
    assert pattern.coverage_rule == 'optional'
    assert pattern.normalize().coverage_rule == 'optional'
    with pytest.raises(TypeError, match='uniform coverage'):
        ForEach['student', 'assignment'::'optional']


@pytest.mark.parametrize(('rules', 'keys', 'gaps'), [
    (('matched', 'matched', 'matched'), 'c', {}),
    (('matched', 'matched', 'required'), 'bc', {'b': ('C',)}),
    (('matched', 'matched', 'optional'), 'bc', {}),
    (('matched', 'required', 'required'), 'abc',
     {'a': ('B', 'C'), 'b': ('C',)}),
    (('matched', 'required', 'optional'), 'abc', {'a': ('B',)}),
    (('matched', 'optional', 'optional'), 'abc', {}),
    (('required', 'required', 'required'), 'abcde',
     {'a': ('B', 'C'), 'b': ('C',), 'd': ('A',), 'e': ('A', 'B')}),
    (('required', 'required', 'optional'), 'cde',
     {'d': ('A',), 'e': ('A', 'B')}),
    (('required', 'optional', 'optional'), 'bcde',
     {'d': ('A',), 'e': ('A',)}),
    (('optional', 'optional', 'optional'), 'abcde', {}),
])
def test_three_way_coverage(rules, keys, gaps):
    domains = {'A': 'abc', 'B': 'bcd', 'C': 'cde'}
    join = CoordinateJoin({
        name: ForEach['sample'::rule]
        for name, rule in zip(domains, rules, strict=True)
    })
    indexes = {
        name: {Coord(sample=key): f'{name}/{key}' for key in domain}
        for name, domain in domains.items()
    }
    assert {
        key: missing for key in 'abcde'
        if (missing := join.coverage({name for name, domain in domains.items()
                                      if key in domain}))
    } == gaps
    stop = keys.index(next(iter(gaps))) if gaps else len(keys)
    aligned = join.align(indexes)
    assert list(islice(aligned, stop)) == [
        (Coord(sample=key),
         {name: f'{name}/{key}' if key in domain else None
          for name, domain in domains.items()})
        for key in keys[:stop]
    ]
    if gaps:
        with pytest.raises(ValueError, match='Required inputs.*Coord'):
            next(aligned)
    else:
        assert list(aligned) == []


@pytest.mark.parametrize('rule', ['matched', 'optional', 'required'])
def test_projected_coverage(rule):
    join = CoordinateJoin({
        'offset': ForEach['sample':'student':rule],
        'scores': ForEach['student'::'required', 'assignment'::'required'],
    })
    indexes = {
        'offset': {Coord(student='s1'): 0, Coord(student='s3'): 10},
        'scores': {Coord(student='s1', assignment='a1'): 4,
                   Coord(student='s2', assignment='a1'): 5},
    }
    aligned = join.align(indexes)
    assert next(aligned) == (Coord(student='s1', assignment='a1'),
                             {'offset': 0, 'scores': 4})
    if rule == 'required':
        with pytest.raises(ValueError, match="Required inputs.*offset.*s2"):
            next(aligned)
    elif rule == 'optional':
        assert list(aligned) == [(Coord(student='s2', assignment='a1'),
                                  {'offset': None, 'scores': 5})]
    else:
        assert list(aligned) == []


def test_projected_match_filters_the_complete_optional_domain():
    join = CoordinateJoin({
        'offset': ForEach['student'],
        'scores': ForEach['student'::'optional', 'assignment'::'optional'],
        'weights': ForEach['student'::'required', 'assignment'::'required'],
    })
    indexes = {
        'offset': {Coord(student='s1'): 10},
        'scores': {Coord(student='s1', assignment='a1'): 4,
                   Coord(student='s2', assignment='a1'): 5},
        'weights': {Coord(student='s1', assignment='a1'): 1,
                    Coord(student='s1', assignment='a2'): 1},
    }
    assert list(join.align(indexes)) == [
        (Coord(student='s1', assignment='a1'),
         {'offset': 10, 'scores': 4, 'weights': 1}),
    ]


def test_presence_inputs_include_domain_drivers_and_coverage_checks():
    join = CoordinateJoin({
        'scores': ForEach['student'::'optional', 'assignment'::'optional'],
        'offset': ForEach['student'::'optional'],
        'weight': ForEach['assignment'::'required'],
        'roster': ForEach['student'],
    })
    assert join.presence_inputs == {'scores', 'weight', 'roster'}


@pytest.mark.parametrize(('rule', 'left', 'right'), [
    ('matched', {Coord(sample='s1'): 1}, {Coord(sample='s2'): 2}),
    ('optional', {}, {}),
])
def test_empty_coverage_domain(rule, left, right):
    join = CoordinateJoin(dict.fromkeys(('left', 'right'),
                                       ForEach['sample'::rule]))
    assert list(join.align({'left': left, 'right': right})) == []


def test_required_keys_align_by_binding_order():
    join = CoordinateJoin({
        'left': ForEach['student'::'required', 'assignment'::'required'],
        'right': ForEach[
            'assignment'::'required', 'sample':'student':'required',
        ],
    })
    indexes = {
        'left': {Coord(student='s1', assignment='a1'): 1,
                 Coord(student='s1', assignment='a2'): 2},
        'right': {Coord(assignment='a2', student='s1'): 3,
                  Coord(assignment='a1', student='s1'): 4},
    }
    assert list(join.align(indexes)) == [
        (Coord(student='s1', assignment='a1'), {'left': 1, 'right': 4}),
        (Coord(student='s1', assignment='a2'), {'left': 2, 'right': 3}),
    ]


def test_coordinate_joins_group_projections_and_independent_inputs():
    patterns = {'offset': ForEach['assignment'],
                'scores': ForEach['student', 'assignment'],
                'term': ForEach['term']}
    scores, term = coordinate_joins(patterns)
    assert scores.inputs == ('offset', 'scores')
    assert scores.bindings == ('student', 'assignment')
    assert term.inputs == ('term',)
    assert term.bindings == ('term',)


def test_coordinate_joins_reject_overlap_without_a_complete_input():
    patterns = {'scores': ForEach['student', 'assignment'],
                'enrollment': ForEach['student', 'term']}
    with pytest.raises(TypeError, match='common composite axis pattern'):
        coordinate_joins(patterns)


def test_registered_axis_and_coordinate_views():
    plugin = get_dummy_plugin()
    data = Artifact.import_data('Mapping', {'s1': '4', 's2': '-5'})
    record = plugin.axes[data.type, ForEach['sample']]
    assert record.axis is SampleMappingAxis
    assert record.semantic_type == data.type
    assert record.pattern == ForEach['sample']
    assert record.view_type is str
    assert record.directory_format is MappingDirectoryFormat
    assert record.plugin is plugin
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


def test_coordinate_views_from_axis_reflection():
    data = Artifact.import_data('AssignmentScores', {
        's1/a1': '4', 's1/a2': '5', 's2/a1': '6',
    })
    view_types = {('student',): dict, ('student', 'assignment'): str}
    assert {
        axis: {coord: data.view(view_types[axis], **coord)
               for coord in data.coords(*axis)}
        for axis in data.axes()
    } == {
        ('student',): {
            Coord(student='s1'): {'a1': '4', 'a2': '5'},
            Coord(student='s2'): {'a1': '6'},
        },
        ('student', 'assignment'): {
            Coord(student='s1', assignment='a1'): '4',
            Coord(student='s1', assignment='a2'): '5',
            Coord(student='s2', assignment='a1'): '6',
        },
    }


def test_axis_reads_an_artifact_in_an_older_directory_format():
    old = OldMappingDirFmt()
    old.mapping.write_data({'s1': '4'}, dict)
    archiver = Archiver.from_data(
        Mapping, OldMappingDirFmt,
        data_initializer=old.path._move_or_copy,
        provenance_capture=ImportProvenanceCapture())
    data = Artifact._from_archiver(archiver)

    coord, = data.coords('sample')
    assert coord == Coord(sample='s1')
    assert data.view(str, **coord) == '4'
    assert data.format is OldMappingDirFmt


@pytest.mark.parametrize('pattern', [
    ForEach['sample'::],
    ForEach['sample':'other'],
])
def test_axis_registration_rejects_bound_keys(pattern):
    with pytest.raises(TypeError, match='requires plain axis names'):
        get_dummy_plugin().register_axes(Mapping, {pattern: SampleMappingAxis})


def test_plugin_can_extend_axes_of_another_plugins_artifact():
    class RowAxis(SampleMappingAxis):
        def __iter__(self) -> Iterable[Coord]:
            return (Coord(row=key) for key in self.data)

        def __getitem__(self, coord: Coord) -> str:
            return self.data[coord.row]

        @classmethod
        def write(
            cls, chunks: Iterable[tuple[Coord, str]],
        ) -> MappingDirectoryFormat:
            return super().write(
                (Coord(sample=coord.row), value) for coord, value in chunks)

    plugin = Plugin(name='axis-extension', version='1.0.0',
                    website='https://example.com', package=__package__,
                    project_name='axis-extension')
    plugin.register_axes(Mapping, {ForEach['row']: RowAxis})
    manager = PluginManager()
    with (patch.dict(manager.plugins), patch.dict(manager._plugin_by_id),
          patch.dict(manager.axes)):
        manager.add_plugin(plugin)
        record = manager.get_axis(Mapping, ForEach['row'])
        assert record.plugin is plugin
        assert record.view_type is str
        data = Artifact.import_data(Mapping, {'s1': '4'})
        assert data.axes() == [('sample',), ('row',)]
        assert data.view(str, row='s1') == '4'


def test_axis_registration_rejects_inconsistent_cell_annotations():
    class BadAxis(SampleMappingAxis):
        @classmethod
        def write(
            cls, chunks: Iterable[tuple[Coord, int]],
        ) -> MappingDirectoryFormat:
            raise AssertionError('Registration should only inspect write')

    with pytest.raises(TypeError, match=r'write.*\(Coord, str\)'):
        get_dummy_plugin().register_axes(Mapping, {ForEach['other']: BadAxis})
