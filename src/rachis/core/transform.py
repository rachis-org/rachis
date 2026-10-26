# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------
from __future__ import annotations

from enum import Enum
import pathlib
from typing import Callable, TYPE_CHECKING
if TYPE_CHECKING:
    from rachis.plugin.plugin import TransformerRecord

from rachis import sdk
from rachis.plugin import model
from rachis.core import util


class ModelType:
    @staticmethod
    def from_view_type(view_type):
        if issubclass(view_type, model.base.FormatBase):
            return FormatType(view_type)
        else:
            # TODO: supporting stdlib.typing may require an alternate
            # model type as `isinstance` is a meaningless operation
            # for them so validation would need to be handled differently
            return ObjectType(view_type)

    def __init__(self, view_type):
        self._pm = sdk.PluginManager()
        self._view_type = view_type
        self._view_name = util.get_view_name(self._view_type)
        self._record = None

        if self._view_name in self._pm.views:
            self._record = self._pm.views[self._view_name]

    def make_transformation(self, other, recorder=None):
        target_node = find_transformation_path(
            self._view_type, other._view_type
        )

        if target_node is None:
            raise Exception("No transformation from %r to %r" %
                            (self._view_type, other._view_type))

        return compose_transformation(target_node, recorder=recorder)

    def has_transformation(self, other):
        """
        Checks to see if there exists a transformation to `other`.

        Parameters
        ----------
        other : ModelType subclass
            The type to check for transformation to.

        Returns
        -------
        bool
            Does a transformation path to `other` exist?
        """
        path = find_transformation_path(self._view_type, other._view_type)
        return path is not None

    def coerce_view(self, view):
        return view

    def set_user_owned(self, view, value):
        pass


class FormatType(ModelType):
    def coerce_view(self, view):
        if type(view) is str or isinstance(view, pathlib.Path):
            return self._view_type(view, mode='r')

        if isinstance(view, self._view_type):
            # wrap original path (inheriting the lifetime) and return a
            # read-only instance
            return self._view_type(view.path, mode='r')

        return view

    def validate(self, view, level='min'):
        if not isinstance(view, self._view_type):
            raise TypeError("%r is not an instance of %r."
                            % (view, self._view_type))
        # Formats have a validate method, so defer to it
        view.validate(level)

    def set_user_owned(self, view, value):
        view.path._user_owned = value


class ObjectType(ModelType):
    def validate(self, view, level=None):
        if not isinstance(view, self._view_type):
            raise TypeError("%r is not of type %r, cannot transform further."
                            % (view, self._view_type))


class TransformType(Enum):
    '''
    Annotates a `SearchNode` as being transformed from its parent in one of
    three ways:

    - `registered` means that the transformation was a typical, registered
      transformer
    - `wrap` means that the transformation converts a `FormatBase` into a
      `SingleFileDirectoryFormatBase`
    - `unwrap` type means that the transformation converts a
      `SingleFileDirectoryFormatBase` into a `FormatBase`
    '''
    registered = 1
    wrap = 2
    unwrap = 3


class PathType(Enum):
    '''
    Annotates the path encoded in a `SearchNode` as belonging to one of three
    categories:
        - upgrade_only: only implicit and `upgrade=True` transformations
        - includes_false: implicit, `upgrade=True`, and `upgrade=False`
          transformations
        - includes_none: implicit, `upgrade=True`, `upgrade=False` and
          `upgrade=None` transformations
    '''
    upgrade_only = 1
    includes_false = 2
    includes_none = 3


class SearchNode:
    def __init__(
        self,
        type_: type,
        parent: SearchNode | None = None,
        record: TransformerRecord | None = None,
        transform_type: TransformType | None = None,
    ):
        '''
        Parameters
        ----------
        type_ : type
            The type of the node.
        parent : SearchNode | None
            The node from which this node has been transformed, or None if this
            is the first node in the path.
        record : TransformerRecord | None
            The `TransformerRecord` as registered in `Plugin.transformers` when
            the transformation from parent to self was registered, or None for
            wrap/unwrap transformations.
        transform_type : TransformType | None
            See `TransformType`. None if node is starting node in search.
        '''
        self.type_ = type_
        self.parent = parent
        self.record = record
        self.transform_type = transform_type

    def __len__(self):
        '''
        Returns the number of registered transformers up to this node.
        '''
        return len(self.steps(explicit=True))

    def __repr__(self):
        return (
            f'SearchNode(id={id(self)}, type_={repr(self.type_)}, '
            f'parent={None if self.parent is None else id(self.parent)}, '
            f'transform_type={self.transform_type})'
        )

    def classify(self) -> PathType:
        '''
        Classify the path encoded in a `SearchNode` as one of `PathType`.
        '''
        status = PathType.upgrade_only
        node = self
        while node is not None:
            if node.record is not None and node.record.upgrade is False:
                status = PathType.includes_false
            elif node.record is not None and node.record.upgrade is None:
                status = PathType.includes_none
                return status

            node = node.parent

        return status

    def validate_path(self) -> bool:
        '''
        Validates the transformation path encoded in the chain of parents.
        Ensures that there is at most one `upgrade=None` transformation step
        which, if present, occurs at one of the ends of the path. Implicit
        transformations (defined elsewhere) are not considered when determining
        the ends of the path.

        Accounting for `None`s is done separately here because it is
        impractical to look ahead or look backwards when managing the queue in
        `NodeQueue`.

        Returns
        -------
        bool
            Whether the path is valid.
        '''
        explicit_steps = self.steps(explicit=True)
        none_count = 0
        for i, n in enumerate(explicit_steps):
            if n.record.upgrade is None:
                none_count += 1
                if i not in {0, len(explicit_steps) - 1}:
                    return False

        if none_count > 1:
            return False

        return True

    def steps(self, explicit=False) -> list[SearchNode]:
        '''
        Converts the ancestors of self into a list of `SearchNodes`s.

        Parameters
        ----------
        explicit : bool
            Whether to include only explicit transformation steps (i.e. those
            that are registered).

        Returns
        -------
        list[SearchNode]
            The `SearchNode` ancestry.
        '''
        steps = []
        node = self
        while node is not None:
            steps.insert(0, node)
            node = node.parent

        if explicit:
            return [n for n in steps if n.record is not None]

        return steps

    def has_ancestor(self, node: SearchNode) -> bool:
        '''
        Searches for the type of `node` among the ancestors of `self`. Used to
        prevent cycles during the transformation path search.

        Paremeters
        ----------
        node : SearchNode
            The node the type of which will be searched for among ancestors of
            `self`.

        Returns
        -------
        bool
            Whether a matching ancestor exists.
        '''
        current = self
        while current is not None:
            if current.type_ == node.type_:
                return True

            current = current.parent

        return False


class NodeQueue:
    def __init__(self):
            self.nodes = []

    def push(self, node: SearchNode) -> None:
        '''
        Inserts a node and resorts the queue.

        The queue is sorted primarily by `PathType` and secondarily by
        path length. In both cases lower values are preferred. This ensures
        that `upgrade=True`-only paths are exhausted before including
        `upgrade=False` steps, and so on.
        '''
        self.nodes.append(node)

        def primary(node):
            return int(node.classify().value)

        def secondary(node):
            return len(node)

        def tertiary(node):
            return len(node.steps())

        self.nodes.sort(
            key=lambda n: (primary(n), secondary(n), tertiary(n)),
            reverse=True,
        )

    def pop(self) -> SearchNode | None:
        if not self.nodes:
            return None

        return self.nodes.pop()

    def insert_neighbors(self, node: SearchNode) -> None:
        '''
        Find explicit and implicit neighbors to `node` and add them to the
        queue.

        Parameters
        ----------
        node : SearchNode
            The node the neighbors of which should be added.
        node_queue : NodeQueue
            The remaining nodes to search while finding a transformation path.
            Neighbors are pushed into this queue.
        '''
        pm = sdk.PluginManager()

        # explicit neighbors
        for neighbor, transform_record in pm.transformers.get(
            node.type_, {}
        ).items():
            neighbor = SearchNode(
                type_=neighbor,
                parent=node,
                record=transform_record,
                transform_type=TransformType.registered
            )
            if not node.has_ancestor(neighbor):
                self.push(neighbor)

        # add synthetic link for Dx -> x
        if issubclass(node.type_, model.SingleFileDirectoryFormatBase):
            neighbor = SearchNode(
                type_=node.type_.file.format,
                parent=node,
                record=None,
                transform_type=TransformType.unwrap,
            )
            if not node.has_ancestor(neighbor):
                self.push(neighbor)

        # add synthetic link(s) x -> Dx
        elif issubclass(node.type_, model.base.FormatBase):
            for sfdf in pm._ff_to_sfdf.get(node.type_, []):
                neighbor = SearchNode(
                    type_=sfdf,
                    parent=node,
                    record=None,
                    transform_type=TransformType.wrap,
                )
                if not node.has_ancestor(neighbor):
                    self.push(neighbor)


def find_transformation_path(start: type, target: type) -> SearchNode | None:
    '''
    Searches for a transformation path from `start` to `target`. The path is
    encoded in the chain of parents of the returned `SearchNode`.

    Parameters
    ----------
    start : type
        The type we wish to transform from.
    target : type
        The type we wish to transform to.

    Returns
    -------
    SearchNode | None
        A SearchNode of the target type, if reachable, otherwise None.
    '''
    current = SearchNode(type_=start)

    node_queue = NodeQueue()
    node_queue.push(current)
    while True:
        current = node_queue.pop()

        if current is None:
            return None

        if not current.validate_path():
            continue

        if current.type_ == target:
            return current

        node_queue.insert_neighbors(current)


def compose_transformation(
    target: SearchNode,
    recorder: Callable | None = None,
) -> Callable:
    '''
    Records transformation provenance and composes a transformer closure
    that ties together one or more individual transformations.

    Parameters
    ----------
    target : SearchNode
        The type that we wish to transform to. The transformation path is
        encoded in its ancestors.
    recorder : Callable | None
        The transformation provenance recording function. None if caller does
        not care about provenance, e.g. `Artifact.view`.

    Returns
    -------
    Callable
        The composed transformer closure.
    '''
    pm = sdk.PluginManager()

    steps = target.steps()

    if recorder is not None:
        registered_indices = [
            i for i, node in enumerate(steps)
            if node.transform_type is TransformType.registered
        ]

        if registered_indices:
            recorded_steps = []
            start = 0
            for index in registered_indices:
               recorded_steps.append(
                   (start, index, steps[index].record)
               )
               start = index

            # the last entry must point to the last type in the chain, even
            # if not registered
            last = recorded_steps[-1]
            amended = (last[0], len(steps) - 1, last[2])
            recorded_steps[-1] = amended
        else:
            # a path of only implicit transformations has one entry, no record
            recorded_steps = [(0, len(steps) - 1, None)]

        for start, end, record in recorded_steps:
            input_name = util.get_view_name(steps[start].type_)
            output_name = util.get_view_name(steps[end].type_)
            recorder(
                record,
                input_name=input_name,
                input_record=pm.views.get(input_name),
                output_name=output_name,
                output_record=pm.views.get(output_name),
            )

    if len(steps) == 1:
        def identity_transformation(view, validate_level='min'):
            from_mt = ModelType.from_view_type(steps[0].type_)
            current = from_mt.coerce_view(view)
            from_mt.validate(current, level=validate_level)

            return current

        return identity_transformation

    def transformation(view, validate_level='min'):
        current = view
        for i in range(len(steps) - 1):
            from_type = steps[i].type_
            to_type = steps[i + 1].type_

            if steps[i + 1].transform_type == TransformType.wrap:
                transformer = wrap_transformer(from_type, to_type)
            elif steps[i + 1].transform_type == TransformType.unwrap:
                transformer = unwrap_transformer(from_type)
            else:
                transformer = steps[i + 1].record.transformer

            from_mt = ModelType.from_view_type(from_type)
            to_mt = ModelType.from_view_type(to_type)

            current = from_mt.coerce_view(current)
            from_mt.validate(current, level=validate_level)
            current = transformer(current)

            current = to_mt.coerce_view(current)
            to_mt.validate(current, level=validate_level)

            # an unwrap aliases a file in the source directory; if we keep
            # that file user-owned then a subsequent wrap copies it instead of
            # moving it out from under a user's feet
            if steps[i + 1].transform_type is not TransformType.unwrap:
                to_mt.set_user_owned(current, False)

        return current

    return transformation


def wrap_transformer(file_type: type, sfdf_type: type):
    '''
    A transformer used to convert any `FileFormat` into its associated
    `SingleFileDirectoryFormat`.
    '''
    def transformer(view):
        sfdf = sfdf_type()
        sfdf.file.write_data(view, file_type)
        return sfdf

    return transformer


def unwrap_transformer(sfdf_type: type):
    '''
    A transformer used to convert any `SingleFileDirectoryFormat` into the
    contained `FileFormat`.
    '''
    file_type = sfdf_type.file.format

    def transformer(view):
        file_view = view.file.view(file_type)
        # Retain a reference to our OutPath on the file view so the OutPath
        # is not destroyed while the file view is still in use. The file view
        # is a file inside of an OutPath directory, and if we lose all
        # references to that OutPath, it will be destroyed by the finalizer on
        # OutPath taking its directory, and thus our file, with it.
        #
        # NOTE: I consider this solution to be a bit of a hack. I don't love
        # post facto slapping new attributes onto objects, but this is the
        # cleanest and least intrusive way to keep the OutPath alive.
        file_view.path._source_directory = view.path
        return file_view

    return transformer
