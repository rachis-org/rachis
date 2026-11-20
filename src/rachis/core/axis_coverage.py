# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------


def coordinate_joins(bindings, rules):
    """Group input bindings into independent joins for a Cartesian product."""
    binding_sets = tuple(dict.fromkeys(map(frozenset, bindings.values())))
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
            name: path for name, path in bindings.items()
            if group.issuperset(path)
        }, rules))
    return tuple(joins)


class CoordinateJoin:
    """Join inputs on one complete coordinate and projections of it.

    Complete inputs supply candidate keys. Matched inputs filter the keys
    by intersection. Without a complete matched input, the union of complete
    optional inputs drives the join, or the union of complete required inputs
    if all complete inputs are required. Projected optional inputs supply a
    value or None. Every required input must cover the resulting domain.
    """

    def __init__(self, bindings, rules):
        self.inputs = tuple(bindings)
        self.bindings = max(bindings.values(), key=len)
        self._input_bindings = bindings
        self._complete = tuple(
            name for name, path in bindings.items()
            if len(path) == len(self.bindings)
        )
        self._matched = frozenset(
            name for name in bindings if rules[name] == 'matched')
        self._required = tuple(
            name for name in bindings if rules[name] == 'required')
        complete = frozenset(self._complete)
        optional = frozenset(
            name for name in self._complete if rules[name] == 'optional')
        self._domain = (complete & self._matched) or optional or complete

    def coverage(self, present):
        """Return missing required inputs, or None for an excluded key."""
        if self._domain.isdisjoint(present) or not self._matched <= present:
            return None
        return tuple(name for name in self._required if name not in present)

    def select(self, indexes):
        """Yield bindings, aligned inputs and coverage gaps for each key.

        Index keys follow each input's binding order. Index values are opaque
        to the join. None denotes a missing input.
        """
        keys = {}
        for name in self._complete:
            path = self._input_bindings[name]
            for key in indexes[name]:
                bound = dict(zip(path, key))
                keys[tuple(bound[binding] for binding in self.bindings)] = None

        for key in keys:
            bound = dict(zip(self.bindings, key))
            selected = {
                name: indexes[name].get(tuple(bound[binding]
                                              for binding in path))
                for name, path in self._input_bindings.items()
            }
            present = {name for name, value in selected.items()
                       if value is not None}
            missing = self.coverage(present)
            if missing is not None:
                yield bound, selected, missing
