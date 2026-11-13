# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from collections.abc import Generator, Iterable, Iterator, Mapping
from dataclasses import dataclass
from inspect import getattr_static, signature
from typing import Any, get_args, get_origin, get_type_hints

from rachis.core.util import tuplize


@dataclass(frozen=True, init=False)
class Coord(Mapping):
    """An ordered, hashable coordinate with named axes."""

    _items: tuple

    def __init__(self, **axes):
        object.__setattr__(self, '_items', tuple(axes.items()))

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._items[key][1]
        for name, value in self._items:
            if name == key:
                return value
        raise KeyError(key)

    def __iter__(self):
        return (name for name, _ in self._items)

    def __len__(self):
        return len(self._items)

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    def __repr__(self):
        return 'Coord(' + ', '.join(f'{k}={v!r}' for k, v in self._items) + ')'


@dataclass(frozen=True, eq=False)
class _AxisBinding:
    """Constructive bindings compare by name, nominal bindings by identity."""

    name: str | None = None

    def __eq__(self, other):
        if not isinstance(other, _AxisBinding):
            return NotImplemented
        return self is other or (
            self.name is not None and self.name == other.name)

    def __hash__(self):
        return object.__hash__(self) if self.name is None else hash(self.name)


class _ForEachMeta(type):
    def variable(cls):
        return _AxisBinding()

    def __getitem__(cls, entries):
        axes, bindings, coverage = [], [], []
        declares_coverage = False
        for entry in tuplize(entries):
            if isinstance(entry, slice):
                name, binding, rule = entry.start, entry.stop, entry.step
            else:
                name, binding, rule = entry, None, None
            if (not isinstance(name, str) or not name.isidentifier()
                    or name.startswith('_')):
                raise TypeError(f'Invalid axis name {name!r}')
            if binding is not None and not isinstance(binding, _AxisBinding):
                raise TypeError('Use ForEach.variable() for explicit bindings')
            if rule not in (None, 'matched', 'required', 'optional'):
                raise ValueError(f'Invalid axis coverage {rule!r}')
            axes.append(name)
            bindings.append(_AxisBinding(name) if binding is None else binding)
            coverage.append('matched' if rule is None else rule)
            declares_coverage |= rule is not None
        if not axes or len(set(axes)) != len(axes):
            raise ValueError('ForEach requires distinct axis names')
        if len(set(bindings)) != len(bindings):
            raise ValueError(
                'ForEach must use unique variables in each pattern')
        return cls(tuple(axes), tuple(bindings), tuple(coverage),
                   declares_coverage)


@dataclass(frozen=True)
class ForEach(metaclass=_ForEachMeta):
    """An ordered axis pattern for an artifact or method annotation."""

    axes: tuple
    bindings: tuple
    coverage: tuple
    declares_coverage: bool = False

    def as_unbound(self):
        return ForEach[self.axes]


def validate_axis_protocol(axis):
    """Check that an axis reads and writes the same annotated cell view."""
    from rachis.plugin.model import DirectoryFormat

    if not isinstance(axis, type) or not all(
        callable(getattr(axis, name, None))
        for name in ('__iter__', '__getitem__', 'write')
    ):
        raise TypeError(f'{axis!r} does not implement the axis API')
    if not isinstance(getattr_static(axis, 'write'), classmethod):
        raise TypeError(f'{axis.__name__}.write must be a class method')

    try:
        signature(axis.__iter__).bind(None)
        _, coordinate = signature(axis.__getitem__).bind(None, None).arguments
        chunks, = signature(axis.write).bind(None).arguments
        iterator = get_type_hints(axis.__iter__).get('return')
        reader = get_type_hints(axis.__getitem__)
        writer = get_type_hints(axis.write)
    except (TypeError, ValueError, NameError) as error:
        raise TypeError(
            f'{axis.__name__} has an invalid axis signature') from error

    if (get_origin(iterator) not in (Iterable, Iterator, Generator)
            or get_args(iterator)[:1] != (Coord,)):
        raise TypeError(f'{axis.__name__}.__iter__ must yield Coord values')

    view = reader.get('return')
    if (reader.get(coordinate) is not Coord
            or not isinstance(view, type) or view is Any):
        raise TypeError(f'{axis.__name__}.__getitem__ must take a Coord '
                        'and return a view type')

    chunk_type = writer.get(chunks)
    pair, *_ = get_args(chunk_type) or (None,)
    if (get_origin(chunk_type) not in (Iterable, Iterator, Generator)
            or get_origin(pair) is not tuple
            or get_args(pair) != (Coord, view)):
        raise TypeError(f'{axis.__name__}.write must take an iterable of '
                        f'(Coord, {view.__name__}) pairs')
    directory_format = writer.get('return')
    if (not isinstance(directory_format, type)
            or not issubclass(directory_format, DirectoryFormat)):
        raise TypeError(f'{axis.__name__}.write must return a DirectoryFormat')
