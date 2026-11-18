# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from concurrent.futures import Future
from contextlib import contextmanager
from types import MappingProxyType

from parsl.app.app import python_app

from rachis.sdk.action import Pipeline
from rachis.sdk.futures import as_future
from rachis.sdk.result import ResultCollection
from rachis.sdk.proxy import Proxy, ProxyResults
from rachis.sdk.parallel_config import PARALLEL_CONFIG

from .base import Context, cache_lookup_wrapper, invocation_resources_wrapper


def _map_arg(arg, futures):
    """ Map a proxy artifact for input to a parsl action
    """
    # We add this future to the list and create a new proxy with its index as
    # its future.
    if isinstance(arg, Proxy):
        futures.append(arg._future_)
        mapped = arg.__class__(len(futures) - 1, arg._selector_,
                               getattr(arg, '_qiime_type_', None))
    # We do the above but for all elements in the collection
    elif isinstance(arg, list):
        mapped = [_map_arg(proxy, futures) for proxy in arg]
    elif isinstance(arg, ResultCollection):
        mapped = ResultCollection({
            key: _map_arg(value, futures) for key, value in arg.items()})
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
    elif isinstance(arg, ResultCollection):
        unmapped = ResultCollection({
            key: _unmap_arg(value, inputs) for key, value in arg.items()})
    elif isinstance(arg, dict):
        unmapped = {key: _unmap_arg(proxy, inputs) for
                    key, proxy in arg.items()}
    # We didn't have a proxy at all
    else:
        unmapped = arg

    return unmapped


def _submit_app(frame, executor):
    futures = []
    worker_frame = frame.for_worker()
    worker_frame.bound_arguments = _map_arg(frame.bound_arguments, futures)

    def worker(frame, inputs=()):
        with frame.ctx.cache:
            frame.bound_arguments = _unmap_arg(frame.bound_arguments, inputs)
            return frame()

    frame.action._set_wrapper_name(worker, frame.action.id)
    return python_app(executors=[executor])(worker)(
        worker_frame, inputs=futures)


@contextmanager
def parallel_dispatch_result_wrapper(frame):
    yield
    if not isinstance(frame.results, Future):
        return
    if frame.ctx._runs_on_driver(frame):
        frame.results = frame.results.result()
    else:
        frame.results = ProxyResults(frame.results, frame.output_types)


class ParallelContext(Context):
    execution_ctx = MappingProxyType({'type': 'parsl'})
    outer_bind_wrappers = (
        invocation_resources_wrapper,
        cache_lookup_wrapper,
        parallel_dispatch_result_wrapper,
    )
    _defer_results = True

    def __init__(self, action_obj, parent=None):
        super(ParallelContext, self).__init__(action_obj, parent=parent)

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

    def _runs_on_driver(self, frame):
        return isinstance(frame.action, Pipeline)

    def _dispatch_action_(self, frame):
        # Pipeline bodies keep coordinating locally, as before this refactor.
        # Their child applications still pass through their own outer stacks.
        if self._runs_on_driver(frame):
            self.execution_ctx = {'type': 'parsl', 'parsl_type': 'DFK'}
            return as_future(frame)

        if PARALLEL_CONFIG.parallel_config is None:
            raise ValueError('You must load a parallel config before '
                             'running in parallel.')

        executor = self.action_executor_mapping.get(
            frame.action.plugin_id, {}).get(frame.action.id, 'default')
        self.execution_ctx = {
            'type': 'parsl',
            'parsl_type': self.executor_name_type_mapping[executor],
        }

        future = _submit_app(frame, executor)
        self._get_resources().jobs.append(future)
        return future
