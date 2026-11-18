import os
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import ExitStack, contextmanager
from functools import cached_property
from threading import local


_active = local()


def _clear_active_resources():
    _active.resources = None


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_clear_active_resources)


class InvocationResources(ExitStack):
    """Keep invocation resources alive until work and joins settle."""

    def __init__(self):
        super().__init__()
        self.jobs = []

    @staticmethod
    def current():
        resources = getattr(_active, 'resources', None)
        if resources is None:
            raise RuntimeError('No active invocation resource scope')
        return resources

    @classmethod
    @contextmanager
    def ensure(cls):
        """Open a resource scope if the current thread has none."""
        if getattr(_active, 'resources', None) is not None:
            yield
            return

        try:
            with cls() as resources:
                _active.resources = resources
                yield
        finally:
            _clear_active_resources()

    @cached_property
    def executor(self):
        return self.enter_context(ThreadPoolExecutor(
            max_workers=1, thread_name_prefix='rachis-join'))

    def __exit__(self, *exc_details):
        wait(self.jobs)
        return super().__exit__(*exc_details)
