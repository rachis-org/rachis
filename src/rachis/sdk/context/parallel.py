# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from concurrent.futures import Future
from contextlib import ExitStack
from copy import copy
from types import MappingProxyType

from parsl.app.app import join_app, python_app
from parsl.dataflow.errors import JoinError

from rachis.sdk.action import Pipeline
from rachis.sdk.futures import after_all, result_as_future
from rachis.sdk.mapped import MappedMethod
from rachis.sdk.proxy import Proxy, output_proxy
from rachis.sdk.parallel_config import PARALLEL_CONFIG
from rachis.sdk.results import Results
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
    frame.bound_arguments = _map_arguments(frame, futures)

    def worker(frame, inputs=()):
        with frame.ctx.cache:
            frame.bound_arguments = _unmap_arguments(frame, inputs)
            return frame()

    frame.action._set_wrapper_name(worker, frame.action.id)
    return python_app(executors=[executor])(worker)(frame, inputs=futures)


def _submit_batch(cells, executor):
    futures = []
    frames = tuple(cells)
    for frame in frames:
        frame.bound_arguments = _map_arguments(frame, futures)

    def worker(frames, inputs=()):
        outcomes = []
        with frames[0].ctx.cache:
            for frame in frames:
                frame.bound_arguments = _unmap_arguments(frame, inputs)
                try:
                    outcomes.append(frame())
                except Exception as error:
                    outcomes.append(error)
        return outcomes

    frames[0].action._set_wrapper_name(worker, frames[0].action.id)
    return python_app(executors=[executor])(worker)(frames, inputs=futures)


def _join_exception(job):
    error = job.exception()
    if isinstance(error, JoinError):
        error, _ = error.dependent_exceptions_tids[0]
    return error


def _submit_coordinator(body, coordinate_inputs):
    results = Results(body.output_types, (
        output_proxy(Future(), None, spec.qiime_type)
        for spec in body.output_types.values()
    ))
    outputs = Results._futures(results)
    completion = Future()
    caller_ctx = body.ctx
    body.ctx = copy(caller_ctx)
    body.ctx._parent = caller_ctx

    def fail_outputs(error):
        for future in outputs:
            if not future.done():
                future.set_exception(error)

    def run(*sources):
        # Child work belongs to the body, which can submit more actions after
        # the caller has started waiting on its own scope.
        children = []
        body.bound_arguments = body.bound_arguments | dict(
            zip(coordinate_inputs, sources, strict=True),
        )
        with ExitStack() as cleanup:
            body.ctx._scopes.append((children, cleanup))
            try:
                with body.ctx.cache:
                    returned = body()
            except BaseException as error:
                fail_outputs(error)
            else:
                for value, output in zip(returned, outputs):
                    result_as_future(value, into=output)
            finally:
                body.ctx._scopes.pop()
                body.ctx._defer_cleanup(cleanup)

        return [*outputs, *children]

    def finish(job):
        error = _join_exception(job)
        if error is None:
            completion.set_result(None)
        else:
            # A launch failure leaves nobody to settle the outputs.
            fail_outputs(error)
            completion.set_exception(error)

    body.action._set_wrapper_name(run, body.action.id)
    join_app(run)(*coordinate_inputs.values()).add_done_callback(finish)
    caller_ctx._track_results(completion)
    return results


class ParallelContext(Context):
    execution_metadata = MappingProxyType({'type': 'parsl'})

    def __init__(self, action_obj=None, parent=None):
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
        if self.executor_name_type_mapping is None:
            raise ValueError('You must load a parallel config before '
                             'running in parallel.')

        if isinstance(frame.action, Pipeline):
            self.execution_metadata = {'type': 'parsl', 'parsl_type': 'DFK'}
            return _submit_coordinator(frame, {})

        executor = self._get_executor(frame.action)
        self.execution_metadata = {
            'type': 'parsl',
            'parsl_type': self.executor_name_type_mapping[executor],
        }

        if isinstance(frame.action, MappedMethod):
            coordinates = {}
            for name in frame.action.signature.input_patterns:
                value = frame.bound_arguments[name]
                coordinates[name] = (result_as_future(value)
                                     if isinstance(value, Proxy) else value)
            return _submit_coordinator(frame, coordinates)

        # Parsl resolves upstream dependencies before the worker applies them.
        return Results._from_future(
            _submit_app(frame, executor), frame.output_types,
        )

    def _get_executor(self, action):
        return self.action_executor_mapping.get(
            action.plugin_id, {}).get(action.id, 'default')

    def _dispatch_cells_(self, cells):
        if not cells:
            return {}
        action = next(iter(cells.values())).action
        job = _submit_batch(cells.values(), self._get_executor(action))

        def read_cell(values, index):
            outcome = values[0][index]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        return {
            coord: after_all((job,), lambda values, index=index:
                             read_cell(values, index))
            for index, coord in enumerate(cells)
        }
