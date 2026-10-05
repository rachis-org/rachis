# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from parsl.app.app import python_app

from rachis.sdk.action import Pipeline
from rachis.sdk.proxy import Proxy, ProxyResults
from rachis.sdk.parallel_config import PARALLEL_CONFIG

from .base import Context


def _map_arg(arg, futures):
    """ Map a proxy artifact for input to a parsl action
    """
    # We add this future to the list and create a new proxy with its index as
    # its future.
    if isinstance(arg, Proxy):
        futures.append(arg._future_)
        mapped = arg.__class__(len(futures) - 1, arg._selector_)
    # We do the above but for all elements in the collection
    elif isinstance(arg, list):
        mapped = [_map_arg(proxy, futures) for proxy in arg]
    elif isinstance(arg, dict):
        mapped = {key: _map_arg(proxy, futures) for key, proxy in arg.items()}
    # We just have a real artifact and don't need to map
    else:
        mapped = arg

    return mapped


def _unmap_arg(arg, inputs):
    """ Unmap a proxy artifact given to a parsl action
    """
    # We were hacky and set _future_ to be the index of this artifact in the
    # inputs list
    if isinstance(arg, Proxy):
        resolved_result = inputs[arg._future_]
        unmapped = arg._get_element_(resolved_result)
    # If we got a collection of proxies as the input we were even hackier and
    # added each proxy to the inputs list individually while having a list of
    # their indices in the args.
    elif isinstance(arg, list):
        unmapped = [_unmap_arg(proxy, inputs) for proxy in arg]
    elif isinstance(arg, dict):
        unmapped = {key: _unmap_arg(proxy, inputs) for
                    key, proxy in arg.items()}
    # We didn't have a proxy at all
    else:
        unmapped = arg

    return unmapped


def _contains_proxies(*args, **kwargs):
    """Returns True if any of the args or kwargs are proxies
    """
    return any(isinstance(arg, Proxy) for arg in args) \
        or any(isinstance(value, Proxy) for
               value in kwargs.values())


class ParallelContext(Context):
    def __init__(self, action_obj, parent=None, *, record_provenance=True):
        super(ParallelContext, self).__init__(
            action_obj, parent=parent, record_provenance=record_provenance
        )

        if parent is not None:
            self.action_executor_mapping = parent.action_executor_mapping
            self.executor_name_type_mapping = parent.executor_name_type_mapping
        else:
            self.action_executor_mapping = \
                PARALLEL_CONFIG.action_executor_mapping
            self.executor_name_type_mapping = \
                None if PARALLEL_CONFIG.parallel_config is None \
                else {v.label: v.__class__.__name__
                      for v in PARALLEL_CONFIG.parallel_config.executors}

    def _callable_action_(self, *args, **kwargs):
        # If we have a named_pool, we need to check for cached results that
        # we can reuse.
        #
        # We can short circuit our index checking if any of our arguments
        # are proxies because if we got a proxy as an argument, we know it
        # is a new thing we are computing from a prior step in the pipeline
        # and thus will not be cached.
        if self.cache.named_pool is not None and \
                not _contains_proxies(*args, **kwargs) and \
                (cached_results := self._check_cache(args, kwargs)):
            return cached_results

        # If we didn't have cached results to reuse, we need to execute
        # the action.
        return self._dispatch_(*args, **kwargs)

    def _dispatch_(self, *args, **kwargs):
        # We need to bind this action with a child context to indicate that it
        # is not the root pipeline. This is particularly important to parallel
        # pipelines because the root pipeline needs to wait for its returns
        # to resolve while the children do not.
        futures = []
        mapped_args = []
        mapped_kwargs = {}

        # Parsl will queue up apps with futures as their arguments then not
        # execute the apps until the futures are resolved. This is an extremely
        # handy feature, but QIIME 2 does not play nice with it out of the box.
        # You can look in rachis/sdk/proxy.py for some more details on how this
        # is working, but we are basically taking future QIIME 2 results and
        # mapping them to the correct inputs in the action we are trying to
        # call. This is necessary if we are running a pipeline in particular
        # because the inputs to the next action could contain outputs from the
        # last action that might not be resolved yet because Parsl may be
        # queueing the next action before the last one has completed.
        for arg in args:
            mapped = _map_arg(arg, futures)
            mapped_args.append(mapped)

        for key, value in kwargs.items():
            mapped = _map_arg(value, futures)
            mapped_kwargs[key] = mapped

        # If the user specified a particular executor for a this action
        # determine that here
        if self.action_obj.plugin_id in self.action_executor_mapping:
            executor = self.action_executor_mapping[
                self.action_obj.plugin_id].get(self.action_obj.id, 'default')
        else:
            executor = 'default'

        execution_ctx = {'type': 'parsl'}

        # This a closure so we can change its name to our action name with
        # impunity
        def _run_parsl_action(ctx, execution_ctx, mapped_args,
                              mapped_kwargs, inputs=[]):
            """This is what the parsl app itself actually runs. It's basically
            just a wrapper around our QIIME 2 action. When this is initially
            called, args and kwargs may contain proxies that reference futures
            in inputs. By the time this starts executing, those futures will
            have resolved. We then need to take the resolved inputs and map the
            correct parts of them to the correct args/kwargs before calling the
            action with them.

            This is necessary because a single future in inputs will resolve
            into a Results object. We need to take singular Result objects off
            of that Results object and map them to the correct inputs for the
            action we want to call.
            """
            args = []
            for arg in mapped_args:
                unmapped = _unmap_arg(arg, inputs)
                args.append(unmapped)

            kwargs = {}
            for key, value in mapped_kwargs.items():
                unmapped = _unmap_arg(value, inputs)
                kwargs[key] = unmapped

            # We with in the cache here to make sure archiver.load* puts things
            # in the right cache
            with ctx.cache:
                exe = ctx.action_obj._bind(lambda: self, execution_ctx)
                results = exe(*args, **kwargs)

                return results

        # Set the name of the closure to the name of the action, so we see the
        # correct name in the parsl log
        self.action_obj._set_wrapper_name(
            _run_parsl_action, self.action_obj.id)

        if isinstance(self.action_obj, Pipeline):
            # Nested pipelines are not run as a parsl app at all, they run as a
            # normal python function that will call more python_apps. This
            # means any blocking operation within a pipeline itself will block
            # the entire pipeline
            execution_ctx['parsl_type'] = 'DFK'
            exe = self.action_obj._bind(lambda: self, execution_ctx)
            results = exe(*args, **kwargs)

            return results
        else:
            # This guard is done here because we are about to attempt to access
            # an executor which will fail out with an obscure error if no
            # config was loaded
            if PARALLEL_CONFIG.parallel_config is None:
                raise ValueError('You must load a parallel config before '
                                 'running in parallel.')

            execution_ctx['parsl_type'] = \
                self.executor_name_type_mapping[executor]
            future = python_app(
                executors=[executor])(
                    _run_parsl_action)(self, execution_ctx,
                                       mapped_args, mapped_kwargs,
                                       inputs=futures)

        collated_input = self.action_obj.signature.collate_inputs(
            *args, **kwargs)
        output_types = self.action_obj.signature.solve_output(**collated_input)

        # Again, we return a set of futures not a set of real results
        return ProxyResults(future, output_types)
