# ----------------------------------------------------------------------------
# Copyright (c) 2026, QIIME 2 development team.
# Distributed under the terms of the Modified BSD License.
# ----------------------------------------------------------------------------
"""Tracked directories and the common API for their specializations."""

from pathlib import Path

from .reference import references


class RefDir:
    """A stable directory identity, separate from any one lifetime owner.

    ``root`` is the RefDir itself; ``path`` may expose a consumer's workspace
    within it. Dependencies are root hardlinks, managed through ``reference``.
    A handle's optional acquisition token releases only that acquisition.
    """

    def __init__(
        self,
        cache,
        ref_id,
        scope=None,
        path=None,
        *,
        category=None,
        acquisition=None,
    ):
        self.cache = cache
        self.ref_id = ref_id
        self.scope = scope
        self.root = (
            cache.refdir_path(category, ref_id)
            if category is not None
            else cache.object_path("ref", ref_id)
        )
        self.category = self.root.parent.parent.name
        self.path = Path(path) if path is not None else self.root
        self.acquisition = acquisition

    @classmethod
    def create(cls, cache, category="tempdirs", *, name=None, scope=None):
        """Publish a categorized RefDir with an established acquisition."""
        return cache.create_refdir(category, name=name, scope=scope, cls=cls)

    def reference(self, target, *, acquisition=False):
        """Retain another RefDir or an immutable pack and return its token."""
        return self.cache.reference(self.root, target, acquisition=acquisition)

    def unreference(self, reference):
        """Release exactly one previously returned dependency token."""
        if reference.entry.parent not in (
            self.root,
            self.root / "annotations",
            self.root / "provenance",
        ):
            raise ValueError("Reference belongs to a different RefDir")
        self.cache.unreference(reference)

    def references(self):
        return references(self.cache.path, self.root)

    def release(self):
        """Release this acquisition without affecting other owners."""
        if self.acquisition is not None:
            self.cache.unreference(self.acquisition)
            self.acquisition = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.release()
