# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from rachis.sdk.iresult import IResult
from rachis.sdk.futures import join, result_as_future

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


def contains_proxy(value):
    from rachis.sdk.result import ResultCollection

    if isinstance(value, Proxy):
        return True
    if isinstance(value, (dict, ResultCollection)):
        return any(contains_proxy(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_proxy(item) for item in value)
    return False


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
        """Create the alias once the producing result is available."""
        if self._qiime_type_ is not None:
            self._assert_alias_type_refines_realized_type(
                qiime_type, self._qiime_type_)

        future = join(
            (result_as_future(self),),
            lambda result: result._alias(name, provenance, ctx, qiime_type),
            ctx=ctx)
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
    def __init__(self, future, selector, qiime_type=None, *,
                 coordinate_index=None):
        super().__init__(future, selector, qiime_type)
        self._coordinate_index = coordinate_index

    def _with_future(self, future, qiime_type):
        index = self._coordinate_index
        if index is not None and qiime_type != self.type:
            from rachis.sdk import PluginManager

            manager = PluginManager()
            if manager.get_axes(qiime_type) != manager.get_axes(self.type):
                index = None
        return type(self)(future, None, qiime_type, coordinate_index=index)

    def _view(self, view_type, recorder):
        return self.result()._view(view_type, recorder)

    def view(self, view_type, **coordinate):
        """If we want to view the result we need the future to be resolved
        """
        return self.result().view(view_type, **coordinate)

    def axes(self):
        return self.result().axes()

    def coords(self, *axes):
        return self.result().coords(*axes)

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
        # Pipeline aliases have their own future for the collection.
        if self._selector_ is None:
            return results
        return getattr(results, self._selector_)

    def result(self):
        return self._get_element_(self._future_.result())


def output_proxy(future, selector, qiime_type, *, coordinate_index=None):
    if is_collection_type(qiime_type):
        return ProxyResultCollection(future, selector, qiime_type)
    if is_visualization_type(qiime_type):
        return ProxyVisualization(future, selector, qiime_type)
    return ProxyArtifact(future, selector, qiime_type,
                         coordinate_index=coordinate_index)
