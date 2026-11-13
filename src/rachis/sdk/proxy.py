# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from rachis.sdk.iresult import IResult
from rachis.sdk.futures import after_all, completed_future

import rachis.core.transform as transform
from rachis.core.type.util import is_visualization_type, is_collection_type


class Proxy:
    """Base class to indicate that a given class that inherits from it is a
    proxy. Also implements some generic functionality
    """
    def __eq__(self, other):
        return self.result() == other.result()

    def __ne__(self, other):
        return not (self == other)


def result_future(value):
    """Resolve a result reference, including any prospective collection."""
    from rachis.sdk.result import ResultCollection

    if isinstance(value, (ProxyResult, ProxyResultCollection)):
        return after_all((value._future_,), value._get_element_)
    if isinstance(value, ResultCollection):
        keys = tuple(value.keys())
        return after_all(map(result_future, value.values()),
                         lambda *items: ResultCollection(
                             dict(zip(keys, items))))
    if isinstance(value, list):
        return after_all(map(result_future, value), lambda *items: list(items))
    if isinstance(value, dict):
        keys = tuple(value)
        return after_all(map(result_future, value.values()),
                         lambda *items: dict(zip(keys, items)))
    return completed_future(value)


class ProxyResult(Proxy, IResult):
    def __init__(self, future, selector, qiime_type=None):
        """We have a future that represents the results of some QIIME 2 action,
        and we have a selector indicating specifically which result we want
        """
        self._future_ = future
        self._selector_ = selector
        self._qiime_type_ = qiime_type

    def __repr__(self):
        if self._qiime_type_ is None:
            return f'<{self.__class__.__name__.lower()}: Unknown Type ' \
                   f'{object.__repr__(self)}>'
        else:
            return f'<{self.__class__.__name__.lower()}: {self.type}>'

    def __hash__(self):
        return hash(self.uuid)

    def _alias(self, name, provenance, ctx, qiime_type=None):
        if self._qiime_type_ is not None:
            self._assert_alias_type_refines_realized_type(
                qiime_type, self._qiime_type_)

        future = ctx._join(
            (result_future(self),),
            lambda result: result._alias(name, provenance, ctx, qiime_type))
        alias_type = self._qiime_type_ if qiime_type is None else qiime_type
        return self._with_future(future, alias_type)

    def _with_future(self, future, qiime_type):
        return self.__class__(future, None, qiime_type)

    @property
    def _archiver(self):
        return self.result()._archiver

    @property
    def type(self):
        if self._qiime_type_ is not None:
            return self._qiime_type_

        return self.result().type

    @property
    def uuid(self):
        return self._archiver.uuid

    @property
    def format(self):
        from rachis.sdk import PluginManager

        pm = PluginManager()
        return pm.get_directory_format(self.type)

    @property
    def citations(self):
        return self._archiver.citations

    def result(self):
        return self._get_element_(self._future_.result())

    def _get_element_(self, results):
        """Get the result we want off of the future we have
        """
        from rachis.sdk import Results

        if isinstance(results, Results):
            return getattr(results, self._selector_)

        return results

    def export_data(self, output_dir):
        return self.result().export_data(output_dir)

    def save(self, filepath, ext=None):
        """Blocks then calls save on the result.
        """
        return self.result().save(filepath, ext=ext)

    def validate(self, level=NotImplemented):
        return self.result().validate(level=level)


class ProxyArtifact(ProxyResult):
    """This represents a future Artifact that is being returned by a Parsl app
    """
    def _view(self, view_type, recorder):
        return self.result()._view(view_type, recorder)

    def view(self, view_type):
        """If we want to view the result we need the future to be resolved
        """
        return self.result().view(view_type)

    def has_metadata(self):
        from rachis import Metadata

        from_type = transform.ModelType.from_view_type(self.format)
        to_type = transform.ModelType.from_view_type(Metadata)
        return from_type.has_transformation(to_type)

    def validate(self, level='max'):
        self.result().validate(level=level)


class ProxyVisualization(ProxyResult):
    """This represents a future Visualization that is being returned by a Parsl
       app
    """
    def get_index_paths(self, relative=True):
        return self.result().get_index_paths(relative=relative)


class ProxyResultCollection(Proxy):
    """This represents a collection of future results being returned by a Parsl
       app
    """
    def __init__(self, future, selector, qiime_type=None):
        self._future_ = future
        self._selector_ = selector
        self._qiime_type_ = qiime_type

    def __len__(self):
        return len(self.collection)

    def __iter__(self):
        yield self.collection.__iter__()

    def __setitem__(self, key, item):
        self.collection[key] = item

    def __getitem__(self, key):
        return self.collection[key]

    def __repr__(self):
        return f"<{self.__class__.__name__.lower()}: {self.type}>"

    @property
    def type(self):
        # I'm not a huge fan of the fact that this may or may not need to
        # block. If this is a return from an action (which it basically
        # always will be) we don't need to block for type. Otherwise we do.
        if self._qiime_type_ is not None:
            return self._qiime_type_

        return self.result().type

    @property
    def collection(self):
        return self.result().collection

    def save(self, directory):
        """Blocks then calls save on the result.
        """
        return self.result().save(directory)

    def keys(self):
        return self.collection.keys()

    def values(self):
        return self.collection.values()

    def items(self):
        return self.collection.items()

    def _get_element_(self, results):
        """Get the result we want off of the future we have
        """
        if self._selector_ is None:
            return results
        return getattr(results, self._selector_)

    def result(self):
        return self._get_element_(self._future_.result())


class ProxyResults(Proxy):
    """This represents future results that are being returned by a Parsl app
    """
    def __init__(self, future, signature):
        """We have the future results and the outputs portion of the signature
        of the action creating the results
        """
        self._future_ = future
        self._signature_ = signature
        self._named_ = None

    @classmethod
    def from_named(cls, proxies, signature):
        from rachis.sdk.results import Results

        names = tuple(signature)
        joined = after_all(
            (result_future(proxies[name]) for name in names),
            lambda *outputs: Results(names, outputs))
        results = cls(joined, signature)
        results._named_ = proxies
        return results

    def __iter__(self):
        """Give us a ProxyArtifact for each result in the future
        """
        for s in self._signature_:
            yield self._create_proxy(s)

    def __getattr__(self, attr):
        """Get a particular ProxyArtifact out of the future
        """
        return self._create_proxy(attr)

    def __getitem__(self, index):
        return self._create_proxy(list(self._signature_.keys())[index])

    def __repr__(self):
        lines = []
        lines.append('%s (name = value)' % self.__class__.__name__)
        lines.append('')

        max_len = -1
        for field in self._signature_:
            if len(field) > max_len:
                max_len = len(field)

        for field, value in zip(self._signature_, self):
            field_padding = ' ' * (max_len - len(field))
            lines.append('%s%s = %r' % (field, field_padding, value))

        max_len = -1
        for line in lines:
            if len(line) > max_len:
                max_len = len(line)
        lines[1] = '-' * max_len

        return '\n'.join(lines)

    def __eq__(self, other):
        """ Overriding the one on Proxy because we have _result not result
        """
        return self._result() == other._result()

    def _asdict(self):
        return self._result()._asdict()

    def _result(self):
        """If you are calling an action in a try-except block in a pipeline,
           you need to call this method on the Results object returned by the
           action.

           This is because if the Pipeline was executed with parsl, we need to
           block on the action in the try-except to ensure we get the result
           and raise the potential exception while we are still inside of the
           try-except. Otherwise we would just get the exception whenever the
           future resolved which would likely be outside of the try-except, so
           the exception would be raised and not caught.

           If you call an action in the Python API using parsl inside of a
           context manager (a withed in Cache for instance) you also must call
           this method there to ensure you get you don't start using a
           different cache/pool/whatever before your future resolves.
        """
        return self._future_.result()

    def _create_proxy(self, selector):
        """Create a ProxyResult for the element of the ProxyResults being
           requested
        """
        if self._named_ is not None:
            return self._named_[selector]
        qiime_type = self._signature_[selector].qiime_type
        return output_proxy(self._future_, selector, qiime_type)


def output_proxy(future, selector, qiime_type):
    if is_collection_type(qiime_type):
        return ProxyResultCollection(future, selector, qiime_type)
    if is_visualization_type(qiime_type):
        return ProxyVisualization(future, selector, qiime_type)
    return ProxyArtifact(future, selector, qiime_type)
