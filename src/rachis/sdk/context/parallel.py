# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass, replace
from threading import Event, Thread
from types import MappingProxyType

from parsl.app.app import python_app

from rachis.sdk.futures import call_future
from rachis.sdk.mapped import (
    BatchFrame, CellFrame, CellPrefix, CellReference, CommittedCell,
    read_outcome,
)
from rachis.sdk.result import ResultCollection
from rachis.sdk.proxy import Proxy
from rachis.sdk.parallel_config import PARALLEL_CONFIG

from .base import Context, invocation_resources_wrapper


@dataclass(frozen=True)
class _CellDependency:
    index: int
    name: str


@dataclass(frozen=True)
class _ResultDependency:
    index: int
    selector: str | None


class CellCommitWatcher:
    """Settle each cell future as soon as its worker publishes an outcome."""

    def __init__(self, pending):
        self._pending = pending
        self._stop = Event()
        self._thread = Thread(target=self._watch, daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        self._thread.join()

    def _watch(self):
        pending = self._pending
        try:
            while pending and not self._stop.is_set():
                for path, (cell, batch) in list(pending.items()):
                    # Check completion before the file so a last-minute commit
                    # can still satisfy its cell future.
                    done = batch.done()
                    try:
                        if path.is_file():
                            outcome = read_outcome(path)
                            if isinstance(outcome, Exception):
                                raise outcome
                        elif done:
                            batch.result()
                            raise RuntimeError(
                                f'Batch did not commit {path.name}')
                        else:
                            continue
                    except Exception as error:
                        cell.set_exception(error)
                    else:
                        cell.set_result(outcome)
                    del pending[path]
                if pending:
                    self._stop.wait(0.01)
        finally:
            for cell, _ in pending.values():
                if not cell.done():
                    cell.set_exception(RuntimeError('Cell watcher closed'))


def _map_arg(arg, futures):
    """Encode references as indexes into the app's dependency list."""
    if isinstance(arg, CellReference):
        mapped = _CellDependency(len(futures), arg.name)
        futures.append(arg.future)
    elif isinstance(arg, CellPrefix):
        mapped = replace(arg, members=tuple(
            (coord, _map_arg(cell, futures)) for coord, cell in arg.members))
    elif isinstance(arg, Proxy):
        futures.append(arg._future_)
        mapped = _ResultDependency(len(futures) - 1, arg._selector_)
    elif isinstance(arg, list):
        mapped = [_map_arg(proxy, futures) for proxy in arg]
    elif isinstance(arg, ResultCollection):
        mapped = ResultCollection({
            key: _map_arg(value, futures) for key, value in arg.items()})
    elif isinstance(arg, dict):
        mapped = {key: _map_arg(proxy, futures) for key, proxy in arg.items()}
    else:
        mapped = arg

    return mapped


def _unmap_arg(arg, inputs):
    """Select the worker's inputs from Parsl's resolved dependencies."""
    if isinstance(arg, _CellDependency):
        unmapped = CommittedCell(inputs[arg.index], arg.name)
    elif isinstance(arg, CellPrefix):
        unmapped = replace(arg, members=tuple(
            (coord, _unmap_arg(cell, inputs)) for coord, cell in arg.members))
    elif isinstance(arg, _ResultDependency):
        result = inputs[arg.index]
        unmapped = (result if arg.selector is None
                    else getattr(result, arg.selector))
    elif isinstance(arg, list):
        unmapped = [_unmap_arg(proxy, inputs) for proxy in arg]
    elif isinstance(arg, ResultCollection):
        unmapped = ResultCollection({
            key: _unmap_arg(value, inputs) for key, value in arg.items()})
    elif isinstance(arg, dict):
        unmapped = {key: _unmap_arg(proxy, inputs) for
                    key, proxy in arg.items()}
    else:
        unmapped = arg

    return unmapped


def _contains_proxy(value):
    if isinstance(value, Proxy):
        return True
    if isinstance(value, (dict, ResultCollection)):
        return any(_contains_proxy(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_proxy(item) for item in value)
    return False


@contextmanager
def parsl_worker_wrapper(frame, inputs):
    """Activate the cache and decode one app's resolved dependencies."""
    with frame.ctx.cache:
        for application in frame.cells if isinstance(frame, BatchFrame) else (
                frame,):
            application.bound_arguments = _unmap_arg(
                application.bound_arguments, inputs)
        yield


def _submit_app(frame, executor):
    futures = []
    worker_frame = frame.for_worker()
    for application in (worker_frame.cells
                        if isinstance(worker_frame, BatchFrame)
                        else (worker_frame,)):
        application.bound_arguments = _map_arg(
            application.bound_arguments, futures)
        if isinstance(worker_frame, BatchFrame):
            application.cell = replace(application.cell, inputs={})

    def worker(frame, inputs=()):
        with parsl_worker_wrapper(frame, inputs):
            return frame()

    frame.action._set_wrapper_name(worker, frame.action.id)
    return python_app(executors=[executor])(worker)(
        worker_frame, inputs=futures)


class ParallelContext(Context):
    execution_ctx = MappingProxyType({'type': 'parsl'})
    outer_bind_wrappers = (invocation_resources_wrapper,
                           *Context.outer_bind_wrappers)
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

    def _check_cache(self, inputs):
        # Hashing an unresolved proxy would block the driver on its artifact.
        if not any(_contains_proxy(value) for value in inputs.values()):
            return super()._check_cache(inputs)

    def _dispatch_action_(self, frame):
        # Pipeline bodies keep coordinating locally, as before this refactor.
        # Their child applications still pass through their own outer stacks.
        if frame.action._driver_apply and not frame.action._mapped:
            self.execution_ctx = {'type': 'parsl', 'parsl_type': 'DFK'}
            return call_future(frame)

        if PARALLEL_CONFIG.parallel_config is None:
            raise ValueError('You must load a parallel config before '
                             'running in parallel.')

        executor = self._get_executor(frame.action)
        self.execution_ctx = {
            'type': 'parsl',
            'parsl_type': self.executor_name_type_mapping[executor],
        }

        if frame.action._driver_apply:
            return call_future(frame)

        # Parsl can schedule against upstream futures. The worker validates
        # and coerces the arguments once those futures have resolved.
        future = _submit_app(frame, executor)
        self._get_resources().jobs.append(future)
        return future

    def _get_executor(self, action):
        return self.action_executor_mapping.get(
            action.plugin_id, {}).get(action.id, 'default')

    def _dispatch_cells_(self, cells):
        if not cells:
            return {}
        batches = tuple(tuple(batch) for batch in self._batches(cells))
        scheduled = [coord for batch in batches for coord in batch]
        if len(scheduled) != len(cells) or set(scheduled) != cells.keys():
            raise ValueError('Batches must partition the planned cells')
        resources = self._get_resources()
        futures = {coord: Future() for coord in cells}
        pending = {}
        try:
            for batch in batches:
                if not batch:
                    continue
                frame = CellFrame.for_batch(cells[coord] for coord in batch)
                job = _submit_app(frame, self._get_executor(frame.action))
                resources.jobs.append(job)
                pending.update((cells[coord].cell.path, (futures[coord], job))
                               for coord in batch)
        finally:
            if pending:
                watcher = CellCommitWatcher(pending)
                resources.callback(watcher.close)
        return futures

    def _batches(self, cells):
        for coord in cells:
            yield (coord,)
