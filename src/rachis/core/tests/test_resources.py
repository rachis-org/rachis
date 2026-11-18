import pytest

from rachis.core._resources import InvocationResources


def test_resource_scope_is_borrowed_and_cleared_on_error():
    with pytest.raises(ValueError, match='invocation failed'):
        with InvocationResources.ensure():
            outer = InvocationResources.current()
            with InvocationResources.ensure():
                assert InvocationResources.current() is outer
            assert InvocationResources.current() is outer
            raise ValueError('invocation failed')

    with pytest.raises(RuntimeError, match='No active invocation'):
        InvocationResources.current()
