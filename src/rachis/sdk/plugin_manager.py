# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import collections
import importlib.metadata
import enum

import rachis.core.type
from rachis.core.format import FormatBase
from rachis.plugin.model import SingleFileDirectoryFormatBase
from rachis.core.validate import ValidationObject
from rachis.sdk.util import parse_type
from rachis.core.type import is_semantic_type
from rachis.core.util import in_test_mode


class GetFormatFilters(enum.Flag):
    EXPORTABLE = enum.auto()
    IMPORTABLE = enum.auto()


class UninitializedPluginManagerError(Exception):
    pass


class DuplicatePluginError(Exception):
    pass


class PluginManager:
    entry_point_group = 'rachis.plugins'
    __instance = None

    @classmethod
    def iter_entry_points(cls):
        """Yield QIIME 2 plugin entry points.

        If the RACHISTEST (or legacy QIIMETEST) environment variable is
        set, only the framework testing plugin entry point (`dummy-plugin`)
        will be yielded. Otherwise, all available plugin entry points
        (excluding `dummy-plugin`) will be yielded.

        """
        for entry_point in importlib.metadata.entry_points(
                group=cls.entry_point_group):
            if in_test_mode():
                if entry_point.name in ('dummy-plugin', 'other-plugin'):
                    yield entry_point
            else:
                if entry_point.name not in ('dummy-plugin', 'other-plugin'):
                    yield entry_point

        # backwards compatibility
        if not in_test_mode():
            for entry_point in importlib.metadata.entry_points(
                    group='qiime2.plugins'):
                yield entry_point

    @classmethod
    def reuse_existing(cls):
        if cls.__instance is not None:
            return cls.__instance
        raise UninitializedPluginManagerError

    # This class is a singleton as it is slow to create, represents the
    # state of a rachis installation, and is needed *everywhere*
    def __new__(cls, add_plugins=True):
        if cls.__instance is None:
            self = super().__new__(cls)
            cls.__instance = self
            try:
                self._init(add_plugins=add_plugins)
            except Exception:
                cls.__instance = None
                raise
        else:
            if add_plugins is False:
                raise ValueError(
                    'PluginManager singleton already exists, cannot change '
                    'current value for `add_plugins`.')
        return cls.__instance

    def forget_singleton(self):
        """Allows later instatiation of PluginManager to produce new object

        This is done by clearing class member which saves the instance. This
        will NOT invalidate or remove the object this method is called on.
        """
        self.__class__.__instance = None

    def _init(self, add_plugins):
        self.plugins = {}
        self.type_fragments = {}
        self._plugin_by_id = {}
        self.semantic_types = {}
        self.transformers = collections.defaultdict(dict)
        self._reverse_transformers = collections.defaultdict(dict)
        self.formats = {}
        self.views = {}
        self.artifact_classes = {}
        self.axes = {}
        self._ff_to_sfdf = {}
        self.validators = {}

        if add_plugins:
            # These are all dependent loops, each requires the loop above it to
            # be completed.
            for entry_point in self.iter_entry_points():
                project_name = entry_point.name
                package = entry_point.value.split('.')[0]
                plugin = entry_point.load()

                self.add_plugin(plugin, package, project_name,
                                consistency_check=False)

            self._consistency_check()

    def _consistency_check(self):
        for semantic_type, validator_obj in self.validators.items():
            validator_obj.assert_transformation_available(
                self.get_directory_format(semantic_type))

    def add_plugin(self, plugin, package=None, project_name=None,
                   consistency_check=True):
        if plugin.name in self.plugins:
            raise DuplicatePluginError(
                f'A plugin by the name of {plugin.name} is already registered.'
            )

        self.plugins[plugin.name] = plugin
        self._plugin_by_id[plugin.id] = plugin
        if plugin.package is None:
            plugin.package = package
        if plugin.project_name is None:
            plugin.project_name = project_name

        # validate _after_ applying arguments
        if plugin.package is None:
            raise ValueError(
                'No value specified for package - must provide a value for '
                '`package` or set `plugin.package`.')
        if plugin.project_name is None:
            raise ValueError(
                'No value specified for project_name - must provide a value '
                'for `project_name` or set `plugin.project_name`.')

        self._integrate_plugin(plugin)
        plugin.freeze()
        if consistency_check is True:
            return self._consistency_check()

    def get_plugin(self, *, id=None, name=None):
        if id is None and name is None:
            raise ValueError("No plugin requested.")
        elif id is not None:
            try:
                return self._plugin_by_id[id]
            except KeyError:
                raise KeyError('No plugin currently registered '
                               'with id: "%s".' % (id,))
        else:
            try:
                return self.plugins[name]
            except KeyError:
                raise KeyError('No plugin currently registered '
                               'with name: "%s".' % (name,))

    def _integrate_plugin(self, plugin):
        for type_name, type_record in plugin.type_fragments.items():
            if type_name in self.type_fragments:
                conflicting_type_record = \
                    self.type_fragments[type_name]
                raise ValueError("Duplicate semantic type (%r) defined in"
                                 " plugins: %r and %r"
                                 % (type_name, type_record.plugin.name,
                                    conflicting_type_record.plugin.name))

            self.type_fragments[type_name] = type_record

        for (input, output), transformer_record in plugin.transformers.items():
            if output in self.transformers[input]:
                raise ValueError("Transformer from %r to %r already exists."
                                 % (input, output))
            self.transformers[input][output] = transformer_record
            self._reverse_transformers[output][input] = transformer_record

        for name, record in plugin.views.items():
            if name in self.views:
                raise NameError(
                    "Duplicate view registration (%r) defined in plugins: %r"
                    " and %r" %
                    (name, record.plugin.name, self.formats[name].plugin.name)
                )
            self.views[name] = record

        for name, record in plugin.formats.items():
            fmt = record.format

            if issubclass(
                    fmt, rachis.plugin.model.SingleFileDirectoryFormatBase):
                if fmt.file.format in self._ff_to_sfdf.keys():
                    self._ff_to_sfdf[fmt.file.format].add(fmt)
                else:
                    self._ff_to_sfdf[fmt.file.format] = {fmt}

            # TODO: remove this when `sniff` is removed
            if hasattr(fmt, 'sniff') and hasattr(fmt, '_validate_'):
                raise RuntimeError(
                    'Format %r registered in plugin %r defines sniff and'
                    '_validate_ methods - only one is permitted.' %
                    (name, record.plugin.name)
                )

            self.formats[name] = record

        for name, record in plugin.artifact_classes.items():
            if name in self.artifact_classes:
                raise NameError(
                    "Duplicate artifact class registration (%r) defined in "
                    "plugins %r and %r." %
                    (name, record.plugin.name,
                     self.artifact_classes[name].plugin.name)
                )
            else:
                self.artifact_classes[name] = record

        for key, axis in plugin.axes.items():
            if key in self.axes:
                raise ValueError(f'Duplicate axis registration for {key!r}')
            self.axes[key] = axis

        for semantic_type, validation_object in plugin.validators.items():
            if semantic_type not in self.validators:
                self.validators[semantic_type] = \
                    ValidationObject(semantic_type)

            self.validators[semantic_type].add_validation_object(
                validation_object)

    def get_semantic_types(self):
        result = {}

        for plugin in self.plugins.values():
            result.update(plugin.artifact_classes)

        return result

    # TODO: Should plugin loading be transactional? i.e. if there's
    # something wrong, the entire plugin fails to load any piece, like a
    # databases rollback/commit

    def get_formats(self, *, filter=None, semantic_type=None):
        """
        get_formats(self, *, filter=None, semantic_type=None)

        filter : enum | "IMPORTABLE" | "EXPORTABLE"
            filter is an enum integer that will be used to determine user
            input to output specified formats

        semantic_type : TypeExpression | String
            The semantic type is used to filter the formats associated with
            that specific semantic type

        This method will filter out the formats using the filter provided by
        the user and the semantic type. The return is a dictionary of filtered
        formats keyed on their string names.
        """
        filter_map = {"IMPORTABLE": GetFormatFilters.IMPORTABLE,
                      "EXPORTABLE": GetFormatFilters.EXPORTABLE}
        if filter is not None and not isinstance(filter, GetFormatFilters) \
                and filter not in filter_map:
            raise ValueError(
                f"The provided format filter {filter} is not valid.")
        if isinstance(filter, str):
            filter = filter_map[filter]

        if semantic_type is None:
            formats = set(f.format for f in self.artifact_classes.values())

        else:
            formats = set()

            if isinstance(semantic_type, str):
                semantic_type = parse_type(semantic_type, "semantic")

            if is_semantic_type(semantic_type):
                for artifact_class in self.artifact_classes.values():
                    if semantic_type <= artifact_class.semantic_type:
                        formats.add(artifact_class.format)
                        break

                if not formats:
                    raise ValueError("No formats associated with the type "
                                     f"{semantic_type}.")
            else:
                raise ValueError(f"{semantic_type} is not a valid semantic "
                                 "type.")

        transformable_formats = set(formats)

        if filter is None or GetFormatFilters.IMPORTABLE in filter:
            transformable_formats.update(
                self._get_formats_helper(formats, self._reverse_transformers))

        if filter is None or GetFormatFilters.EXPORTABLE in filter:
            transformable_formats.update(
                self._get_formats_helper(formats, self.transformers))

        result_formats = {}
        for format_ in transformable_formats:
            format_ = format_.__name__
            result_formats[format_] = self.formats[format_]

        return result_formats

    def _get_formats_helper(self, formats, transformer_dict):
        """
        _get_formats_helper(self, formats, transformer_dict)

        formats : Set[DirectoryFormat]
            We are finding all formats that are one transformer away from
            formats in this set

        tranformer_dict : Dict[ str, Dict[str, TransformerReord]]
            The dictionary of transformers allows the method to get formats
            that are transformable from the given format

        This method creates a set utilizing the transformers dictionary and
        the formats set to get related formats for a specific format.
        """
        query_set = set(formats)

        for format_ in formats:
            if issubclass(format_, SingleFileDirectoryFormatBase):
                if format_.file.format.__name__ in self.formats:
                    query_set.add(format_.file.format)

        result_formats = set(query_set)

        for format_ in query_set:
            for transformed_format in transformer_dict[format_]:
                if issubclass(transformed_format, FormatBase):
                    result_formats.add(transformed_format)

                    if issubclass(transformed_format,
                                  SingleFileDirectoryFormatBase):
                        result_formats.add(transformed_format.file.format)

                    if transformed_format in self._ff_to_sfdf:
                        result_formats.update(
                            self._ff_to_sfdf[transformed_format])

        return result_formats

    @property
    def type_formats(self):
        # self.type_formats was replaced with self.artifact_classes - this
        # property provides backward compatibility
        return list(self.artifact_classes.values())

    @property
    def importable_formats(self):
        """Formats that are importable.
        A format is importable in a QIIME 2 deployment if it can be transformed
        into at least one of the canonical semantic type formats.
        """
        return self.get_formats(filter=GetFormatFilters.IMPORTABLE)

    @property
    def exportable_formats(self):
        """Formats that are exportable.
        A format is exportable in a QIIME 2 deployment if it can be transformed
        from at least one of the canonical semantic type formats.
        """
        return self.get_formats(filter=GetFormatFilters.EXPORTABLE)

    @property
    def importable_types(self):
        """Return set of concrete semantic types that are importable.
        A concrete semantic type is importable if it has an associated
        directory format.
        """
        return self.get_semantic_types()

    def get_directory_format(self, semantic_type):
        if not rachis.core.type.is_semantic_type(semantic_type):
            raise TypeError(
                "Must provide a semantic type via `semantic_type`, not %r" %
                semantic_type)

        # TODO: ideally we could just lookup semantic_type in
        # self.artifact_classes but properties get in the way. Is there a way
        # to strip properties so this could be simplified to return
        # self.artifact_classes[semantic_type] while catching a KeyError?
        for artifact_class_record in self.artifact_classes.values():
            if semantic_type <= artifact_class_record.semantic_type:
                return artifact_class_record.format

        # TODO: We need a good way to tell if a semantic type is registered but
        # does not have a directory format. The previous error was causing a
        # lot of confusion.
        #
        # Reference: https://github.com/qiime2/qiime2/issues/514
        raise TypeError(
            "Semantic type %r is invalid, either because it doesn't have a "
            "compatible directory format, or because it's not registered."
            % semantic_type)

    def get_axes(self, semantic_type):
        return {
            pattern: axis for (registered, pattern), axis in self.axes.items()
            if semantic_type <= registered
        }

    def get_axis(self, semantic_type, pattern):
        try:
            return self.get_axes(semantic_type)[pattern.axis_key]
        except KeyError:
            raise ValueError(
                f'{semantic_type!r} has no axis {pattern.axes!r}'
            ) from None
