# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import ExitStack, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
import warnings

from rachis.plugin import IContext
from rachis.core.type.util import is_collection_type
from rachis.core.type import HashableInvocation
from rachis.core.cache import get_cache
import rachis.sdk
from rachis.sdk.futures import call_future


def _validate_collection(collection_order):
    """Validate that all indexed items in the collection agree on how
    large the collection should be and that we have that many elements.
    """
    if not all([
        elem.total == collection_order[0].total
        for elem in collection_order]) \
            or len(collection_order) != collection_order[0].total:
        warnings.warn("Incomplete collection found when recycling, "
                      "collection will be remade")
        return False

    return True


@contextmanager
def cache_lookup_wrapper(frame):
    ctx = frame.ctx
    if (frame.results is None and ctx._parent is not None
            and ctx.cache.named_pool is not None):
        frame.results = ctx._check_cache(frame.bound_arguments)
    yield


@contextmanager
def invocation_resources_wrapper(frame):
    ctx = frame.ctx
    if ctx._parent is None and not ctx._entered:
        with ctx:
            yield
    else:
        yield


class InvocationResources(ExitStack):
    """Keep driver resources alive while submitted work and joins settle."""

    def __init__(self):
        super().__init__()
        self.jobs = []
        self.executor = self.enter_context(ThreadPoolExecutor(
            max_workers=1, thread_name_prefix='rachis-join'))

    def close(self):
        wait(self.jobs)
        super().close()


@contextmanager
def mapped_invocation_wrapper(frame):
    ctx = frame.ctx
    directory = TemporaryDirectory(dir=ctx.cache.path)
    if ctx._defer_results:
        frame.directory = Path(ctx._get_resources().enter_context(directory))
        yield
    else:
        with directory as path:
            frame.directory = Path(path)
            yield


class Context(IContext):
    outer_bind_wrappers = (cache_lookup_wrapper,)
    inner_apply_wrappers = ()
    outer_apply_wrappers = (mapped_invocation_wrapper,)
    mapped_inner_apply_wrappers = ()
    execution_ctx = MappingProxyType({'type': 'synchronous'})
    _defer_results = False
    _resources = None
    _entered = False

    def __init__(self, action_obj=None, parent=None):
        if parent is not None:
            self.cache = parent.cache
        else:
            self.cache = get_cache()
            # Only ever do this on the root context. We only want to index the
            # pool once before we start adding our own stuff to it.
            with self.cache.lock:
                if self.cache.named_pool is not None:
                    self.cache.named_pool.create_index()

        if action_obj is None and parent is not None:
            raise ValueError('Only parentless contexts can be instantiated '
                             'without an action_obj')

        self.action_obj = action_obj
        self._parent = parent
        self.execution_ctx = dict(type(self).execution_ctx)

    def _dispatch_action_(self, frame):
        return call_future(frame)

    def _get_resources(self):
        if self._parent is not None:
            return self._parent._get_resources()
        if self._resources is None:
            self._resources = InvocationResources()
        return self._resources

    def __enter__(self):
        self._entered = True
        return self

    def __exit__(self, *exc_details):
        if self._resources is not None:
            self._resources.close()
            self._resources = None
        self._entered = False

    def __getstate__(self):
        state = self.__dict__.copy()
        state.pop('_resources', None)
        return state

    def _dispatch_cells_(self, cells):
        """Return a future for every invocation coordinate in cells."""
        return {coord: call_future(cell) for coord, cell in cells.items()}

    def _check_cache(self, inputs):
        plugin = self.action_obj.plugin_id.replace('_', '-')
        plugin_action = f'{plugin}:{self.action_obj.id}'

        signature = self.action_obj.signature
        signature.check_types(**inputs)
        callable_args = signature.coerce_user_input(**inputs)

        # Make args and kwargs look how they do when we read them
        # out of a .yaml file (list of single value dicts of
        # input_name: value)
        arguments = []
        for k, v in callable_args.items():
            arguments.append({k: v})

        invocation = HashableInvocation(plugin_action, arguments)
        if invocation in self.cache.named_pool.index:
            # It is conceivable that since we created our index the
            # pool we indexed has been destroyed. If that is the
            # case we want to just continue on and rerun the action
            try:
                return self._load_cache(invocation)
            except KeyError:
                pass

    def _load_cache(self, invocation):
        """Load cached results
        """
        cached_outputs = self.cache.named_pool.index[invocation]
        loaded_outputs = {}

        for name, _type in self.action_obj.signature.outputs.items():
            if is_collection_type(_type.qiime_type):
                loaded_collection = rachis.sdk.ResultCollection()
                cached_collection = cached_outputs[name]

                # Get the order we should load collection items in
                collection_order = list(cached_collection.keys())
                if not _validate_collection(collection_order):
                    return None
                collection_order.sort(key=lambda x: x.idx)

                for elem_info in collection_order:
                    elem = cached_collection[elem_info]
                    loaded_elem = self.cache.named_pool.load(elem)
                    loaded_collection[
                        elem_info.item_name] = loaded_elem

                loaded_outputs[name] = loaded_collection
            else:
                output = cached_outputs[name]
                loaded_outputs[name] = \
                    self.cache.named_pool.load(output)

        return rachis.sdk.Results(
            loaded_outputs.keys(), loaded_outputs.values())

    def get_action(self, plugin: str, action: str):
        """Return a function matching the callable API of an action.
        This function is aware of the pipeline context and manages its own
        cleanup as appropriate.
        """
        plugin = plugin.replace('_', '-')

        pm = rachis.sdk.PluginManager()
        try:
            plugin_obj = pm.plugins[plugin]
        except KeyError:
            raise ValueError("A plugin named %r could not be found." % plugin)

        try:
            new_action_obj = plugin_obj.actions[action]
        except KeyError:
            raise ValueError(
                "An action named %r was not found for plugin %r"
                % (action, plugin))

        def context_factory():
            # Independent applications of the same callable need independent
            # Contexts and execution metadata, including parallel workers.
            return self.__class__(new_action_obj, parent=self)

        return new_action_obj._outer_bind(context_factory)


    def make_artifact(self, type, view, view_type=None):
        """Return a new artifact from a given view.

        This artifact is automatically tracked and cleaned by the pipeline
        context.
        """
        artifact = rachis.sdk.Artifact.import_data(type, view, view_type)
        self.add_reference(artifact)
        return artifact

    def make_report(self, template, collection, descriptions=None):
        """Create a report from visualizations and descriptions.

        Descriptions are plain text with the same keys as the collection.
        """
        viz = rachis.sdk.Visualization.make_report(
            # wait for any proxies via .result()
            template, {k: v.result() for k, v in collection.items()},
            descriptions=descriptions)
        self.add_reference(viz)
        return viz

    # NOTE: We end up with both the artifact and the pipeline alias of artifact
    # in the named cache in the end. We only have the pipeline alias in the
    # process pool
    def add_reference(self, ref):
        """Add a reference to something destructable that will be owned by the
           parent scope. The reason it needs to be tracked is so that on
           failure, a context can still identify what will (no longer) be
           returned.
        """
        with self.cache.lock:
            new_ref = self.cache.process_pool.save(ref)

            if self.cache.named_pool is not None:
                self.cache.named_pool.save(new_ref)

        # Return an artifact backed by the data in the cache
        return new_ref
