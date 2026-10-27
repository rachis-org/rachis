# ----------------------------------------------------------------------------
# Copyright (c) 2026, QIIME 2 development team.
# Distributed under the terms of the Modified BSD License.
# ----------------------------------------------------------------------------
import os
from pathlib import Path
import uuid

import pytest

from rachis import Cache
from rachis.core.refdir import RefDir
from rachis.core.reference import (
    Reference,
    directory_references,
    new_id,
    target_name,
    target_path,
)


@pytest.fixture
def cache(tmp_path):
    cache = Cache(tmp_path / "cache")
    with cache:
        yield cache
    cache.close()


def test_scope_acquisitions_release_independently(cache):
    scope = cache.create_scope()
    directory = RefDir.create(cache, "custom", scope=scope)
    second = scope.adopt(directory.ref_id)
    marker = directory.root / ".iref"
    assert marker.stat().st_nlink == 3
    assert directory.acquisition.entry != second.entry
    assert target_path(cache.path, second.entry.name) == marker
    directory.release()
    cache.garbage_collection()
    assert directory.root.exists()
    scope.unreference(second)
    cache.garbage_collection()
    assert not directory.root.exists()
    scope.close()


def test_dependency_decoding_does_not_read_marker(cache, monkeypatch):
    directory = RefDir.create(cache, "custom")
    reference = cache.root_scope.adopt(directory.ref_id)

    def unexpected(*args, **kwargs):
        raise AssertionError(
            "Encoded references must not read marker contents"
        )

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", unexpected)
        kind, identity, path = cache.marker_target(reference.entry)
    assert (kind, identity, path) == (
        "custom",
        directory.ref_id,
        directory.root,
    )


def test_generic_categories_and_dependency_chain_gc(cache):
    nodes = [
        RefDir.create(cache, category)
        for category in (
            "custom_a",
            "custom_b",
            "custom_c",
        )
    ]
    nodes[0].reference(nodes[1])
    nodes[1].reference(nodes[2])
    for node in nodes:
        node.release()
    # Deep discovery also recovers an interrupted scheduling operation.
    cache._candidates.clear()
    cache.garbage_collection(deep=True)
    assert all(not node.root.exists() for node in nodes)


@pytest.mark.parametrize("category", ["artifact", "scope", "ref"])
def test_custom_categories_do_not_alias_backend_objects(cache, category):
    directory = RefDir.create(cache, category)
    assert directory.category == category
    assert directory.root.parent.parent == cache.path / "mutable" / category
    directory.release()
    cache.garbage_collection(deep=True)
    assert not directory.root.exists()


def test_refdir_specialization_uses_the_parent_ownership_api(cache):
    class Workspace(RefDir):
        def write(self, value):
            (self.path / "value").write_bytes(value)

    directory = Workspace.create(cache, "workspace", name="content")
    directory.write(b"custom state")
    acquisition = cache.root_scope.adopt(directory.ref_id)
    directory.release()
    cache.garbage_collection(deep=True)
    assert (directory.path / "value").read_bytes() == b"custom state"
    cache.root_scope.unreference(acquisition)
    cache.garbage_collection()
    assert not directory.root.exists()


def test_external_marker_hardlink_retains_custom_refdir(cache, tmp_path):
    directory = RefDir.create(cache, "custom")
    anchor = tmp_path / "external.iref"
    os.link(directory.root / ".iref", anchor)
    directory.release()
    cache.garbage_collection(deep=True)
    assert directory.root.exists()
    anchor.unlink()
    cache.garbage_collection(deep=True)
    assert not directory.root.exists()


@pytest.mark.parametrize("category", ["provenance", "annotations"])
def test_pack_reference_uses_the_pack_inode_and_cascades(cache, category):
    from rachis.core.archive.pack import pack_path
    from rachis.core.reference import durable_write

    owner = RefDir.create(cache, "custom")
    identity = str(uuid.uuid4())
    pack = pack_path(cache, category, identity)
    durable_write(pack, b"immutable pack contents")
    reference = owner.reference(pack)
    assert reference.entry.suffix == ".zip"
    assert reference.entry.samefile(pack)
    assert target_path(cache.path, reference.entry.name) == pack
    assert pack.stat().st_nlink == 2
    assert reference in list(owner.references())
    owner.release()
    cache.garbage_collection()
    assert not owner.root.exists()
    assert not pack.exists()


def test_reference_is_idempotent_and_rejects_a_different_inode(cache):
    first, second = RefDir.create(cache), RefDir.create(cache)
    one = first.reference(second)
    two = first.reference(second)
    assert one == two
    assert second.root.joinpath(".iref").stat().st_nlink == 3
    with cache.lock, pytest.raises(ValueError, match="different target"):
        Reference.link(
            cache.path, first.root, first.root / ".iref", name=one.entry.name
        )


@pytest.mark.parametrize(
    "name",
    [
        "mutable..aa.id.iref",
        "../escape.iref",
        "_bad_.mutable.custom.aa.id.iref",
        "immutable.archives.aa.id.extra.iref",
    ],
)
def test_invalid_encoded_paths_are_rejected(cache, name):
    with pytest.raises(ValueError):
        target_path(cache.path, name)


def test_pending_acquisition_decodes_to_the_same_target(cache):
    directory = RefDir.create(cache)
    name = target_name(cache.path, directory.root / ".iref", new_id())
    assert (
        target_path(cache.path, ".pending." + name) == directory.root / ".iref"
    )


def test_scope_nesting_is_flat_and_failure_retains_the_stack(cache):
    from rachis.sdk.context.base import Context

    pool = cache.create_pool("failed")
    with pool:
        outer = Context()._execution_context()
        with pytest.raises(RuntimeError, match="failure"), outer:
            outer_work = RefDir.create(cache, "outer_work")
            inner = Context(object(), parent=outer)._execution_context()
            with inner:
                inner_work = RefDir.create(cache, "inner_work")
                raise RuntimeError("failure")
    assert outer.scope.closed and inner.scope.closed
    assert list(outer.scope.root.glob("mutable.scopes.*.iref")) == []
    retained = {
        ref.target.parent
        for ref in directory_references(cache.path, pool.path)
    }
    assert outer.scope.root in retained and inner.scope.root in retained
    cache.close()
    assert outer_work.root.exists() and inner_work.root.exists()
    reopened = Cache(cache.path)
    reopened.remove("failed")
    reopened.garbage_collection(deep=True)
    assert not outer_work.root.exists() and not inner_work.root.exists()
    reopened.close()


@pytest.mark.parametrize("pooled", [False, True])
def test_context_rejects_pool_changes_without_mutating_its_scope(
    cache, pooled
):
    from contextlib import nullcontext
    from rachis.sdk.context.base import Context

    one, two = cache.create_pool("one"), cache.create_pool("two")
    with one if pooled else nullcontext():
        frame = Context()._execution_context()
        with frame:
            scratch = RefDir.create(cache)
            for pool in (one, two):
                with pytest.raises(ValueError, match="call site"):
                    with pool:
                        raise AssertionError("Pool selection was permitted")
            assert cache.scope is frame.scope
            assert cache.named_pool is (one if pooled else None)
            child = Context(object(), parent=frame)._execution_context()
            with child:
                child_scratch = RefDir.create(cache)
                assert child.pool is frame.pool
            assert two.path not in frame.scope.owners
        cache.garbage_collection()
        assert frame.scope.closed and not frame.scope.root.exists()
        assert not child.scope.root.exists()
        assert not scratch.root.exists()
        assert not child_scratch.root.exists()


def test_context_binding_survives_pool_exit_before_execution(cache):
    from rachis.sdk.context.base import Context

    pool = cache.create_pool("bound")
    with pool:
        context = Context()
    assert cache.named_pool is None
    frame = context._execution_context()
    with frame:
        assert frame.pool is pool and cache.named_pool is pool
        assert pool.path in frame.scope.owners
    assert cache.named_pool is None
    cache.garbage_collection()
    assert not frame.scope.root.exists()


def test_child_context_inherits_pool_across_callsite_selection_changes(cache):
    from rachis.sdk.context.base import Context

    one, two = cache.create_pool("one"), cache.create_pool("two")
    with one:
        parent = Context()
    with two:
        child = Context(object(), parent=parent)
        assert parent.pool is child.pool is one


def test_concurrent_contexts_keep_pool_and_scope_bindings_separate(cache):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from rachis.sdk.context.base import Context

    pools = [cache.create_pool(name) for name in ("one", "two")]
    frames = []
    for pool in pools:
        with pool:
            frames.append(Context()._execution_context())
    barrier = Barrier(2)

    def execute(frame):
        with frame:
            barrier.wait(timeout=10)
            assert cache.named_pool is frame.pool
            assert cache.scope is frame.scope
            directory = RefDir.create(cache)
            assert directory.scope is frame.scope
        assert cache.named_pool is None
        return directory

    with ThreadPoolExecutor(max_workers=2) as executor:
        directories = list(executor.map(execute, frames))
    assert cache.named_pool is None
    cache.garbage_collection()
    assert all(not directory.root.exists() for directory in directories)


def test_bound_callable_reuses_results_from_its_pool(cache, monkeypatch):
    from rachis.core.testing.util import get_dummy_plugin
    from rachis.sdk.context.base import Context

    one = cache.create_pool("one")
    method = get_dummy_plugin().methods["returns_int"]
    original = method._callable
    calls = []

    def counted(int):
        calls.append(int)
        return original(int)

    monkeypatch.setattr(method, "_callable", counted)
    with one:
        frame = Context()._execution_context()
        with frame:
            action = frame.get_action("dummy_plugin", "returns_int")
            (first,) = action(42)
            one.create_index()
            (second,) = action(42)
            assert cache.scope is frame.scope
            assert frame.pool is one
            assert first.uuid == second.uuid
            assert calls == [42]


@pytest.mark.parametrize("backend", ["v1", "v2"])
@pytest.mark.parametrize("pooled", [False, True])
@pytest.mark.parametrize("fail", [False, True])
def test_pipeline_pool_selection_is_only_allowed_at_the_callsite(
    tmp_path, monkeypatch, backend, pooled, fail
):
    from contextlib import nullcontext
    from rachis import CacheV1, CacheV2
    from rachis.core.testing.util import get_dummy_plugin

    cache = (CacheV1 if backend == "v1" else CacheV2)(tmp_path / "cache")
    selected = cache.create_pool("selected")
    other = cache.create_pool("other")
    pipeline = get_dummy_plugin().pipelines["pointless_pipeline"]
    original = pipeline._callable

    def blocked(ctx):
        assert ctx.pool is (selected if pooled else None)
        for pool in (selected, other):
            with pytest.raises(ValueError, match="call site"):
                with pool:
                    raise AssertionError("Pool selection was permitted")
            with pytest.raises(ValueError, match="call site"):
                pool.__exit__()
        assert cache.named_pool is ctx.pool
        if fail:
            raise RuntimeError("pipeline failed")
        child = ctx.get_action("dummy_plugin", "returns_int")
        assert child(4)[0].view(int) == 4
        return original(ctx)

    monkeypatch.setattr(pipeline, "_callable", blocked)
    try:
        with cache:
            with selected if pooled else nullcontext():
                if fail:
                    with pytest.raises(RuntimeError, match="pipeline failed"):
                        pipeline()
                else:
                    assert pipeline()[0].view(int) == 4
                assert cache.named_pool is (selected if pooled else None)
            # Both successful and failed actions restore call-site selection.
            with other:
                assert cache.named_pool is other
    finally:
        if backend == "v2":
            cache.close()


@pytest.mark.parametrize("backend", ["v1", "v2"])
def test_both_backends_bind_contexts_and_children_at_the_callsite(
    tmp_path, backend
):
    from rachis import CacheV1, CacheV2
    from rachis.sdk.context.base import Context

    cache = (CacheV1 if backend == "v1" else CacheV2)(tmp_path / "cache")
    try:
        with cache:
            one, two = cache.create_pool("one"), cache.create_pool("two")
            unpooled = Context()
            with one:
                parent = Context()
                first = parent._execution_context()
                second = parent._execution_context()
                assert first is not parent and second is not parent
                assert first is not second
                with first:
                    assert first.pool is one
                with second:
                    assert second.pool is one
            with two:
                child = Context(object(), parent=parent)
                assert unpooled.pool is None
                assert parent.pool is child.pool is one
    finally:
        if backend == "v2":
            cache.close()


def test_pool_save_retains_only_the_requested_result(cache):
    from rachis import Artifact
    from rachis.core.testing.type import IntSequence1

    unrelated = RefDir.create(cache, "scratch")
    result = Artifact.import_data(IntSequence1, [1, 2, 3])
    pool = cache.create_pool("saved")
    saved = pool.save(result)
    scopes = list(directory_references(cache.path, pool.path))
    assert len(scopes) == 1
    members = list(directory_references(cache.path, scopes[0].target.parent))
    assert [member.target.parent for member in members] == [
        cache.refdir(saved._archiver.ref_id, "archives").root
    ]
    assert unrelated.root not in [member.target.parent for member in members]


def test_explicit_save_to_another_pool_survives_action_completion(cache):
    from rachis import Artifact
    from rachis.core.testing.type import IntSequence1
    from rachis.sdk.context.base import Context

    one, two = cache.create_pool("one"), cache.create_pool("two")
    with one:
        frame = Context()._execution_context()
        with frame:
            scratch = RefDir.create(cache)
            result = Artifact.import_data(IntSequence1, [1, 2, 3])
            saved = two.save(result)
            assert frame.pool is one and cache.scope is frame.scope
        cache.garbage_collection(deep=True)
        assert not frame.scope.root.exists() and not scratch.root.exists()
        assert two.load(str(saved.uuid)).view(list) == [1, 2, 3]


def test_reentering_the_same_pool_restores_each_scope(cache):
    pool = cache.create_pool("nested")
    initial = cache.scope
    with pool:
        first = cache.scope
        with pool:
            second = cache.scope
            assert second is not first
        assert cache.scope is first and cache.named_pool is pool
    assert cache.scope is initial and cache.named_pool is None


def test_resume_skips_completed_inner_pipeline_after_caller_failure(
    cache,
    monkeypatch,
):
    from rachis.core.testing.type import SingleInt
    from rachis.core.testing.util import get_dummy_plugin
    from rachis.core.util import load_action_yaml
    from rachis.plugin import Bool

    plugin = get_dummy_plugin()
    inner = plugin.pipelines["pointless_pipeline"]
    original = inner._callable
    calls, returned = [], []

    def counted(ctx):
        result = original(ctx)
        calls.append(cache.refdir(result._archiver.ref_id, "archives"))
        return result

    monkeypatch.setattr(inner, "_callable", counted)

    def resume_after_completed_inner(ctx, fail=False):
        (result,) = ctx.get_action("dummy_plugin", "pointless_pipeline")()
        returned.append(str(result.uuid))
        if fail:
            raise RuntimeError("caller failure")
        return result

    plugin.pipelines.register_function(
        function=resume_after_completed_inner,
        inputs={},
        parameters={"fail": Bool},
        outputs=[("output", SingleInt)],
        name="Resume after a completed child",
        description="Tests frame completion independently of caller failure",
    )
    try:
        outer = plugin.pipelines["resume_after_completed_inner"]
        with cache.create_pool("resume"):
            with pytest.raises(RuntimeError, match="caller failure"):
                outer(fail=True)
            cache.garbage_collection(deep=True)
            # The child's private construction value is no longer retained.
            assert len(calls) == 1 and not calls[0].root.exists()
            result = outer().output
        assert len(calls) == 1
        assert returned[0] == returned[1]
        assert (
            load_action_yaml(result._archiver.path)["action"]["alias-of"]
            == returned[0]
        )
        assert result.view(int) == 4
    finally:
        del plugin.pipelines["resume_after_completed_inner"]
