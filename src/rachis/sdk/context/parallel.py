# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from concurrent.futures import Future, wait
from contextlib import ExitStack
from copy import copy
from dataclasses import dataclass
from threading import Thread
from time import sleep
from types import MappingProxyType

from parsl.app.app import join_app, python_app
from parsl.dataflow.errors import JoinError

from rachis.sdk.action import Pipeline
from rachis.sdk.futures import after_all, as_future, result_as_future
from rachis.sdk.mapped import MappedMethod, ProxyCell, read_commit
from rachis.sdk.proxy import Proxy, ProxyArtifact, output_proxy
from rachis.sdk.parallel_config import PARALLEL_CONFIG
from rachis.sdk.results import Results
from rachis.sdk.util import Sievable

from .base import Context


@dataclass(frozen=True)
class _ResultDependency:
    index: int


def _watch_cell_commits(pending):
    """Settle each cell future as soon as its worker publishes an outcome."""
    try:
        while pending:
            for path, (cell, batch) in list(pending.items()):
                # Check completion before the file so a last-minute commit
                # can still satisfy its cell future.
                done = batch.done()
                try:
                    if path.is_file():
                        outcome = read_commit(path)
                        if isinstance(outcome, Exception):
                            raise outcome
                    elif done:
                        batch.result()
                        raise RuntimeError(f'Batch did not commit {path.name}')
                    else:
                        continue
                except Exception as error:
                    cell.set_exception(error)
                else:
                    cell.set_result(outcome)
                del pending[path]
            if pending:
                sleep(0.01)
    finally:
        for cell, _ in pending.values():
            if not cell.done():
                cell.set_exception(RuntimeError('Cell watcher closed'))


def _sieve_inputs(frame, types):
    arguments = frame.bound_arguments
    inputs = frame.action.signature.inputs
    selected, rebuild = Sievable.sieve(
        {name: value for name, value in arguments.items() if name in inputs},
        isinstance=types,
    )
    return selected, lambda replacements: arguments | rebuild(replacements)


def _map_arguments(frame, futures):
    """Encode references as indexes into the app's dependency list."""
    references, rebuild = _sieve_inputs(frame, (Proxy, ProxyCell))

    def encode(reference):
        index = len(futures)
        future = (reference.future if isinstance(reference, ProxyCell)
                  else result_as_future(reference))
        futures.append(future)
        return _ResultDependency(index)

    return rebuild(map(encode, references))


def _unmap_arguments(frame, inputs):
    """Select the worker's inputs from Parsl's resolved dependencies."""
    dependencies, rebuild = _sieve_inputs(frame, _ResultDependency)
    return rebuild(inputs[dependency.index] for dependency in dependencies)


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
        with frames[0].ctx.cache:
            for frame in frames:
                frame.bound_arguments = _unmap_arguments(frame, inputs)
                # Each frame publishes its outcome, including errors. A failed
                # cell must not prevent the remaining cells from running.
                as_future(frame)

    frames[0].action._set_wrapper_name(worker, frames[0].action.id)
    return python_app(executors=[executor])(worker)(frames, inputs=futures)


def _join_exception(job):
    error = job.exception()
    if isinstance(error, JoinError):
        # Surface the invocation's original error at scope exit.
        error, _ = error.dependent_exceptions_tids[0]
    return error


def _submit_coordinator(body, coordinate_indexes=None):
    results = Results(body.output_types, (
        output_proxy(Future(), None, spec.qiime_type,
                     coordinate_index=Future())
        for spec in body.output_types.values()
    ))
    outputs = Results._futures(results)
    completion = Future()
    caller_ctx = body.ctx
    body.ctx = copy(caller_ctx)
    body.ctx._parent = caller_ctx

    def fail_outputs(error):
        for output in results:
            if not output._future_.done():
                output._future_.set_exception(error)
            if (isinstance(output, ProxyArtifact)
                    and not output._coordinate_index.done()):
                output._coordinate_index.set_exception(error)

    def run(indexes):
        # Child work belongs to the body, which can submit more actions after
        # the caller has started waiting on its own scope.
        children = []
        body.coordinate_indexes = indexes
        with ExitStack() as cleanup:
            body.ctx._scopes.append((children, cleanup))
            try:
                with body.ctx.cache:
                    returned = body()
            except BaseException as error:
                fail_outputs(error)
            else:
                for value, output in zip(returned, results):
                    if isinstance(output, ProxyArtifact):
                        index = (value._coordinate_index
                                 if isinstance(value, ProxyArtifact) else None)
                        if index is None:
                            output._coordinate_index.set_result(None)
                        else:
                            after_all((index,), lambda values: values[0],
                                      into=output._coordinate_index)
                    result_as_future(value, into=output._future_)
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
    join_app(run)(coordinate_indexes).add_done_callback(finish)
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
            return _submit_coordinator(frame)

        executor = self._get_executor(frame.action)
        self.execution_metadata = {
            'type': 'parsl',
            'parsl_type': self.executor_name_type_mapping[executor],
        }

        if isinstance(frame.action, MappedMethod):
            indexes = frame.action._prepare_coordinate_indexes(frame)
            return _submit_coordinator(frame, indexes)

        # Parsl resolves upstream dependencies before the worker applies them.
        return Results._from_future(
            _submit_app(frame, executor), frame.output_types,
        )

    def _get_executor(self, action):
        return self.action_executor_mapping.get(
            action.plugin_id, {}).get(action.id, 'default')

    def _dispatch_cells_(self, cells, *, cleanup):
        if not cells:
            return {}, ()
        futures = {coord: Future() for coord in cells}
        executor = self._get_executor(next(iter(cells.values())).action)
        pending = {}
        jobs = []
        try:
            for batch in self._batches(cells):
                job = _submit_batch(
                    (cells[coord] for coord in batch),
                    executor)
                jobs.append(job)
                pending.update((cells[coord].path, (futures[coord], job))
                               for coord in batch)
        finally:
            if jobs:
                # A batch can still use the workspace after publishing a cell.
                cleanup.callback(wait, jobs)
                watcher = Thread(target=_watch_cell_commits, args=(pending,),
                                 daemon=True)
                watcher.start()
                cleanup.callback(watcher.join)
        return futures, jobs

    def _batches(self, cells):
        for coord in cells:
            yield (coord,)
