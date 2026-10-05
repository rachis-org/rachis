# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------
import abc


class IContext(metaclass=abc.ABCMeta):
    @abc.abstractmethod
    def get_action(
            self, plugin: str, action: str, *, record_provenance: bool=True
        ):
        """Return a function matching the callable API of an action.
        This function is aware of the pipeline context and manages its own
        cleanup as appropriate.
        """

    @abc.abstractmethod
    def make_artifact(self, type, view, view_type=None):
        """Return a new artifact from a given view.

        This artifact is automatically tracked and cleaned by the pipeline
        context.
        """

    @abc.abstractmethod
    def make_report(self, template, collection, descriptions=None):
        """Create a report based on a template and a collection of
        visualizations, with optional descriptions.
        """
