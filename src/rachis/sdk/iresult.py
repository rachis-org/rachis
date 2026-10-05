# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import abc


class IResult(metaclass=abc.ABCMeta):
    @staticmethod
    def _assert_alias_type_refines_realized_type(qiime_type, realized_type):
        if qiime_type is not None and not qiime_type <= realized_type:
            raise TypeError(
                "Alias type %r must be a subtype of realized result type %r."
                % (qiime_type, realized_type))

    @abc.abstractmethod
    def _alias(self, name, provenance, ctx, qiime_type=None):
        """
        Create a pipeline alias for this result
        """

    @property
    @abc.abstractmethod
    def record_provenance(self):
        """
        Whether this Result recorded provenance or not
        """

    @property
    @abc.abstractmethod
    def type(self):
        """
        The semantic type of this result
        """

    @property
    @abc.abstractmethod
    def uuid(self):
        """
        The uuid associated with this result
        """

    @property
    @abc.abstractmethod
    def format(self):
        """
        The directory format associated with this result
        """

    @property
    @abc.abstractmethod
    def citations(self):
        """
        Get the citations associated with this result
        """

    @property
    @abc.abstractmethod
    def result(self):
        """
        Provide standardized interface with ProxyResult which uses .result to
        block
        """

    @abc.abstractmethod
    def export_data(self, output_dir):
        """
        Export the data from this result to the given dir
        """

    @abc.abstractmethod
    def save(self, filepath, ext=None):
        """
        Save this result to the given filepath as a QIIME 2 result
        """

    @abc.abstractmethod
    def validate(self, level=NotImplemented):
        """
        Validate this result to the specified level
        """
