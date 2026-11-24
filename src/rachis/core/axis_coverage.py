# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------


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
