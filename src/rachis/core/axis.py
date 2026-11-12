# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from collections.abc import Mapping
from dataclasses import dataclass


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


class _Binding:
    """An explicit binding is scoped by object identity."""


@dataclass(frozen=True)
class ForEach:
    """An ordered axis pattern for an artifact or method annotation."""

    axes: tuple
    bindings: tuple
    coverage: tuple

    @staticmethod
    def variable():
        return _Binding()

    @classmethod
    def __class_getitem__(cls, entries):
        if not isinstance(entries, tuple):
            entries = (entries,)
        axes, bindings, coverage = [], [], []
        for entry in entries:
            if isinstance(entry, slice):
                name, binding, rule = entry.start, entry.stop, entry.step
            else:
                name, binding, rule = entry, None, None
            if not isinstance(name, str) or not name.isidentifier():
                raise TypeError(f'Invalid axis name {name!r}')
            if binding is not None and not isinstance(binding, _Binding):
                raise TypeError('Use ForEach.variable() for explicit bindings')
            if rule not in (None, 'matched', 'required', 'optional'):
                raise ValueError(f'Invalid axis coverage {rule!r}')
            axes.append(name)
            bindings.append(name if binding is None else binding)
            coverage.append(rule)
        if not axes or len(set(axes)) != len(axes):
            raise ValueError('ForEach requires distinct axis names')
        if len(set(bindings)) != len(bindings):
            raise ValueError('A ForEach pattern must bind each variable once')
        return cls(tuple(axes), tuple(bindings), tuple(coverage))

    @property
    def axis_key(self):
        return ForEach[self.axes]
