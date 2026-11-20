# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from collections.abc import Iterable, Iterator

from typing import Annotated

from rachis import Coord, ForEach
from .format import MappingDirectoryFormat


def _write_mapping(data):
    directory = MappingDirectoryFormat()
    directory.mapping.write_data(dict(data), dict)
    return directory


class SampleMappingAxis:
    def __init__(self, path, *, mode):
        self.data = MappingDirectoryFormat(path, mode=mode).mapping.view(dict)

    def __iter__(self) -> Iterator[Coord]:
        return (Coord(sample=key) for key in self.data)

    def __getitem__(self, coord: Coord) -> str:
        return self.data[coord.sample]

    @classmethod
    def write(
        cls, chunks: Iterable[tuple[Coord, str]],
    ) -> MappingDirectoryFormat:
        return _write_mapping((coord.sample, value) for coord, value in chunks)


class AssignmentScoreAxis(SampleMappingAxis):
    def __iter__(self) -> Iterator[Coord]:
        for key in self.data:
            student, assignment = key.split('/')
            yield Coord(student=student, assignment=assignment)

    def __getitem__(self, coord: Coord) -> str:
        return self.data[f'{coord.student}/{coord.assignment}']

    @classmethod
    def write(
        cls, chunks: Iterable[tuple[Coord, str]],
    ) -> MappingDirectoryFormat:
        return _write_mapping((f'{coord.student}/{coord.assignment}', value)
                              for coord, value in chunks)


class StudentScoresAxis(AssignmentScoreAxis):
    def __iter__(self) -> Iterator[Coord]:
        return iter(dict.fromkeys(
            Coord(student=coord.student) for coord in super().__iter__()))

    def __getitem__(self, coord: Coord) -> dict:
        return {
            cell.assignment: self.data[f'{cell.student}/{cell.assignment}']
            for cell in super().__iter__() if cell.student == coord.student
        }

    @classmethod
    def write(
        cls, chunks: Iterable[tuple[Coord, dict]],
    ) -> MappingDirectoryFormat:
        return _write_mapping((f'{coord.student}/{assignment}', value)
                              for coord, scores in chunks
                              for assignment, value in scores.items())


def filter_samples(
    data: Annotated[str, ForEach['sample']],
) -> tuple[
    Annotated[str | None, ForEach['sample']],
    Annotated[str, ForEach['sample']],
]:
    return (data if int(data) >= 0 else None), data


def increment_samples(
    data: Annotated[str, ForEach['sample']],
    amount: int,
) -> Annotated[str, ForEach['sample']]:
    return str(int(data) + amount)


def mapped_sample_pipeline(ctx, data):
    filter_samples = ctx.get_action('dummy_plugin', 'filter_samples')
    increment = ctx.get_action('dummy_plugin', 'increment_samples')
    retained, _ = filter_samples(data)
    intermediate, = increment(retained, 1)
    final, = increment(intermediate, 2)
    return final


def filter_scores(
    data: Annotated[str, ForEach['student', 'assignment']],
) -> Annotated[str | None, ForEach['student', 'assignment']]:
    return data if int(data) >= 0 else None


def sum_scores(
    data: Annotated[dict, ForEach['student']],
) -> Annotated[str, ForEach['sample':'student']]:
    return str(sum(map(int, data.values())))


def mapped_scores_pipeline(ctx, data):
    filter_scores = ctx.get_action('dummy_plugin', 'filter_scores')
    sum_scores = ctx.get_action('dummy_plugin', 'sum_scores')
    filtered, = filter_scores(data)
    totals, = sum_scores(filtered)
    output, = ctx.get_action('dummy_plugin', 'increment_samples')(totals, 1)
    return filtered, output
