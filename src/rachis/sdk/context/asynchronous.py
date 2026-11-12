# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import concurrent.futures

from .base import Context
from rachis.sdk.action import Frame


def _subprocess_apply(frame):
    # The worker uses synchronous dispatch for any pipeline child calls, while
    # this application retains its asynchronous execution provenance.
    # Activate the transported cache so the worker Context uses that cache.
    with frame.ctx.cache:
        ctx = Context(frame.action)
        ctx.execution_ctx = dict(frame.ctx.execution_ctx)
        ctx.inner_wrappers = frame.ctx.inner_wrappers
        worker_frame = Frame(frame.action, ctx, frame.inputs)
        return worker_frame.action._inner_apply(worker_frame)


class AsynchronousContext(Context):
    execution_ctx = {'type': 'asynchronous'}

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
        # Only the action and bound inputs are needed by the worker.
        worker_frame = Frame(frame.action, self, frame.inputs)
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
