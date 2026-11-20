# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from rachis.sdk.futures import after_all, result_as_future
from rachis.sdk.proxy import output_proxy


# Signature fields must be identifiers, so they cannot shadow this state.
_COMPLETION_FUTURE = 'completion future'


# This class provides an interface similar to a `namedtuple` type. We can't use
# `namedtuple` directly because each `Action` will return a `Results` object
# with `Action`-specific fields (`Action.signature` determines the fields).
# Dynamically-defined namedtuple types aren't pickleable, which is necessary
# for `asynchronous`. They aren't pickleable because the namedtuple type must
# be accessible as a module global, but this global type would be redefined
# each time an `Action` is instantiated.
class Results(tuple):
    """Tuple class representing the named results of an ``Action``.

    Provides an interface similar to a ``namedtuple`` type (e.g. fields are
    accessible as attributes).

    Pending results contain typed output proxies and carry a completion future.
    Users should not need to instantiate this class directly.

    """

    # Subclassing `tuple` requires `__new__` override.
    def __new__(cls, fields, values, *, future=None):
        fields = tuple(fields)
        values = tuple(values)

        if len(fields) != len(values):
            raise ValueError(
                "`fields` and `values` must have matching length: %d != %d" %
                (len(fields), len(values)))

        # Create tuple instance, store fields, and create read-only attributes
        # for each field name. Fields must be stored for pickling/copying (see
        # `__getnewargs__`).
        #
        # Note: setting field names as attributes allows for tab-completion in
        # interactive contexts! Using `__getattr__` does not support this.
        self = super().__new__(cls, values)

        # Must set attributes this way because `__setattr__` prevents
        # setting directly (necessary for immutability).
        object.__setattr__(self, '_fields', fields)
        if future is not None:
            object.__setattr__(self, _COMPLETION_FUTURE, future)

        # Attach field names as instance attributes.
        for field, value in zip(fields, values):
            object.__setattr__(self, field, value)

        return self

    @classmethod
    def _from_future(cls, future, signature):
        outputs = (
            output_proxy(future, name, spec.qiime_type)
            for name, spec in signature.items()
        )
        return cls(signature, outputs, future=future)

    @classmethod
    def _from_named(cls, outputs):
        fields = tuple(outputs)
        values = tuple(outputs.values())
        future = after_all(
            map(result_as_future, values),
            lambda realized: cls(fields, realized),
        )
        return cls(fields, values, future=future)

    def __getnewargs__(self):
        """Arguments to pass to `__new__`. Used by copy and pickle."""
        # `tuple(self)` returns `values`.
        return self._fields, tuple(self)

    # `__setattr__` and `__delattr__` must be defined to prevent users from
    # creating or deleting attributes after this class has been instantiated.
    # `tuple` and `namedtuple` do not have this problem because they are
    # immutable (`__slots__ = ()`). We cannot make this class immutable because
    # we cannot define nonempty `__slots__` when subclassing `tuple`, and we
    # need the `_fields` attribute. We work around this issue by disallowing
    # setting and deleting attributes. The error messages here match those
    # raised by `namedtuple` in Python 3.5.1.
    def __setattr__(self, name, value):
        raise AttributeError("can't set attribute")

    def __delattr__(self, name):
        raise AttributeError("can't delete attribute")

    def __eq__(self, other):
        # Results with different field names should not compare equal, even if
        # their values are equal.
        if not isinstance(other, Results):
            return False
        left = self._result()
        right = other._result()
        return left._fields == right._fields and tuple(left) == tuple(right)

    def __ne__(self, other):
        return not (self == other)

    def __repr__(self):
        # It is possible to provide an evalable repr but this type of repr does
        # not make the field/value pairs apparent. If the constructor accepted
        # **kwargs, the order of field/value pairs would be lost.
        lines = []
        lines.append('%s (name = value)' % self.__class__.__name__)
        lines.append('')

        max_len = -1
        for field in self._fields:
            if len(field) > max_len:
                max_len = len(field)

        for field, value in zip(self._fields, self):
            field_padding = ' ' * (max_len - len(field))
            lines.append('%s%s = %r' % (field, field_padding, value))

        max_len = -1
        for line in lines:
            if len(line) > max_len:
                max_len = len(line)
        lines[1] = '-' * max_len

        return '\n'.join(lines)

    def _asdict(self):
        result = self._result()
        return dict(zip(result._fields, result))

    def _completion_future(self):
        return getattr(self, _COMPLETION_FUTURE, None)

    def _result(self):
        """Return realized results, waiting for completion when needed."""
        future = Results._completion_future(self)
        return self if future is None else future.result()
