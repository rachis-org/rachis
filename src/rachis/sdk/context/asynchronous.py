# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import concurrent.futures
from types import MappingProxyType

from .base import Context


def _subprocess_apply(frame):
    # The worker uses synchronous dispatch for any pipeline child calls, while
    # this application retains its asynchronous execution provenance.
    # Activate the transported cache so the worker Context uses that cache.
    with frame.ctx.cache:
        ctx = Context(frame.action)
        ctx.execution_ctx = dict(frame.ctx.execution_ctx)
        ctx.inner_apply_wrappers = frame.ctx.inner_apply_wrappers
        ctx.outer_apply_wrappers = frame.ctx.outer_apply_wrappers
        ctx.mapped_inner_apply_wrappers = frame.ctx.mapped_inner_apply_wrappers
        frame.ctx = ctx
        return frame()


class AsynchronousContext(Context):
    execution_ctx = MappingProxyType({'type': 'asynchronous'})

    def _dispatch_action_(self, frame):
        # x-ref for why this try except exists
        # https://github.com/qiime2/qiime2/issues/73
        try:
            import matplotlib as plt
            if plt.rcParams['backend'].lower() == 'macosx':
                raise EnvironmentError(backend_error_template %
                                       plt.matplotlib_fname())
        except ImportError:
            pass

        pool = concurrent.futures.ProcessPoolExecutor(max_workers=1)
        worker_frame = frame.for_worker()
        future = pool.submit(_subprocess_apply, worker_frame)
        # TODO: pool.shutdown(wait=False) caused the child process to
        # hang unrecoverably. This seems to be a bug in Python 3.7
        # It's probably best to gut concurrent.futures entirely, so we're
        # ignoring the resource leakage for the moment.
        return future


# TODO add unit test for callables raising this
backend_error_template = """
Your current matplotlib backend (MacOSX) does not work with asynchronous calls.
A recommended backend is Agg, and can be changed by modifying your
matplotlibrc "backend" parameter, which can be found at: \n\n %s
"""
