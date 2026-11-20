# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from types import MappingProxyType

from parsl.app.app import python_app

from rachis.sdk.action import Pipeline
from rachis.sdk.proxy import Proxy, ProxyResults
from rachis.sdk.parallel_config import PARALLEL_CONFIG
from rachis.sdk.util import Sievable

from .base import Context


def _sieve_inputs(frame, types):
    arguments = frame.bound_arguments
    inputs = frame.action.signature.inputs
    selected, rebuild = Sievable.sieve(
        {name: value for name, value in arguments.items() if name in inputs},
        isinstance=types,
    )
    return selected, lambda replacements: arguments | rebuild(replacements)


def _map_arguments(frame, futures):
    """Encode proxies as indexes into the app's dependency list."""
    proxies, rebuild = _sieve_inputs(frame, Proxy)

    def encode(proxy):
        index = len(futures)
        futures.append(proxy._future_)
        return proxy.__class__(index, proxy._selector_,
                               getattr(proxy, '_qiime_type_', None))

    return rebuild(map(encode, proxies))


def _unmap_arguments(frame, inputs):
    """Select the worker's inputs from Parsl's resolved dependencies."""
    proxies, rebuild = _sieve_inputs(frame, Proxy)
    return rebuild(
        proxy._get_element_(inputs[proxy._future_]) for proxy in proxies
    )


def _submit_app(frame, executor):
    futures = []
    worker_frame = frame.for_worker()
    worker_frame.bound_arguments = _map_arguments(frame, futures)

    def worker(frame, inputs=()):
        with frame.ctx.cache:
            frame.bound_arguments = _unmap_arguments(frame, inputs)
            return frame()

    frame.action._set_wrapper_name(worker, frame.action.id)
    return python_app(executors=[executor])(worker)(
        worker_frame, inputs=futures)


class ParallelContext(Context):
    execution_ctx = MappingProxyType({'type': 'parsl'})

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

    def _dispatch_action_(self, frame):
        # Pipeline bodies keep coordinating locally, as before this refactor.
        # Their child applications still pass through their own outer stacks.
        if isinstance(frame.action, Pipeline):
            self.execution_ctx = {'type': 'parsl', 'parsl_type': 'DFK'}
            return frame()

        if PARALLEL_CONFIG.parallel_config is None:
            raise ValueError('You must load a parallel config before '
                             'running in parallel.')

        executor = self.action_executor_mapping.get(
            frame.action.plugin_id, {}).get(frame.action.id, 'default')
        self.execution_ctx = {
            'type': 'parsl',
            'parsl_type': self.executor_name_type_mapping[executor],
        }

        return ProxyResults(_submit_app(frame, executor), frame.output_types)
