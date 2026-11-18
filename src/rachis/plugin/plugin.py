# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------
"""
.. For the docs below, we set the following artificial global context
    >>> import builtins

    >>> builtins.__version__ = '0.0.1'
    >>> builtins.website = 'http://github.com/qiime2'
    >>> plugin = Plugin('my-plugin', builtins.__version__, builtins.website)
    >>> builtins.plugin = plugin

    >>> from rachis.core.testing.type import (
    ...     Foo, Bar, C1, IntSequence1, IntSequence2)
    >>> builtins.Foo = Foo
    >>> builtins.Bar = Bar
    >>> builtins.Variant1 = Foo
    >>> builtins.Variant2 = Bar
    >>> builtins.BaseType = C1
    >>> builtins.IntSequence1 = IntSequence1
    >>> builtins.IntSequence2 = IntSequence2

    >>> import pandas
    >>> builtins.pd = pandas

    >>> from typing import Literal
    >>> builtins.Literal = Literal

    >>> from rachis.plugin import TextFileFormat
    >>> class CSVFormat(TextFileFormat):
    ...     def _validate_(self, level):
    ...         pass
    >>> builtins.CSVFormat = CSVFormat

    >>> from rachis.plugin import SingleFileDirectoryFormat
    >>> builtins.CSVDirFormat = SingleFileDirectoryFormat(
    ...     'CSVDirFormat', 'data.csv', builtins.CSVFormat)

    >>> from rachis.plugin import Citations
    >>> builtins.citations = Citations.load('citations.bib',
    ...                                     package='rachis.core.testing')

    >>> from rachis.core.testing.method import optional_artifacts_method
    >>> builtins.my_method = optional_artifacts_method

    >>> from rachis.core.testing.visualizer import most_common_viz
    >>> builtins.my_visualizer = most_common_viz

    >>> from rachis.core.testing.pipeline import collection_pipeline
    >>> builtins.my_pipeline = collection_pipeline

    >>> builtins.example_function_variant1 = lambda x: None
    >>> builtins.example_function_variant2 = lambda x: None

"""
import collections
import inspect
import types
from typing import Any, Optional, Union

from rachis.core.cite import Citations, CitationRecord
import rachis.sdk
import rachis.core.type.grammar as grammar
from rachis.core.validate import ValidationObject
from rachis.plugin.model import DirectoryFormat
from rachis.plugin.model.base import FormatBase
from rachis.core.type import is_semantic_type
from rachis.core.util import get_view_name
from rachis.core.cite import _make_citations_tuple


TransformerRecord = collections.namedtuple(
    'TransformerRecord', ['transformer', 'plugin', 'citations', 'upgrade'])
SemanticTypeRecord = collections.namedtuple(
    'SemanticTypeRecord', ['semantic_type', 'plugin'])
SemanticTypeFragmentRecord = collections.namedtuple(
    'SemanticTypeFragmentRecord', ['fragment', 'plugin'])
FormatRecord = collections.namedtuple('FormatRecord', ['format', 'plugin'])
ViewRecord = collections.namedtuple(
    'ViewRecord', ['name', 'view', 'plugin', 'citations'])
# semantic_type and type_expression will point to the same value in
# ArtifactClassRecords as type_expression is deprecated in favor of
# semantic_type
ArtifactClassRecord = collections.namedtuple(
    'ArtifactClassRecord', ['semantic_type', 'format', 'plugin', 'description',
                            'examples', 'type_expression'])
ValidatorRecord = collections.namedtuple(
    'ValidatorRecord', ['validator', 'view', 'plugin', 'context'])


class Plugin:
    """
    A Rachis Plugin.

    An instance of this class defines all features of a given plugin
    and is instantiated as a module global (i.e. a singleton).

    """

    methods: 'PluginMethods'
    visualizers: 'PluginVisualizers'
    pipelines: 'PluginPipelines'

    def __init__(self, name: str, version: str, website: str,
                 package: Optional[str] = None,
                 project_name: Optional[str] = None,
                 citation_text: Optional[Any] = None,
                 user_support_text: Optional[str] = None,
                 short_description: Optional[str] = None,
                 description: Optional[str] = None,
                 citations: Optional[
                     Union[Citations, 'list[CitationRecord]']] = None):
        """
        Parameters
        ----------
        name
          The name of the plugin (hyphens will be automatically replaced for
          the plugin ID)
        version
          The version of the plugin (this should match the package version)
        website
          A URL to find more information about this plugin
        package
          The Python package name of your plugin. This is largely defunct and
          is set by the entry-point during plugin loading.
        project_name
          The external name of the plugin (distinct from the internal ``name``,
          i.e. q2-my-plugin vs my-plugin). Also defunct and set by the
          entry-point during plugin loading.
        citation_text
          **Deprecated**. Does nothing. Use ``citations`` instead.
        user_support_text
          A message about where to find user support. The default will suggest
          users visit the QIIME 2 forum.
        short_description
          A small (single-line) description to help identify the plugin in a
          list.
        description
          A more complete description of the plugins purpose.
        citations : CitationRecord or list of CitationRecord
          Citation(s) to associate with a result whenever this plugin is
          used. Can also use an entire :py:class:`Citations` object.

        Examples
        --------
        >>> plugin = Plugin('my-plugin', __version__, website)
        """
        self.id = name.replace('-', '_')
        self.name = name
        self.version = version
        self.website = website

        # Filled in by the PluginManager if not provided.
        self.package = package
        self.project_name = project_name

        if user_support_text is None:
            self.user_support_text = ('Please post to the QIIME 2 forum for '
                                      'help with this plugin: https://forum.'
                                      'qiime2.org')
        else:
            self.user_support_text = user_support_text

        if short_description is None:
            self.short_description = ''
        else:
            self.short_description = short_description

        if description is None:
            self.description = ('No description available. '
                                'See plugin website: %s'
                                % self.website)
        else:
            self.description = description

        self.citations = _make_citations_tuple(citations)

        self.methods = PluginMethods(self)
        self.visualizers = PluginVisualizers(self)
        self.pipelines = PluginPipelines(self)

        self.formats = {}
        self.views = {}
        self.type_fragments = {}
        self.transformers = {}
        self.artifact_classes = {}
        self.validators = {}

    def freeze(self):
        pass

    @property
    def actions(self):
        # TODO this doesn't handle method/visualizer name collisions. The
        # auto-generated `rachis.plugins.<plugin-name>.actions` API has the
        # same problem. This should be solved at method/visualizer registration
        # time, which will solve the problem for both APIs.
        actions = {}
        actions.update(self.methods)
        actions.update(self.visualizers)
        actions.update(self.pipelines)
        return types.MappingProxyType(actions)

    @property
    def types(self):
        return self.artifact_classes

    @property
    def type_formats(self):
        # self.type_formats was replaced with self.artifact_classes - this
        # property provides backward compatibility
        return list(self.artifact_classes.values())

    def register_formats(self, *formats, citations=None):
        """Register file formats to the plugin

        Parameters
        ----------
        *formats : TextFileFormat, BinaryFileFormat, or DirectoryFormat
          Formats which are created or defined by this plugin.
        citations : CitatonRecord or list of CitationRecord
          Citation(s) to associate with a result whenever this format is used
          internally. Can also use an entire :py:class:`Citations` object.

        Returns
        -------
        None

        Notes
        -----
        :py:func:`SingleFileDirectoryFormat` returns a
        :py:class:`DirectoryFormat`
        """
        for format in formats:
            if not issubclass(format, FormatBase):
                raise TypeError("%r is not a valid format." % format)

        self.register_views(*formats, citations=citations)

    def register_views(self, *views, citations=None):
        """Register arbitrary views (Python classes or formats) to the plugin

        Parameters
        ----------
        *views : type
          Views which are created or defined by this plugin
        citations : CitationRecord or list of CitationRecord
          Citation(s) to associate with a result whenever this format is used
          internally. Can also use an entire :py:class:`Citations` object.

        Returns
        -------
        None
        """
        citations = _make_citations_tuple(citations)

        for view in views:
            if not isinstance(view, type):
                raise TypeError("%r should be a class." % view)

            is_format = False
            if issubclass(view, FormatBase):
                is_format = True

            name = get_view_name(view)
            if name in self.views:
                raise NameError("View %r is already registered by this "
                                "plugin." % name)

            self.views[name] = ViewRecord(
                name=name, view=view, plugin=self, citations=citations)

            if is_format:
                self.formats[name] = FormatRecord(format=view, plugin=self)

    def register_validator(self, semantic_expression):
        """**Decorator** which registers a validator

        Parameters
        ----------
        semantic_expression : semantic type expression
          An expression (may include operators like union (``|``)) which will
          be compared to an artifact. If the artifact is in the domain of the
          type expression, it will be transformed into a view that matches the
          type annotation of the decorated function and that function will be
          executed.

        Returns
        -------
        decorator
          A decorator which can be applied to a function which takes
          ``data`` as the first argument and ``level`` as the second.
          ``data`` must be annotated with a type which will become the
          transformed view. ``level`` should accept the strings
          ``"min"`` and ``"max"``. It does not necessarily need to change
          behavior based on ``level``, but it is encouraged where it could save
          the user time.

        Examples
        --------
        >>> @plugin.register_validator(Foo | Bar)
        ... def validate_something(data: pd.DataFrame,
        ...                        level: Literal['min', 'max']):
        ...     if data.empty:
        ...         raise ValidationError("This data is empty.")
        """
        if not is_semantic_type(semantic_expression):
            raise TypeError('%s is not a Semantic Type' % semantic_expression)

        def decorator(validator):

            validator_signature = inspect.getfullargspec(validator)

            if 'data' not in validator_signature.annotations:
                raise TypeError('No expected view type provided as annotation'
                                ' for `data` variable in %r.' %
                                (validator.__name__))

            if not ['data', 'level'] == validator_signature.args:
                raise TypeError('The function signature: %r does not contain'
                                ' the required arguments and only the required'
                                ' arguments: %r' % (
                                    validator_signature.args,
                                    ['data', 'level']))

            for semantic_type in semantic_expression:
                if semantic_type not in self.validators:
                    self.validators[semantic_type] = \
                        ValidationObject(semantic_type)

                self.validators[semantic_type].add_validator(
                    ValidatorRecord(
                        validator=validator,
                        view=validator.__annotations__['data'],
                        plugin=self,
                        context=semantic_expression))
            return validator
        return decorator

    def register_transformer(
        self, _fn=None, *, citations=None, upgrade: Optional[bool] = None
    ):
        """ **Decorator** which registers a transformer to convert data

        Parameters
        ----------
        _fn : Callable
          Ignore this parameter as it is the mechanism to allow argumentless
          decoration
        citations : CitationRecord or list of CitationRecord
          Citation(s) to associate with a result whenever this transformer is
          used internally. Can also use an entire :py:class:`Citations` object.
        upgrade : Bool | None
            How to consider this transformer when searching for transformation
            paths between formats. There are three options:

            `True` indicates that the transformer is not lossy, is usable at
            any step in transformer search paths, and takes precedence over
            `False`.

            `False` indicates that the transformer is lossy but can be used in
            transformer search paths only if there are no other satisfactory
            paths that include only `upgrade=True` transformers.

            None indicates that the transformer is lossy and can be used only
            at the terminal ends of search paths. This is the default.

        Returns
        -------
        decorator
          A decorator which can be applied to a function which takes a single
          argument with a type annotation and a single return annotation.
          This decorated function will then be executed whenever data matching
          the type annotation exists and there is a need to view that data as
          the return annotation.

        Notes
        -----
        Since the function is entirely defined by the input and output type,
        the name of the function is usually unimportant and only adds noise. We
        tend to use ``_<number>`` as the name, but any other name may be used.

        Examples
        --------
        >>> @plugin.register_transformer(upgrade=True)
        ... def _0(data: pd.DataFrame) -> CSVFormat:
        ...     ff = CSVFormat()
        ...     with ff.open() as fh:
        ...         data.write_csv(fh)
        ...     return ff

        >>> @plugin.register_transformer(citations=[
        ...     citations['baerheim1994effect'],
        ...     citations['silvers1997effects']
        ... ])
        ... def _1(ff: CSVFormat) -> pd.DataFrame:
        ...     with ff.open() as fh:
        ...         return pd.read_csv(fh)
        """
        # `_fn` allows us to figure out if we are called with or without
        # arguments in order to support both:
        # ```
        # @plugin.register_transformer
        # def _(x: A) -> B:
        #    ...
        # ```
        # and
        # ```
        # @plugin.register_transformer(restrict=True)
        # def _(x: A) -> B:
        #   ...
        # ```
        if (
            upgrade is not True and upgrade is not False
            and upgrade is not None
        ):
            raise TypeError(
                'The upgrade parameter must be one of True, False, or None.'
            )

        citations = _make_citations_tuple(citations)

        def decorator(transformer):
            annotations = transformer.__annotations__.copy()
            if len(annotations) != 2:
                raise TypeError("A transformer must only have a single input"
                                " and output annotation.")
            try:
                output = annotations.pop('return')
            except KeyError:
                raise TypeError("A transformer must provide a return type.")

            if type(output) is tuple:
                raise TypeError("A transformer can only return a single type,"
                                " not %r." % (output,))

            input = list(annotations.values())[0]
            if (input, output) in self.transformers:
                raise TypeError("Duplicate transformer (%r) from %r to %r."
                                % (transformer, input, output))
            if input == output:
                raise TypeError("Plugins should not register identity"
                                " transformations (%r, %r to %r)."
                                % (transformer, input, output))

            self.transformers[input, output] = TransformerRecord(
                transformer=transformer,
                plugin=self,
                citations=citations,
                upgrade=upgrade
            )
            return transformer

        if _fn is None:
            return decorator
        else:
            # Apply the decorator as we were applied with a single function
            return decorator(_fn)

    def register_semantic_types(self, *type_fragments):
        """Register semantic type fragments to the plugin

        Parameters
        ----------
        *type_fragments : semantic types
          Semantic type fragments to register. If a plugin had defined types
          for this expression: ``BaseType[Variant1 | Variant2]`` then
          ``type_fragments`` would be ``BaseType, Variant1, Variant2``

        Returns
        -------
        None

        Examples
        --------
        >>> plugin.register_semantic_types(BaseType, Variant1, Variant2)
        """
        for type_fragment in type_fragments:
            if not is_semantic_type(type_fragment):
                raise TypeError("%r is not a semantic type." % type_fragment)

            if not (isinstance(type_fragment, grammar.IncompleteExp) or
                    (type_fragment.is_concrete() and
                    not type_fragment.fields)):
                raise ValueError("%r is not a semantic type symbol."
                                 % type_fragment)

            if type_fragment.name in self.type_fragments:
                raise ValueError("Duplicate semantic type symbol %r."
                                 % type_fragment)

            self.type_fragments[type_fragment.name] = \
                SemanticTypeFragmentRecord(
                    fragment=type_fragment, plugin=self)

    def _register_artifact_class(self, semantic_type, directory_format,
                                 description, examples):
        if not issubclass(directory_format, DirectoryFormat):
            raise TypeError("%r is not a directory format." % directory_format)
        if not is_semantic_type(semantic_type):
            raise TypeError("%r is not a semantic type." % semantic_type)

        for t in semantic_type:
            if t.predicate is not None:
                raise ValueError("%r has a predicate, differentiating format"
                                 " on predicate is not supported.")

        if description is None:
            description = ""
        if examples is None:
            examples = {}

        # register_semantic_type_to_format can accept type expressions such as
        # Kennel[Dog | Cat]. By iterating, we will register the concrete types
        # (e.g., Kennel[Dog] and Kennel[Cat], not the type expression)
        for e in list(semantic_type):
            semantic_type_str = str(e)
            if semantic_type_str in self.artifact_classes:
                raise NameError("Artifact class %s was registered more than "
                                "once. Artifact classes can only be "
                                "registered once." % semantic_type_str)

            self.artifact_classes[semantic_type_str] =\
                ArtifactClassRecord(
                    semantic_type=e, format=directory_format,
                    plugin=self, description=description,
                    examples=types.MappingProxyType(examples),
                    type_expression=e)

    def register_semantic_type_to_format(self, semantic_type,
                                         artifact_format=None,
                                         directory_format=None):
        """Connect a semantic type expression to a format. **Deprecated**

        Permits an arbitrary type expression and expands it to all concrete
        variants which are then associated with the supplied
        ``directory_format``.

        As this does not support documentation or examples, it is recommended
        to use :py:meth:`Plugin.register_artifact_class` instead, which takes
        a single concrete expression, but allows for additional specific
        information.

        Parameters
        ----------
        semantic_type : semantic type expression
          A semantic type expression which may include operators.
        artifact_format : type
          **Super deprecated**
        directory_format : subclass of DirectoryFormat
          A directory format which will define the ``/data/`` directory of a
          stored artifact.

        Returns
        -------
        None
        """
        # Handle the deprecated parameter name, artifact_format. This is being
        # replaced with directory_format for clarity.
        if artifact_format is not None and directory_format is not None:
            raise ValueError('directory_format and artifact_format were both'
                             'provided when registering artifact class %s.'
                             'Please provide directory_format only as '
                             'artifact_format is deprecated.'
                             % str(semantic_type))
        elif artifact_format is None and directory_format is None:
            raise ValueError('directory_format or artifact_format must be '
                             'provided when registering artifact class %s.'
                             'Please provide directory_format only as '
                             'artifact_format is deprecated.'
                             % str(semantic_type))
        else:
            directory_format = directory_format or artifact_format

        self._register_artifact_class(semantic_type=semantic_type,
                                      directory_format=directory_format,
                                      description=None,
                                      examples=None)

    def register_artifact_class(self, semantic_type, directory_format,
                                description=None, examples=None):
        """Register an artifact class which defines an Artifact

        Parameters
        ----------
        semantic_type : type expression
          A *concrete* type expression which will be the type of this artifact
          class.
        directory_format : subclass of DirectoryFormat
          A directory format which will define the ``/data/`` directory of a
          stored artifact.
        description : str
          A description of what this artifact will represent.
        examples : dict[str, callable]
          A dict of example name to usage example functions which take a
          single argument (the usage driver, a.k.a. ``use``). Each function
          which will demonstrate importing this data when executed.

        Returns
        -------
        None

        Examples
        --------
        >>> plugin.register_artifact_class(
        ...     semantic_type=BaseType[Variant1],
        ...     directory_format=CSVDirFormat,
        ...     description="This data represents something important.",
        ...     examples={
        ...         'example1': example_function_variant1,
        ...         'example2': example_function_variant2
        ...     }
        ... )
        """
        if not semantic_type.is_concrete():
            raise TypeError("Only a single type can be registered at a time "
                            "with register_artifact_class. Registration "
                            "attempted for %s." % str(semantic_type))
        self._register_artifact_class(
            semantic_type, directory_format, description, examples)


class PluginActions(dict):
    def __init__(self, plugin):
        self._plugin_id = plugin.id
        super().__init__()


class PluginMethods(PluginActions):
    """Accessed via ``plugin.methods``"""
    def register_function(self, function, inputs, parameters, outputs, name,
                          description, input_descriptions=None,
                          parameter_descriptions=None,
                          output_descriptions=None, citations=None,
                          deprecated=False, migrated=False, examples=None):
        """Register a method to the associated plugin.

        Parameters
        ----------
        function : callable
          A function which will be called when the user uses this method.
        inputs : dict[str, type expression]
          A dictionary of function-parameter names to semantic type
          expressions. The keys of the dictionary must match the parameter
          names which are on the ``function``. Collections and type variables
          are also permitted so long as they contain semantic types.
        parameters : dict[str, type expression]
          A dictionary of function parameter names to primitive type
          expressions. The keys of the dictionary must match the parameter
          names which are on the ``function``. Collections and type variables
          are also permitted so long as they contain primitive types.
        outputs : dict[str, type expression]
          A dictionary of named outputs to *concrete* semantic types. The keys
          of the dictionary will become the parameter names for these outputs
          and must match the number of annotated outputs on the ``function``.
          Collections and type variables are also permitted so long as they
          contain semantic types. (In older versions of QIIME 2 this was a list
          of tuples as dictionaries were not yet ordered.)
        name : str
          A short description, ideally a human-oriented name or title.
        description : str
          A longer description of the action.
        input_descriptions: dict[str, str]
          A dictionary of input names (see ``inputs``) to descriptions.
        parameter_descriptions: dict[str, str]
          A dictionary of parameter names (see ``parameters``) to descriptions.
        output_descriptions: dict[str, str]
          A dictionary of output names (see ``outputs``) to descriptions.
        citations : CitationRecord or list of CitationRecord
          Citation(s) to associate with a result whenever this action is
          used. Can also use an entire :py:class:`.Citations` object.
        deprecated : bool
          Whether this action is deprecated and should be migrated away from.
        migrated : bool | Mapping[str, str]
          Either False (default) or a mapping with keys:
          {'to_plugin', 'from_distro', 'to_distro', 'epoch'}
          indicating where/when this action will be migrated.
        examples : dict[str, callable]
          A dict of example name to usage example functions which take a
          single argument (the usage driver, a.k.a. ``use``). Each function
          will carry out some example situation for this action when executed.

        Returns
        -------
        None

        Examples
        --------
        >>> from rachis.plugin import Int
        >>> plugin.methods.register_function(
        ...     function=my_method,
        ...     inputs={
        ...         'ints': IntSequence1,
        ...         'optional1': IntSequence1,
        ...         'optional2': IntSequence1 | IntSequence2
        ...     },
        ...     parameters={
        ...         'num1': Int,
        ...         'num2': Int
        ...     },
        ...     outputs={
        ...         'output': IntSequence1
        ...     },
        ...     name='My cool method',
        ...     description='It does something very clever and interesting.'
        ... )
        """
        citations = _make_citations_tuple(citations)

        if examples is None:
            examples = {}

        method = rachis.sdk.Method._init(function, inputs, parameters, outputs,
                                         self._plugin_id, name, description,
                                         input_descriptions,
                                         parameter_descriptions,
                                         output_descriptions, citations,
                                         deprecated, migrated, examples)
        self[method.id] = method


class PluginVisualizers(PluginActions):
    """Accessed via ``plugin.visualizers``"""
    def register_function(self, function, inputs, parameters, name,
                          description, input_descriptions=None,
                          parameter_descriptions=None, citations=None,
                          deprecated=False, migrated=False, examples=None):
        """Register a visualizer to the associated plugin.

        Parameters
        ----------
        function : callable
          A function which will be called when the user uses this method. This
          function receives an ``output_dir`` as the first argument.
        inputs : dict[str, type expression]
          A dictionary of function-parameter names to semantic type
          expressions. The keys of the dictionary must match the parameter
          names which are on the ``function``. Collections and type variables
          are also permitted so long as they contain semantic types.
        parameters : dict[str, type expression]
          A dictionary of function parameter names to primitive type
          expressions. The keys of the dictionary must match the parameter
          names which are on the ``function``. Collections and type variables
          are also permitted so long as they contain primitive types.
        name : str
          A short description, ideally a human-oriented name or title.
        description : str
          A longer description of the action.
        input_descriptions: dict[str, str]
          A dictionary of input names (see ``inputs``) to descriptions.
        parameter_descriptions: dict[str, str]
          A dictionary of parameter names (see ``parameters``) to descriptions.
        citations : CitationRecord or list of CitationRecord
          Citation(s) to associate with a result whenever this action is
          used. Can also use an entire :py:class:`.Citations` object.
        deprecated : bool
          Whether this action is deprecated and should be migrated away from.
        migrated : bool | Mapping[str, str]
          Either False (default) or a mapping with keys:
          {'to_plugin', 'from_distro', 'to_distro', 'epoch'}
          indicating where/when this action will be migrated.
        examples : dict[str, callable]
          A dict of example name to usage example functions which take a
          single argument (the usage driver, a.k.a. ``use``). Each function
          will carry out some example situation for this action when executed.

        Notes
        -----
        Unlike methods and pipelines, there are no registered outputs as every
        visualizer returns a single ``Visualization``  output.

        Examples
        --------
        >>> plugin.visualizers.register_function(
        ...     function=my_visualizer,
        ...     inputs={'ints': IntSequence1 | IntSequence2},
        ...     parameters={},
        ...     name='Visualize most common integers',
        ...     description='Produces a ranked list of integers.',
        ...     citations=[citations['witcombe2006sword']]
        ... )
        """
        citations = _make_citations_tuple(citations)

        if examples is None:
            examples = {}

        visualizer = rachis.sdk.Visualizer._init(function, inputs, parameters,
                                                 self._plugin_id, name,
                                                 description,
                                                 input_descriptions,
                                                 parameter_descriptions,
                                                 citations, deprecated,
                                                 migrated, examples)
        self[visualizer.id] = visualizer


class PluginPipelines(PluginActions):
    """Accessed via ``plugin.pipelines``"""
    def register_function(self, function, inputs, parameters, outputs, name,
                          description, input_descriptions=None,
                          parameter_descriptions=None,
                          output_descriptions=None, citations=None,
                          deprecated=False, migrated=False, examples=None):
        """Register a pipeline to the associated plugin.

        Parameters
        ----------
        function : callable
          A function which will be called when the user uses this method. This
          function receives a Context object ``ctx`` as its first argument.
        inputs : dict[str, type expression]
          A dictionary of function-parameter names to semantic type
          expressions. The keys of the dictionary must match the parameter
          names which are on the ``function``. Collections and type variables
          are also permitted so long as they contain semantic types.
        parameters : dict[str, type expression]
          A dictionary of function parameter names to primitive type
          expressions. The keys of the dictionary must match the parameter
          names which are on the ``function``. Collections and type variables
          are also permitted so long as they contain primitive types.
        outputs : dict[str, type expression]
          A dictionary of named outputs to *concrete* semantic types. The keys
          of the dictionary will become the parameter names for these outputs
          and must match the number of annotated outputs on the ``function``.
          Collections and type variables are also permitted so long as they
          contain semantic types. (In older versions of QIIME 2 this was a list
          of tuples as dictionaries were not yet ordered.)
        name : str
          A short description, ideally a human-oriented name or title.
        description : str
          A longer description of the action.
        input_descriptions: dict[str, str]
          A dictionary of input names (see ``inputs``) to descriptions.
        parameter_descriptions: dict[str, str]
          A dictionary of parameter names (see ``parameters``) to descriptions.
        output_descriptions: dict[str, str]
          A dictionary of output names (see ``outputs``) to descriptions.
        citations : CitationRecord or list of CitationRecord
          Citation(s) to associate with a result whenever this action is
          used. Can also use an entire :py:class:`.Citations` object.
        deprecated : bool
          Whether this action is deprecated and should be migrated away from.
        migrated : bool | Mapping[str, str]
          Either False (default) or a mapping with keys:
          {'to_plugin', 'from_distro', 'to_distro', 'epoch'}
          indicating where/when this action will be migrated.
        examples : dict[str, callable]
          A dict of example name to usage example functions which take a
          single argument (the usage driver, a.k.a. ``use``). Each function
          will carry out some example situation for this action when executed.

        Examples
        --------
        >>> from rachis.plugin import Collection
        >>> plugin.pipelines.register_function(
        ...     function=my_pipeline,
        ...     inputs={'ints': Collection[IntSequence1]},
        ...     parameters={},
        ...     outputs={'output': Collection[IntSequence1]},
        ...     name='Do thing multiple times',
        ...     description='Takes a collection and returns a better one'
        ... )
        """
        citations = _make_citations_tuple(citations)

        if examples is None:
            examples = {}

        pipeline = rachis.sdk.Pipeline._init(function, inputs, parameters,
                                             outputs, self._plugin_id, name,
                                             description, input_descriptions,
                                             parameter_descriptions,
                                             output_descriptions, citations,
                                             deprecated, migrated, examples)
        self[pipeline.id] = pipeline
