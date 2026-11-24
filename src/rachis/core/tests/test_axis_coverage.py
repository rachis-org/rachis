# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from itertools import islice

import pytest

from rachis.core.axis import Coord, ForEach
from rachis.core.axis_coverage import CoordinateJoin, coordinate_joins


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
