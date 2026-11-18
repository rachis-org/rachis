# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from concurrent.futures import Future
from threading import Lock


def as_future(function):
    """Convert a callable into a Future by evaluating it and
    recording the result or exception."""
    future = Future()
    try:
        result = function()
        future.set_result(result)
    except Exception as error:
        future.set_exception(error)
    return future


def result_as_future(value):
    """Resolve a result reference, including any prospective collection."""
    from rachis.sdk.proxy import ProxyResult, ProxyResultCollection
    from rachis.sdk.result import ResultCollection

    if isinstance(value, (ProxyResult, ProxyResultCollection)):
        return after_all((value._future_,), value._get_element_)
    if isinstance(value, ResultCollection):
        keys = tuple(value.keys())
        return after_all(map(result_as_future, value.values()),
                         lambda *items: ResultCollection(
                             dict(zip(keys, items))))
    if isinstance(value, list):
        return after_all(map(result_as_future, value),
                         lambda *items: list(items))
    if isinstance(value, dict):
        keys = tuple(value)
        return after_all(map(result_as_future, value.values()),
                         lambda *items: dict(zip(keys, items)))
    return as_future(lambda: value)


def join(dependencies, combine, *, ctx):
    """Combine results locally and track deferred work in its invocation."""
    if not ctx._defer_results:
        return after_all(dependencies, combine)

    resources = ctx._get_resources()

    def run(*values):
        with ctx.cache:
            return combine(*values)

    future = after_all(dependencies, run,
                       run_local=resources.executor.submit)
    resources.jobs.append(future)
    return future


def after_all(dependencies, combine, *, run_local=None):
    """Call combine with settled dependency values in their original order."""
    dependencies = tuple(dependencies)
    future = Future()
    lock = Lock()
    remaining = len(dependencies)

    def finish():
        try:
            values = tuple(dep.result() for dep in dependencies)
            if run_local is None:
                future.set_result(combine(*values))
            else:
                task = run_local(combine, *values)
                task.add_done_callback(publish)
        except BaseException as error:
            future.set_exception(error)

    def publish(task):
        try:
            value = task.result()
        except BaseException as error:
            future.set_exception(error)
        else:
            future.set_result(value)

    def ready(_):
        nonlocal remaining
        with lock:
            remaining -= 1
            last = remaining == 0
        if last and future.set_running_or_notify_cancel():
            finish()

    if dependencies:
        for dependency in dependencies:
            dependency.add_done_callback(ready)
    elif future.set_running_or_notify_cancel():
        finish()
    return future
