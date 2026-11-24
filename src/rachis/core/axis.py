# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from collections.abc import Generator, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
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

    def __and__(self, other):
        """Combine compatible coordinates, keeping left bindings first."""
        if not isinstance(other, Coord):
            return NotImplemented
        axes = dict(self._items)
        for name, value in other._items:
            existing = axes.setdefault(name, value)
            if existing is not value and existing != value:
                raise ValueError(f'Conflicting values for axis {name!r}')
        return Coord(**axes)

    def project(self, names):
        """Keep the requested names in the specified order."""
        return Coord(**{name: self[name] for name in names})

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    def __repr__(self):
        return 'Coord(' + ', '.join(f'{k}={v!r}' for k, v in self._items) + ')'


@dataclass(frozen=True)
class AxisBinding:
    axis: str
    binding: str
    coverage: str


class _ForEachMeta(type):
    def __getitem__(cls, entries):
        entries = tuplize(entries)
        bound = any(isinstance(entry, slice) for entry in entries)
        pattern = []
        for entry in entries:
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
            pattern.append(AxisBinding(
                name, name if binding is None else binding,
                'matched' if rule is None else rule,
            ))
        axes = {entry.axis for entry in pattern}
        if not axes or len(axes) != len(pattern):
            raise ValueError('ForEach requires distinct axis names')
        if len({entry.coverage for entry in pattern}) != 1:
            raise TypeError('A composite axis needs uniform coverage')
        return cls(tuple(pattern), bound)


@dataclass(frozen=True)
class ForEach(metaclass=_ForEachMeta):
    """An ordered axis pattern for an artifact or method annotation."""

    entries: tuple[AxisBinding, ...]
    _bound: bool = False

    def __repr__(self):
        entries = ', '.join(
            f'{entry.axis!r}:{entry.binding!r}:{entry.coverage!r}'
            if self._bound else repr(entry.axis)
            for entry in self.entries
        )
        return f'ForEach[{entries}]'

    @property
    def axes(self):
        return tuple(entry.axis for entry in self.entries)

    @property
    def bindings(self):
        return tuple(entry.binding for entry in self.entries)

    @property
    def coverage(self):
        return tuple(entry.coverage for entry in self.entries)

    @property
    def coverage_rule(self):
        """The shared coverage rule for the complete axis path."""
        return self.entries[0].coverage

    def normalize(self):
        """Use the bound form of the pattern and validate binding names."""
        if len(set(self.bindings)) != len(self.entries):
            raise ValueError(
                'ForEach must use unique bindings in each pattern')
        return replace(self, _bound=True)

    def as_unbound(self):
        """Return the pattern of bare axis names used for axis registration."""
        return ForEach[self.axes]

    def to_bound_coord(self, coord):
        """Read an artifact coordinate using the method's binding names."""
        return Coord(**{entry.binding: coord[entry.axis]
                        for entry in self.entries})

    def to_axis_coord(self, coord):
        """Project a method coordinate onto the artifact's axis names."""
        return Coord(**{entry.axis: coord[entry.binding]
                        for entry in self.entries})


def coordinate_joins(patterns):
    """Group input bindings into independent joins for a Cartesian product."""
    binding_sets = tuple(dict.fromkeys(
        frozenset(pattern.bindings) for pattern in patterns.values()
    ))
    joins = []
    seen = set()
    for group in binding_sets:
        if any(group < other for other in binding_sets):
            continue
        if seen.intersection(group):
            raise TypeError(
                'Overlapping mapped inputs need a common composite '
                'axis pattern'
            )
        seen.update(group)
        joins.append(CoordinateJoin({
            name: pattern for name, pattern in patterns.items()
            if group.issuperset(pattern.bindings)
        }))
    return tuple(joins)


class CoordinateJoin:
    """Join inputs on one complete coordinate and projections of it.

    Complete inputs supply candidate keys. Matched inputs filter the keys
    by intersection. Without a complete matched input, the union of complete
    optional inputs drives the join, or the union of complete required inputs
    if all complete inputs are required. Projected optional inputs supply a
    value or None. Every required input must cover the resulting domain.
    """

    def __init__(self, patterns):
        self.inputs = tuple(patterns)
        self._input_bindings = {name: pattern.bindings
                                for name, pattern in patterns.items()}
        self.bindings = max(self._input_bindings.values(), key=len)
        rules = {name: pattern.coverage_rule
                 for name, pattern in patterns.items()}
        self._complete = tuple(
            name for name, bindings in self._input_bindings.items()
            if len(bindings) == len(self.bindings)
        )
        self._matched = frozenset(
            name for name, rule in rules.items() if rule == 'matched')
        self._required = tuple(
            name for name, rule in rules.items() if rule == 'required')
        complete = frozenset(self._complete)
        optional = frozenset(
            name for name in self._complete if rules[name] == 'optional')
        self._domain = (complete & self._matched) or optional or complete

    @property
    def presence_inputs(self):
        """Names whose realized keys can change coverage."""
        return self._domain | self._matched | frozenset(self._required)

    def coverage(self, present):
        """Return missing required inputs, or None for an excluded key."""
        if self._domain.isdisjoint(present) or not self._matched <= present:
            return None
        return tuple(name for name in self._required if name not in present)

    def align(self, indexes):
        """Yield an invocation coordinate and its aligned input values.

        Input indexes are keyed by coordinates in each input's binding order.
        Excluded coordinates are skipped. A missing required input raises.
        """
        coordinates = dict.fromkeys(
            coord.project(self.bindings)
            for name in self._complete
            for coord in indexes[name]
        )

        for coord in coordinates:
            selected = {
                name: indexes[name].get(coord.project(bindings))
                for name, bindings in self._input_bindings.items()
            }
            present = {name for name, value in selected.items()
                       if value is not None}
            missing = self.coverage(present)
            if missing is None:
                continue
            if missing:
                raise ValueError(f'Required inputs {list(missing)!r} lack '
                                 f'{coord!r}')
            yield coord, selected


def validate_axis_protocol(axis):
    """Check the axis protocol and return its view and directory types."""
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
    return view, directory_format
