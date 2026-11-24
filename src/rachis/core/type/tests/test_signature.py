# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from typing import Annotated

import pytest

from rachis.core.axis import Coord, ForEach
from rachis.core.testing.type import AssignmentScores, Mapping
from rachis.core.type import Int
from rachis.core.type.signature import MethodSignature


def test_solve_coordinates_inner_join():
    def join(
        left: Annotated[str, ForEach['left_id':'key']],
        right: Annotated[str, ForEach['right_id':'key']],
    ) -> Annotated[str, ForEach['key']]:
        raise AssertionError('Coordinate solving cannot call the method')

    signature = MethodSignature(
        join, inputs={'left': Mapping, 'right': Mapping},
        parameters={}, outputs={'result': Mapping},
    )
    indexes = {
        'left': {Coord(left_id=1): 'left 1', Coord(left_id=2): 'left 2'},
        'right': {Coord(right_id=2): 'right 2', Coord(right_id=3): 'right 3'},
    }

    assert list(signature.solve_coordinates(indexes)) == [
        (Coord(key=2), {'left': 'left 2', 'right': 'right 2'}),
    ]


def test_solve_coordinates_left_outer_join():
    def join(
        left: Annotated[str, ForEach['left_id':'key']],
        right: Annotated[str | None, ForEach['right_id':'key':'optional']],
    ) -> Annotated[str, ForEach['key']]:
        raise AssertionError('Coordinate solving cannot call the method')

    signature = MethodSignature(
        join, inputs={'left': Mapping, 'right': Mapping},
        parameters={}, outputs={'result': Mapping},
    )
    indexes = {
        'left': {Coord(left_id=1): 'left 1', Coord(left_id=2): 'left 2'},
        'right': {Coord(right_id=2): 'right 2', Coord(right_id=3): 'right 3'},
    }

    assert list(signature.solve_coordinates(indexes)) == [
        (Coord(key=1), {'left': 'left 1', 'right': None}),
        (Coord(key=2), {'left': 'left 2', 'right': 'right 2'}),
    ]


def test_solve_coordinates_right_outer_join():
    def join(
        left: Annotated[str | None, ForEach['left_id':'key':'optional']],
        right: Annotated[str, ForEach['right_id':'key']],
    ) -> Annotated[str, ForEach['key']]:
        raise AssertionError('Coordinate solving cannot call the method')

    signature = MethodSignature(
        join, inputs={'left': Mapping, 'right': Mapping},
        parameters={}, outputs={'result': Mapping},
    )
    indexes = {
        'left': {Coord(left_id=1): 'left 1', Coord(left_id=2): 'left 2'},
        'right': {Coord(right_id=2): 'right 2', Coord(right_id=3): 'right 3'},
    }

    assert list(signature.solve_coordinates(indexes)) == [
        (Coord(key=2), {'left': 'left 2', 'right': 'right 2'}),
        (Coord(key=3), {'left': None, 'right': 'right 3'}),
    ]


def test_solve_coordinates_full_outer_join():
    def join(
        left: Annotated[str | None, ForEach['left_id':'key':'optional']],
        right: Annotated[str | None, ForEach['right_id':'key':'optional']],
    ) -> Annotated[str, ForEach['key']]:
        raise AssertionError('Coordinate solving cannot call the method')

    signature = MethodSignature(
        join, inputs={'left': Mapping, 'right': Mapping},
        parameters={}, outputs={'result': Mapping},
    )
    indexes = {
        'left': {Coord(left_id=1): 'left 1', Coord(left_id=2): 'left 2'},
        'right': {Coord(right_id=2): 'right 2', Coord(right_id=3): 'right 3'},
    }

    assert list(signature.solve_coordinates(indexes)) == [
        (Coord(key=1), {'left': 'left 1', 'right': None}),
        (Coord(key=2), {'left': 'left 2', 'right': 'right 2'}),
        (Coord(key=3), {'left': None, 'right': 'right 3'}),
    ]


def test_solve_coordinates_inner_join_in_a_product():
    def join(
        left: Annotated[str, ForEach['left_id':'key']],
        right: Annotated[str, ForEach['right_id':'key']],
        replicates: Annotated[int, ForEach['replicate']],
    ) -> Annotated[str, ForEach['key', 'replicate']]:
        raise AssertionError('Coordinate solving cannot call the method')

    signature = MethodSignature(
        join, inputs={'left': Mapping, 'right': Mapping},
        parameters={'replicates': Int}, outputs={'result': AssignmentScores},
    )
    indexes = {
        'left': {Coord(left_id=1): 'left 1', Coord(left_id=2): 'left 2'},
        'right': {Coord(right_id=2): 'right 2', Coord(right_id=3): 'right 3'},
        'replicates': {Coord(replicate=0): 0, Coord(replicate=1): 1},
    }

    assert list(signature.solve_coordinates(indexes)) == [
        (Coord(key=2, replicate=0),
         {'left': 'left 2', 'right': 'right 2', 'replicates': 0}),
        (Coord(key=2, replicate=1),
         {'left': 'left 2', 'right': 'right 2', 'replicates': 1}),
    ]


def test_solve_coordinates_left_outer_join_in_a_product():
    def join(
        left: Annotated[str, ForEach['left_id':'key']],
        right: Annotated[str | None, ForEach['right_id':'key':'optional']],
        replicates: Annotated[int, ForEach['replicate']],
    ) -> Annotated[str, ForEach['key', 'replicate']]:
        raise AssertionError('Coordinate solving cannot call the method')

    signature = MethodSignature(
        join, inputs={'left': Mapping, 'right': Mapping},
        parameters={'replicates': Int}, outputs={'result': AssignmentScores},
    )
    indexes = {
        'left': {Coord(left_id=1): 'left 1', Coord(left_id=2): 'left 2'},
        'right': {Coord(right_id=2): 'right 2', Coord(right_id=3): 'right 3'},
        'replicates': {Coord(replicate=0): 0, Coord(replicate=1): 1},
    }

    assert list(signature.solve_coordinates(indexes)) == [
        (Coord(key=1, replicate=0),
         {'left': 'left 1', 'right': None, 'replicates': 0}),
        (Coord(key=1, replicate=1),
         {'left': 'left 1', 'right': None, 'replicates': 1}),
        (Coord(key=2, replicate=0),
         {'left': 'left 2', 'right': 'right 2', 'replicates': 0}),
        (Coord(key=2, replicate=1),
         {'left': 'left 2', 'right': 'right 2', 'replicates': 1}),
    ]


def test_solve_coordinates_right_outer_join_in_a_product():
    def join(
        left: Annotated[str | None, ForEach['left_id':'key':'optional']],
        right: Annotated[str, ForEach['right_id':'key']],
        replicates: Annotated[int, ForEach['replicate']],
    ) -> Annotated[str, ForEach['key', 'replicate']]:
        raise AssertionError('Coordinate solving cannot call the method')

    signature = MethodSignature(
        join, inputs={'left': Mapping, 'right': Mapping},
        parameters={'replicates': Int}, outputs={'result': AssignmentScores},
    )
    indexes = {
        'left': {Coord(left_id=1): 'left 1', Coord(left_id=2): 'left 2'},
        'right': {Coord(right_id=2): 'right 2', Coord(right_id=3): 'right 3'},
        'replicates': {Coord(replicate=0): 0, Coord(replicate=1): 1},
    }

    assert list(signature.solve_coordinates(indexes)) == [
        (Coord(key=2, replicate=0),
         {'left': 'left 2', 'right': 'right 2', 'replicates': 0}),
        (Coord(key=2, replicate=1),
         {'left': 'left 2', 'right': 'right 2', 'replicates': 1}),
        (Coord(key=3, replicate=0),
         {'left': None, 'right': 'right 3', 'replicates': 0}),
        (Coord(key=3, replicate=1),
         {'left': None, 'right': 'right 3', 'replicates': 1}),
    ]


def test_solve_coordinates_full_outer_join_in_a_product():
    def join(
        left: Annotated[str | None, ForEach['left_id':'key':'optional']],
        right: Annotated[str | None, ForEach['right_id':'key':'optional']],
        replicates: Annotated[int, ForEach['replicate']],
    ) -> Annotated[str, ForEach['key', 'replicate']]:
        raise AssertionError('Coordinate solving cannot call the method')

    signature = MethodSignature(
        join, inputs={'left': Mapping, 'right': Mapping},
        parameters={'replicates': Int}, outputs={'result': AssignmentScores},
    )
    indexes = {
        'left': {Coord(left_id=1): 'left 1', Coord(left_id=2): 'left 2'},
        'right': {Coord(right_id=2): 'right 2', Coord(right_id=3): 'right 3'},
        'replicates': {Coord(replicate=0): 0, Coord(replicate=1): 1},
    }

    assert list(signature.solve_coordinates(indexes)) == [
        (Coord(key=1, replicate=0),
         {'left': 'left 1', 'right': None, 'replicates': 0}),
        (Coord(key=1, replicate=1),
         {'left': 'left 1', 'right': None, 'replicates': 1}),
        (Coord(key=2, replicate=0),
         {'left': 'left 2', 'right': 'right 2', 'replicates': 0}),
        (Coord(key=2, replicate=1),
         {'left': 'left 2', 'right': 'right 2', 'replicates': 1}),
        (Coord(key=3, replicate=0),
         {'left': None, 'right': 'right 3', 'replicates': 0}),
        (Coord(key=3, replicate=1),
         {'left': None, 'right': 'right 3', 'replicates': 1}),
    ]


def test_solve_coordinates_broadcasts_optional_inputs_over_the_product():
    def score(
        scores: Annotated[str, ForEach['student', 'assignment']],
        offset: Annotated[str | None, ForEach['sample':'student':'optional']],
        terms: Annotated[int, ForEach['iteration':'term']],
    ) -> Annotated[str, ForEach['student', 'assignment', 'term']]:
        raise AssertionError('Coordinate solving cannot call the method')

    signature = MethodSignature(
        score, inputs={'scores': AssignmentScores, 'offset': Mapping},
        parameters={'terms': Int}, outputs={'result': AssignmentScores},
    )
    indexes = {
        'scores': {Coord(student='s1', assignment='a1'): '4',
                   Coord(student='s2', assignment='a1'): '5'},
        'offset': {Coord(sample='s1'): '10'},
        'terms': {Coord(iteration=0): 0, Coord(iteration=1): 1},
    }

    assert list(signature.solve_coordinates(indexes)) == [
        (Coord(student='s1', assignment='a1', term=0),
         {'scores': '4', 'offset': '10', 'terms': 0}),
        (Coord(student='s1', assignment='a1', term=1),
         {'scores': '4', 'offset': '10', 'terms': 1}),
        (Coord(student='s2', assignment='a1', term=0),
         {'scores': '5', 'offset': None, 'terms': 0}),
        (Coord(student='s2', assignment='a1', term=1),
         {'scores': '5', 'offset': None, 'terms': 1}),
    ]


def test_solve_coordinates_broadcasts_a_curve_per_assignment():
    def grade(
        scores: Annotated[str, ForEach['student', 'assignment']],
        curve: Annotated[str, ForEach['sample':'assignment':'required']],
    ) -> Annotated[str, ForEach['student', 'assignment']]:
        raise AssertionError('Coordinate solving cannot call the method')

    signature = MethodSignature(
        grade, inputs={'scores': AssignmentScores, 'curve': Mapping},
        parameters={}, outputs={'result': AssignmentScores},
    )
    indexes = {
        'scores': {Coord(student='s1', assignment='a1'): '4',
                   Coord(student='s1', assignment='a2'): '5',
                   Coord(student='s2', assignment='a1'): '6',
                   Coord(student='s2', assignment='a2'): '7'},
        'curve': {Coord(sample='a2'): '20', Coord(sample='a1'): '10'},
    }

    assert list(signature.solve_coordinates(indexes)) == [
        (Coord(student='s1', assignment='a1'), {'scores': '4', 'curve': '10'}),
        (Coord(student='s1', assignment='a2'), {'scores': '5', 'curve': '20'}),
        (Coord(student='s2', assignment='a1'), {'scores': '6', 'curve': '10'}),
        (Coord(student='s2', assignment='a2'), {'scores': '7', 'curve': '20'}),
    ]


def test_solve_coordinates_intersects_matched_inputs_with_reordered_axes():
    def grade(
        scores: Annotated[str, ForEach['student', 'assignment']],
        rubric: Annotated[str, ForEach[
            'task':'assignment', 'person':'student',
        ]],
        roster: Annotated[str, ForEach['id':'student':'required']],
    ) -> Annotated[str, ForEach['student', 'assignment']]:
        raise AssertionError('Coordinate solving cannot call the method')

    signature = MethodSignature(
        grade, inputs={'scores': AssignmentScores, 'rubric': AssignmentScores,
                       'roster': Mapping},
        parameters={}, outputs={'result': AssignmentScores},
    )
    indexes = {
        'scores': {Coord(student='s1', assignment='a1'): '4',
                   Coord(student='s1', assignment='a2'): '5',
                   Coord(student='s2', assignment='a1'): '6'},
        'rubric': {Coord(task='a2', person='s1'): 'rubric 2',
                   Coord(task='a1', person='s1'): 'rubric 1',
                   Coord(task='a3', person='s3'): 'rubric 3'},
        'roster': {Coord(id='s1'): 'student 1'},
    }

    assert list(signature.solve_coordinates(indexes)) == [
        (Coord(student='s1', assignment='a1'),
         {'scores': '4', 'rubric': 'rubric 1', 'roster': 'student 1'}),
        (Coord(student='s1', assignment='a2'),
         {'scores': '5', 'rubric': 'rubric 2', 'roster': 'student 1'}),
    ]


def test_solve_coordinates_aligns_overlaps_against_a_complete_input():
    def combine(
        left: Annotated[str, ForEach['a', 'b']],
        right: Annotated[str, ForEach['b', 'c']],
        complete: Annotated[str, ForEach['a', 'b', 'c']],
    ) -> Annotated[str, ForEach['a', 'b', 'c']]:
        raise AssertionError('Coordinate solving cannot call the method')

    signature = MethodSignature(
        combine, inputs=dict.fromkeys(
            ('left', 'right', 'complete'), AssignmentScores),
        parameters={}, outputs={'result': AssignmentScores},
    )
    indexes = {
        'left': {Coord(a=1, b=1): 'left 1', Coord(a=2, b=1): 'left 2'},
        'right': {Coord(b=1, c=1): 'right 1', Coord(b=1, c=2): 'right 2'},
        'complete': {
            Coord(a=1, b=1, c=2): 'complete 1',
            Coord(a=2, b=1, c=1): 'complete 2',
            Coord(a=2, b=1, c=3): 'complete 3',
        },
    }

    assert list(signature.solve_coordinates(indexes)) == [
        (Coord(a=1, b=1, c=2),
         {'left': 'left 1', 'right': 'right 2', 'complete': 'complete 1'}),
        (Coord(a=2, b=1, c=1),
         {'left': 'left 2', 'right': 'right 1', 'complete': 'complete 2'}),
    ]


def test_signature_rejects_overlapping_inputs_without_a_complete_input():
    def combine(
        left: Annotated[str, ForEach['a', 'b']],
        right: Annotated[str, ForEach['b', 'c']],
    ) -> Annotated[str, ForEach['a', 'b', 'c']]:
        raise AssertionError('Coordinate solving cannot call the method')

    with pytest.raises(TypeError, match='common composite axis pattern'):
        MethodSignature(
            combine, inputs={'left': AssignmentScores,
                             'right': AssignmentScores},
            parameters={}, outputs={'result': AssignmentScores},
        )


def test_solve_coordinates_unions_complete_optional_inputs():
    def collect_scores(
        first: Annotated[str | None, ForEach[
            'student'::'optional', 'assignment'::'optional',
        ]],
        second: Annotated[str | None, ForEach[
            'person':'student':'optional', 'task':'assignment':'optional',
        ]],
        weights: Annotated[str, ForEach[
            'student'::'required', 'assignment'::'required',
        ]],
    ) -> Annotated[str, ForEach['student', 'assignment']]:
        raise AssertionError('Coordinate solving cannot call the method')

    signature = MethodSignature(
        collect_scores,
        inputs=dict.fromkeys(('first', 'second', 'weights'), AssignmentScores),
        parameters={}, outputs={'result': AssignmentScores},
    )
    indexes = {
        'first': {Coord(student='s1', assignment='a1'): '4',
                  Coord(student='s2', assignment='a2'): '5'},
        'second': {Coord(person='s2', task='a2'): '6',
                   Coord(person='s3', task='a3'): '7'},
        'weights': {Coord(student='s1', assignment='a1'): '1',
                    Coord(student='s2', assignment='a2'): '2',
                    Coord(student='s3', assignment='a3'): '3',
                    Coord(student='s4', assignment='a4'): '4'},
    }

    assert list(signature.solve_coordinates(indexes)) == [
        (Coord(student='s1', assignment='a1'),
         {'first': '4', 'second': None, 'weights': '1'}),
        (Coord(student='s2', assignment='a2'),
         {'first': '5', 'second': '6', 'weights': '2'}),
        (Coord(student='s3', assignment='a3'),
         {'first': None, 'second': '7', 'weights': '3'}),
    ]


@pytest.mark.parametrize('terms', [
    {Coord(term=0): 0},
    {},
], ids=['nonempty-product', 'empty-product'])
def test_solve_coordinates_checks_required_coverage_before_yielding(terms):
    def score(
        terms: Annotated[int, ForEach['term']],
        scores: Annotated[str, ForEach['student', 'assignment']],
        roster: Annotated[str, ForEach['id':'student':'required']],
    ) -> Annotated[str, ForEach['term', 'student', 'assignment']]:
        raise AssertionError('Coordinate solving cannot call the method')

    signature = MethodSignature(
        score, inputs={'scores': AssignmentScores, 'roster': Mapping},
        parameters={'terms': Int}, outputs={'result': AssignmentScores},
    )
    indexes = {
        'terms': terms,
        'scores': {Coord(student='s1', assignment='a1'): '4',
                   Coord(student='s2', assignment='a1'): '5'},
        'roster': {Coord(id='s1'): 'student 1'},
    }

    with pytest.raises(ValueError, match="Required inputs.*roster.*s2"):
        next(signature.solve_coordinates(indexes))
