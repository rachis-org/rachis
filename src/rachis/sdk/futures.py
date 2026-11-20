# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from concurrent.futures import Future
from threading import Lock

from rachis.sdk.util import Sievable


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

    proxy_types = (ProxyResult, ProxyResultCollection)
    if isinstance(value, proxy_types):
        return after_all(
            (value._future_,), lambda values: value._get_element_(values[0]),
        )
    if isinstance(value, (ResultCollection, list, dict)):
        proxies, rebuild = Sievable.sieve(value, isinstance=proxy_types)
        return after_all(map(result_as_future, proxies), rebuild)
    return as_future(lambda: value)


def join(dependencies, combine, *, ctx):
    """Combine settled results under the invocation's cache."""

    def run(values):
        with ctx.cache:
            return combine(*values)

    return after_all(dependencies, run)


def after_all(dependencies, combine):
    """Call combine with a tuple of settled values in dependency order."""
    dependencies = tuple(dependencies)
    future = Future()
    lock = Lock()
    remaining = len(dependencies)

    def finish():
        try:
            values = tuple(dep.result() for dep in dependencies)
            result = combine(values)
        except BaseException as error:
            future.set_exception(error)
        else:
            future.set_result(result)

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
