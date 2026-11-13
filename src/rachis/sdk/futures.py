# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from concurrent.futures import Future
from threading import Lock


def completed_future(value):
    future = Future()
    future.set_result(value)
    return future


def call_future(call):
    """Run work inline with the same result contract as deferred work."""
    try:
        return completed_future(call())
    except Exception as error:
        future = Future()
        future.set_exception(error)
        return future


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
