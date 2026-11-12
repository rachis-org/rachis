# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from rachis import Coord
from .format import MappingDirectoryFormat


def _write_mapping(data):
    directory = MappingDirectoryFormat()
    directory.mapping.write_data(dict(data), dict)
    return directory


class SampleMappingAxis:
    cell_type = str

    def __init__(self, path, *, mode):
        self.data = MappingDirectoryFormat(path, mode=mode).mapping.view(dict)

    def __iter__(self):
        return (Coord(sample=key) for key in self.data)

    def __getitem__(self, coord):
        return self.data[coord.sample]

    @classmethod
    def write(cls, chunks):
        return _write_mapping((coord.sample, value) for coord, value in chunks)


class SampleMagMappingAxis(SampleMappingAxis):
    def __iter__(self):
        for key in self.data:
            sample, mag = key.split('/')
            yield Coord(sample=sample, mag=mag)

    def __getitem__(self, coord):
        return self.data[f'{coord.sample}/{coord.mag}']

    @classmethod
    def write(cls, chunks):
        return _write_mapping(
            (f'{coord.sample}/{coord.mag}', value) for coord, value in chunks)


class SampleMagMarginAxis(SampleMagMappingAxis):
    cell_type = dict

    def __iter__(self):
        return iter(dict.fromkeys(
            Coord(sample=coord.sample) for coord in super().__iter__()))

    def __getitem__(self, coord):
        return {
            cell.mag: self.data[f'{cell.sample}/{cell.mag}']
            for cell in super().__iter__() if cell.sample == coord.sample
        }

    @classmethod
    def write(cls, chunks):
        return super().write(
            (Coord(sample=coord.sample, mag=mag), value)
            for coord, mags in chunks for mag, value in mags.items())
