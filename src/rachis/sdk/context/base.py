# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------
import warnings

from rachis.plugin import IContext
from rachis.core.type.util import is_collection_type
from rachis.core.type import HashableInvocation
from rachis.core.cache import get_cache
import rachis.sdk


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


class Context(IContext):
    def __init__(self, action_obj=None, parent=None):
        if parent is not None:
            self.cache = parent.cache
        else:
            self.cache = get_cache()
            # Only ever do this on the root context. We only want to index the
            # pool once before we start adding our own stuff to it.
            if self.cache.named_pool is not None:
                if self.cache.CURRENT_FORMAT_VERSION == 'v2':
                    self.cache.named_pool.create_index()
                else:
                    with self.cache.lock:
                        self.cache.named_pool.create_index()

        if action_obj is None and parent is not None:
            raise ValueError('Only parentless contexts can be instantiated '
                             'without an action_obj')

        self.action_obj = action_obj
        self._parent = parent
        if self.cache.CURRENT_FORMAT_VERSION == 'v2':
            self.scope = None

    def _execution_context(self):
        if self.cache.CURRENT_FORMAT_VERSION != 'v2':
            return self
        from copy import copy
        ctx = copy(self)
        owner = (self._parent.scope if self._parent is not None
                 else self.cache.scope)
        ctx.scope = owner.child()
        return ctx

    def _adopt_outputs(self, outputs):
        if self.cache.CURRENT_FORMAT_VERSION != 'v2':
            return
        owner = (self._parent.scope if self._parent is not None
                 else self.cache.root_scope)
        def adopt(value):
            if isinstance(value, rachis.sdk.IResult):
                archiver = value._archiver
                if hasattr(archiver, 'ref_id'):
                    owner.adopt(archiver.ref_id)
            elif isinstance(value, dict) or hasattr(value, 'values'):
                for child in value.values():
                    adopt(child)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    adopt(child)
        adopt(outputs)

    def _dispatch_(self, args, kwargs):
        exe = self.action_obj._bind(lambda: self)
        results = exe(*args, **kwargs)

        return results

    def _callable_action_(self, *args, **kwargs):
        # If we have a named_pool, we need to check for cached results that
        # we can reuse.
        if self.cache.named_pool is not None and \
                (cached_results := self._check_cache(args, kwargs)):
            return cached_results

        # If we didn't have cached results to reuse, we need to execute
        # the action.
        return self._dispatch_(args, kwargs)

    def _check_cache(self, args, kwargs):
        plugin = self.action_obj.plugin_id.replace('_', '-')
        plugin_action = f'{plugin}:{self.action_obj.id}'

        # Type management for inputs
        collated_inputs = self.action_obj.signature.collate_inputs(
            *args, **kwargs)
        callable_args = self.action_obj.signature.coerce_user_input(
            **collated_inputs)

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

        # Create a context for the new action
        child_context = self.__class__(new_action_obj, parent=self)

        # Return a callable for the new action
        callable_action = child_context.action_obj._rewrite_wrapper_signature(
            child_context._callable_action_)
        child_context.action_obj._set_wrapper_properties(callable_action)
        return callable_action


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
        if self.cache.CURRENT_FORMAT_VERSION == 'v2':
            new_ref = self.cache.retain_result(ref)
            owner = self.scope
            if owner is None or owner.closed:
                owner = (self._parent.scope if self._parent is not None
                         and not self._parent.scope.closed
                         else self.cache.root_scope)
            owner.adopt(new_ref._archiver.ref_id)
            if self.cache.named_pool is not None:
                self.cache.named_pool.save(new_ref)
            return new_ref
        with self.cache.lock:
            new_ref = self.cache.process_pool.save(ref)

            if self.cache.named_pool is not None:
                self.cache.named_pool.save(new_ref)

        # Return an artifact backed by the data in the cache
        return new_ref
