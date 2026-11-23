# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import pytest

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
    join = CoordinateJoin(dict.fromkeys(domains, ('sample',)),
                          dict(zip(domains, rules)))
    indexes = {
        name: {(key,): f'{name}/{key}' for key in domain}
        for name, domain in domains.items()
    }
    assert list(join.select(indexes)) == [
        ({'sample': key},
         {name: f'{name}/{key}' if key in domain else None
          for name, domain in domains.items()},
         gaps.get(key, ()))
        for key in keys
    ]


@pytest.mark.parametrize(('rule', 'keys', 'missing'), [
    ('matched', ('s1',), ()),
    ('optional', ('s1', 's2'), ()),
    ('required', ('s1', 's2'), ('offset',)),
])
def test_projected_coverage(rule, keys, missing):
    join = CoordinateJoin(
        {'offset': ('student',), 'scores': ('student', 'assignment')},
        {'offset': rule, 'scores': 'required'},
    )
    indexes = {
        'offset': {('s1',): 0, ('s3',): 10},
        'scores': {('s1', 'a1'): 4, ('s2', 'a1'): 5},
    }
    assert list(join.select(indexes)) == [
        ({'student': key, 'assignment': 'a1'},
         {'offset': 0 if key == 's1' else None,
          'scores': 4 if key == 's1' else 5},
         missing if key == 's2' else ())
        for key in keys
    ]


def test_projected_match_filters_the_complete_optional_domain():
    join = CoordinateJoin(
        {'offset': ('student',), 'scores': ('student', 'assignment'),
         'weights': ('student', 'assignment')},
        {'offset': 'matched', 'scores': 'optional', 'weights': 'required'},
    )
    indexes = {
        'offset': {('s1',): 10},
        'scores': {('s1', 'a1'): 4, ('s2', 'a1'): 5},
        'weights': {('s1', 'a1'): 1, ('s1', 'a2'): 1},
    }
    assert list(join.select(indexes)) == [
        ({'student': 's1', 'assignment': 'a1'},
         {'offset': 10, 'scores': 4, 'weights': 1}, ()),
    ]


def test_presence_inputs_include_domain_drivers_and_coverage_checks():
    join = CoordinateJoin(
        {'scores': ('student', 'assignment'), 'offset': ('student',),
         'weight': ('assignment',), 'roster': ('student',)},
        {'scores': 'optional', 'offset': 'optional',
         'weight': 'required', 'roster': 'matched'},
    )
    assert join.presence_inputs == {'scores', 'weight', 'roster'}


@pytest.mark.parametrize(('rule', 'left', 'right'), [
    ('matched', {('s1',): 1}, {('s2',): 2}),
    ('optional', {}, {}),
])
def test_empty_coverage_domain(rule, left, right):
    join = CoordinateJoin(
        {'left': ('sample',), 'right': ('sample',)},
        {'left': rule, 'right': rule},
    )
    assert list(join.select({'left': left, 'right': right})) == []


def test_required_keys_align_by_binding_order():
    join = CoordinateJoin(
        {'left': ('student', 'assignment'),
         'right': ('assignment', 'student')},
        {'left': 'required', 'right': 'required'},
    )
    indexes = {
        'left': {('s1', 'a1'): 1, ('s1', 'a2'): 2},
        'right': {('a2', 's1'): 3, ('a1', 's1'): 4},
    }
    assert list(join.select(indexes)) == [
        ({'student': 's1', 'assignment': 'a1'}, {'left': 1, 'right': 4}, ()),
        ({'student': 's1', 'assignment': 'a2'}, {'left': 2, 'right': 3}, ()),
    ]


def test_coordinate_joins_group_projections_and_independent_inputs():
    bindings = {'offset': ('assignment',),
                'scores': ('student', 'assignment'), 'term': ('term',)}
    rules = dict.fromkeys(bindings, 'matched')
    scores, term = coordinate_joins(bindings, rules)
    assert scores.inputs == ('offset', 'scores')
    assert scores.bindings == ('student', 'assignment')
    assert term.inputs == ('term',)
    assert term.bindings == ('term',)


def test_coordinate_joins_reject_overlap_without_a_complete_input():
    bindings = {'scores': ('student', 'assignment'),
                'enrollment': ('student', 'term')}
    with pytest.raises(TypeError, match='common composite axis pattern'):
        coordinate_joins(bindings, dict.fromkeys(bindings, 'matched'))
