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


class _ForEachMeta(type):
    def __getitem__(cls, entries):
        axes = []
        pattern = []
        for entry in tuplize(entries):
            if isinstance(entry, slice):
                name, binding, rule = entry.start, entry.stop, entry.step
            else:
                name, binding, rule = entry, None, None
            if (not isinstance(name, str) or not name.isidentifier()
                    or name.startswith('_')):
                raise TypeError(f'Invalid axis name {name!r}')
            if binding is not None and not isinstance(binding, str):
                raise TypeError('ForEach bindings must be strings')
            if rule not in (None, 'matched', 'required', 'optional'):
                raise ValueError(f'Invalid axis coverage {rule!r}')
            if isinstance(entry, slice):
                entry = slice(name, name if binding is None else binding,
                              'matched' if rule is None else rule)
            pattern.append(entry)
            axes.append(name)
        if not axes or len(set(axes)) != len(axes):
            raise ValueError('ForEach requires distinct axis names')
        return cls(tuple(pattern))


@dataclass(frozen=True)
class ForEach(metaclass=_ForEachMeta):
    """An ordered axis pattern for an artifact or method annotation."""

    entries: tuple[str | slice, ...]

    def __repr__(self):
        entries = ', '.join(
            f'{entry.start!r}:{entry.stop!r}:{entry.step!r}'
            if isinstance(entry, slice) else repr(entry)
            for entry in self.entries
        )
        return f'ForEach[{entries}]'

    def __hash__(self):
        # Python 3.10 and 3.11 slices are unhashable.
        return hash(tuple(
            (entry.start, entry.stop, entry.step)
            if isinstance(entry, slice) else entry
            for entry in self.entries
        ))

    @property
    def axes(self):
        return tuple(entry.start if isinstance(entry, slice) else entry
                     for entry in self.entries)

    @property
    def bindings(self):
        return tuple(entry.stop if isinstance(entry, slice) else entry
                     for entry in self.entries)

    @property
    def coverage(self):
        return tuple(entry.step if isinstance(entry, slice) else 'matched'
                     for entry in self.entries)

    def normalize(self):
        """Expand all entries to slices and fill in their defaults."""
        bindings = self.bindings
        if len(set(bindings)) != len(bindings):
            raise ValueError(
                'ForEach must use unique bindings in each pattern')
        return ForEach(tuple(map(slice, self.axes, bindings, self.coverage)))

    def as_unbound(self):
        """Return the pattern of bare axis names used for axis registration."""
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
