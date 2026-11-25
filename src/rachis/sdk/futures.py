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


def result_as_future(value, *, into=None):
    """Resolve a result reference, including any prospective collection."""
    from rachis.sdk.proxy import ProxyResult, ProxyResultCollection
    from rachis.sdk.result import ResultCollection

    proxy_types = (ProxyResult, ProxyResultCollection)
    if isinstance(value, proxy_types):
        return after_all(
            (value._future_,), lambda values: value._get_element_(values[0]),
            into=into,
        )
    if isinstance(value, (ResultCollection, list, dict)):
        proxies, rebuild = Sievable.sieve(value, isinstance=proxy_types)
        return after_all(map(result_as_future, proxies), rebuild, into=into)
    return after_all((), lambda _: value, into=into)


def join(dependencies, combine, *, ctx):
    """Combine settled results under the invocation's cache."""

    def run(values):
        with ctx.cache:
            return combine(*values)

    return after_all(dependencies, run)


def after_all(dependencies, combine, *, into=None):
    """Call combine in dependency order and complete a future with its result.

    If supplied, into is the pending future to complete and return.
    """
    def resolve(settled):
        return combine(tuple(dep.result() for dep in settled))

    return after_settled(dependencies, resolve, into=into)


def after_settled(dependencies, combine, *, into=None):
    """Call combine with the futures once every dependency has settled."""
    dependencies = tuple(dependencies)
    future = Future() if into is None else into
    lock = Lock()
    remaining = len(dependencies)

    def finish():
        try:
            result = combine(dependencies)
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
