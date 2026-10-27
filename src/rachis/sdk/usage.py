# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------
"""
The Usage API provides an interface-agnostic way for QIIME 2 plugin
developers to define examples of how to use their plugin’s actions. This
enables the programmatic generation of examples for all QIIME 2 interfaces,
eliminating the need to maintain specific examples for multiple interfaces.

**Importantly there are two sides to the API**, the usage example side, and the
interface driver side. A usage example must never call a method which is
intended for a usage driver. These methods will be denoted with the following
admonition:

Warning
-------
  For use by interface drivers only. Do not use in a written usage example.

Interface developers may want to pay special attention to these methods, as
they will likely simplify their code.

If the above warning is not present, then the method is likely intended to be
used to describe some example and may be used by an example writer, or
overriden by a usage driver.

For the docs below, we set the following artificial global, as if we were
always in a usage example with a ``use`` variable defined. This is only to fool
the doctest module. This should never be done in the real world.

>>> import builtins
>>> builtins.use = ExecutionUsage()

"""
from typing import Set, List, Literal, Any, Callable, Type, Union
import dataclasses
import functools
import re

import rachis
from rachis import sdk
from rachis.core.type import (
    is_semantic_type, is_visualization_type, is_collection_type
)


def assert_usage_var_type(usage_variable, *valid_types):
    """Testing utility to assert a usage variable has the right type.

    Parameters
    ----------
    usage_variable : `rachis.sdk.usage.UsageVariable`
        The usage variable to test.
    *valid_types : 'artifact', 'artifact_collection', 'visualization',
                   'visualization_collection', 'metadata', 'column', 'format'
        The valid variable types to expect.

    Raises
    ------
    AssertionError
        If the variable is not the correct type.
    """
    if usage_variable.var_type not in valid_types:
        tmpl = (
            usage_variable.name, valid_types, usage_variable.var_type,
        )
        raise AssertionError('Incorrect var_type for %s, need %s got %s'
                             % tmpl)


class UsageAction:
    """An object which represents a deferred lookup for a QIIME 2 action.

    One of three "argument objects" used by :meth:`Usage.action`. The other two
    are :class:`UsageInputs` and :class:`UsageOutputNames`.

    """
    def __init__(self, plugin_id: str, action_id: str):
        """Constructor for UsageAction.

        The parameters should identify an existing plugin and action of that
        plugin.

        Important
        ---------
        There should be an existing plugin manager by the time this
        object is created, or an error will be raised. Typically instantiation
        happens by executing an example, so this will generally be true.

        Parameters
        ----------
        plugin_id : str
            The (typically under-scored) name of a plugin, e.g. "my_plugin".
        action_id : str
            The (typically under-scored) name of an action, e.g. "my_action".

        Raises
        ------
        rachis.sdk.UninitializedPluginManagerError
            If there is not an existing plugin manager to define the available
            plugins.

        Examples
        --------
        >>> results = use.action(
        ...     use.UsageAction(plugin_id='dummy_plugin',
        ...                     action_id='params_only_method'),
        ...     use.UsageInputs(name='foo', age=42),
        ...     use.UsageOutputNames(out='bar1')
        ... )
        >>> results.out
        <ExecutionUsageVariable name='bar1', var_type='artifact'>

        See Also
        --------
        UsageInputs
        UsageOutputNames
        Usage.action
        rachis.sdk.PluginManager
        """
        if plugin_id == '':
            raise ValueError('Must specify a value for plugin_id.')

        if action_id == '':
            raise ValueError('Must specify a value for action_id.')

        self.plugin_id: str = plugin_id
        """The (typically under-scored) name of a plugin, e.g. "my_plugin".

        Warning
        -------
        For use by interface drivers only.
        Do not use in a written usage example.
        """
        self.action_id: str = action_id
        """The (typically under-scored) name of an action, e.g. "my_action".

        Warning
        -------
        For use by interface drivers only.
        Do not use in a written usage example.
        """

        try:
            self._plugin_manager = sdk.PluginManager.reuse_existing()
        except sdk.UninitializedPluginManagerError:
            raise sdk.UninitializedPluginManagerError(
                'Please create an instance of sdk.PluginManager'
            )

    def __repr__(self):
        return 'UsageAction(plugin_id=%r, action_id=%r)' %\
            (self.plugin_id, self.action_id)

    def get_action(self) -> sdk.Action:
        """Retrieve the actual SDK object (rachis.sdk.Action)

        Warning
        -------
        For use by interface drivers only. Do not use in a written usage
        example.

        Returns
        -------
        action : instance of rachis.sdk.Action subclass

        Raises
        ------
        KeyError
            If the action parameterized by this object does not exist in the
            pre-initialized plugin manager.
        """
        plugin = self._plugin_manager.get_plugin(id=self.plugin_id)
        try:
            action_f = plugin.actions[self.action_id]
        except KeyError:
            raise KeyError('No action currently registered with '
                           'id: "%s".' % (self.action_id,))
        return action_f


class UsageInputs:
    """A dict-like mapping of parameters to arguments for invoking an action.

    One of three "argument objects" used by :meth:`Usage.action`. The other two
    are :class:`UsageAction` and :class:`UsageOutputNames`.

    Parameters should match the signature of the associated action, and
    arguments may be `UsageVariable` s or primitive values.

    """
    def __init__(self, **kwargs):
        """Constructor for UsageInputs.

        Parameters
        ----------
        **kwargs : primitive or UsageVariable
            The keys used should match the signature of the action. The values
            should be valid arguments of the action or variables of such
            arguments.

        Examples
        --------
        >>> results = use.action(
        ...     use.UsageAction(plugin_id='dummy_plugin',
        ...                     action_id='params_only_method'),
        ...     use.UsageInputs(name='foo', age=42),
        ...     use.UsageOutputNames(out='bar2')
        ... )
        >>> results.out
        <ExecutionUsageVariable name='bar2', var_type='artifact'>

        See Also
        --------
        UsageAction
        UsageOutputNames
        Usage.action
        """
        self.values = kwargs

    def __repr__(self):
        return 'UsageInputs(**%r)' % (self.values,)

    def __getitem__(self, key):
        """Same as a dictionary.

        Warning
        -------
        For use by interface drivers only. Do not use in a written usage
        example.
        """
        return self.values[key]

    def __contains__(self, key):
        """Same as a dictionary.

        Warning
        -------
        For use by interface drivers only. Do not use in a written usage
        example.
        """
        return key in self.values

    def items(self):
        """Same as a dictionary.

        Warning
        -------
        For use by interface drivers only. Do not use in a written usage
        example.
        """
        return self.values.items()

    def keys(self):
        """Same as a dictionary.

        Warning
        -------
        For use by interface drivers only. Do not use in a written usage
        example.
        """
        return self.values.keys()

    def values(self):
        """Same as a dictionary.

        Warning
        -------
        For use by interface drivers only. Do not use in a written usage
        example.
        """
        return self.values.values()

    def map_variables(self, function):
        """Convert variables into something else, leaving primitives alone.

        Warning
        -------
        For use by interface drivers only. Do not use in a written usage
        example.

        Parameters
        ----------
        function : Callable[[UsageVariable], Any]
            The function to map over all variables. This function will not be
            called on any primitive values.

        Returns
        -------
        dict
            A new dictionary of key-value pairs where all variables have been
            converted by `function`.

        Examples
        --------
        >>> # Example situation
        >>> var = use.usage_variable('foo', lambda: ..., 'artifact')
        >>> inputs = UsageInputs(foo=var, bar='bar')

        >>> inputs.map_variables(lambda v: v.to_interface_name())
        {'foo': 'foo', 'bar': 'bar'}

        >>> inputs.map_variables(lambda v: v.execute())
        {'foo': ..., 'bar': 'bar'}

        See Also
        --------
        UsageVariable.to_interface_name
        UsageVariable.execute
        """
        result = {}

        def mapped(v):
            if isinstance(v, UsageVariable):
                assert_usage_var_type(v, 'artifact', 'artifact_collection',
                                      'visualization_collection',
                                      'metadata', 'column')
                v = function(v)
            return v

        for name, value in self.items():
            if isinstance(value, (list, set)):
                collection_type = type(value)
                value = [mapped(v) for v in value]
                value = collection_type(value)
            else:
                value = mapped(value)

            result[name] = value

        return result


class UsageOutputNames:
    """A dict-like mapping of action outputs to desired names.

    One of three "argument objects" used by :meth:`Usage.action`. The other two
    are :class:`UsageAction` and :class:`UsageInputs`.

    All names must be strings.

    Note
    ----
    The order defined by this object will dictate the order of the variables
    returned by :meth:`Usage.action`.

    """
    def __init__(self, **kwargs):
        """Constructor for UsageOutputNames.

        Parameters
        ----------
        **kwargs : str
            The name of the resulting variables to be returned by
            :meth:`Usage.action`.

        Raises
        ------
        TypeError
            If the values provided are not strings.

        Examples
        --------
        >>> results = use.action(
        ...     use.UsageAction(plugin_id='dummy_plugin',
        ...                     action_id='params_only_method'),
        ...     use.UsageInputs(name='foo', age=42),
        ...     use.UsageOutputNames(out='bar3')
        ... )
        >>> results.out
        <ExecutionUsageVariable name='bar3', var_type='artifact'>

        See Also
        --------
        UsageAction
        UsageInputs
        Usage.action
        """
        self.validate_names(kwargs)
        self.values = kwargs

    def __repr__(self):
        return 'UsageOutputNames(**%r)' % (self.values, )

    def __getitem__(self, key):
        """Same as a dictionary.

        Warning
        -------
        For use by interface drivers only. Do not use in a written usage
        example.
        """
        return self.values[key]

    def __contains__(self, key):
        """Same as a dictionary.

        Warning
        -------
        For use by interface drivers only. Do not use in a written usage
        example.
        """
        return key in self.values

    def validate_names(self, kwargs):
        """
        Validates that all names for output vars are strings and follow our
        usual naming rules of Python identifier + -

        Warning
        -------
        For use by interface drivers only. Do not use in a written usage
        example.

        kwargs : str
            The name of the resulting variables to be returned by
            :meth:`Usage.action`.

        Raises
        ------
        ValueError
            If the value passed in is not a string or not a valid Python
            identifier.
        """
        for key, val in kwargs.items():
            if not isinstance(val, str):
                raise TypeError(
                    'Name provided for key %r must be a string, not a %r.' %
                    (key, type(val)))
            rachis.util.is_valid_python_identifier(val, 'name')


    def items(self):
        """Same as a dictionary.

        Warning
        -------
        For use by interface drivers only. Do not use in a written usage
        example.
        """
        return self.values.items()

    def keys(self):
        """Same as a dictionary.

        Warning
        -------
        For use by interface drivers only. Do not use in a written usage
        example.
        """
        return self.values.keys()

    def values(self):
        """Same as a dictionary.

        Warning
        -------
        For use by interface drivers only. Do not use in a written usage
        example.
        """
        return self.values.values()


class UsageOutputs(sdk.Results):
    """A vanity class over :class:`rachis.sdk.Results`.

    Returned by :meth:`Usage.action` with order defined by
    :class:`UsageOutputNames`.
    """
    pass


VAR_TYPES = ('artifact', 'artifact_collection', 'visualization',
             'visualization_collection', 'metadata', 'column', 'format')
T_VAR_TYPES = Literal['artifact', 'artifact_collection', 'visualization',
                      'visualization_collection',
                      'metadata', 'column', 'format']
COLLECTION_VAR_TYPES = ('artifact_collection', 'visualization_collection')


class UsageVariable:
    """A variable which represents some QIIME 2 generate-able value.

    These should not be used to represent primitive values such as strings,
    numbers, booleans, or lists/sets thereof.

    """
    DEFERRED = object()
    VAR_TYPES = VAR_TYPES
    COLLECTION_VAR_TYPES = COLLECTION_VAR_TYPES

    def __init__(self, name: str, factory: Callable[[], Any],
                 var_type: T_VAR_TYPES, usage: 'Usage'):
        """Constructor for UsageVariable. Generally initialized for you.

        Warning
        -------
        For use by interface drivers only (and rarely at that).
        Do not use in a written usage example.

        Parameters
        ----------
        name : str
            The name of this variable (interfaces will use this as a starting
            point).
        factory : Callable[[], Any]
            A function which will return a realized value of `var_type`.
        var_type : 'artifact', 'artifact_collection', 'visualization',
                   'visualization_collection', 'metadata', 'column', 'format'
            The type of value which will be returned by the factory.
            Most are self-explanatory, but "format" indicates that the factory
            produces a QIIME 2 file format or directory format, which is used
            for importing data.
        usage : Usage
            The currently executing usage driver. Provided for convenience.
        """
        if not callable(factory):
            raise TypeError('value for `factory` should be a `callable`, '
                            'recieved %s' % (type(factory),))

        if var_type not in self.VAR_TYPES:
            raise ValueError('value for `var_type` should be one of %r, '
                             'received %s' % (self.VAR_TYPES, var_type))

        self.name: str = name
        """The name of the variable, may differ from :meth:`to_interface_name`.

        Warning
        -------
        For use by interface drivers only.
        Do not use in a written usage example.
        """
        self.factory: Callable[[], Any] = factory
        """The factory which produces the value. Generally :meth:`execute`
        should be used as it will calculate the results once, instead of
        generating a new object each time.

        Warning
        -------
        For use by interface drivers only (and rarely at that).
        Do not use in a written usage example.
        """
        self.var_type: Literal['artifact', 'artifact_collection',
                               'visualization', 'visualization_collection',
                               'metadata', 'column', 'format'] = var_type
        """The general type of this variable.

        Warning
        -------
        For use by interface drivers only.
        Do not use in a written usage example.
        """
        self.value: Any = self.DEFERRED
        """The value of this variable, or DEFERRED. See :attr:`is_deferred`.

        Warning
        -------
        For use by interface drivers only.
        Do not use in a written usage example.
        """
        self.use: Usage = usage
        """The current :class:`Usage` instance being used. Typically this is
        an instance of a subclass.

        Warning
        -------
        For use by interface drivers only.
        It won't break anything, but it would be super-super-super weird to
        use in a written usage example.
        """

    def __repr__(self):
        return '<%s name=%r, var_type=%r>' % (self.__class__.__name__,
                                              self.name, self.var_type)

    @property
    def is_deferred(self) -> bool:
        """Check if the value of this variable is available.

        Warning
        -------
        For use by interface drivers only. Do not use in a written usage
        example.
        """
        return self.value is self.DEFERRED

    def execute(self) -> Any:
        """Execute the factory to produce a value, this is stored and returned.

        Warning
        -------
        For use by interface drivers only. Do not use in a written usage
        example.

        Examples
        --------
        >>> var = UsageVariable('foo', lambda: '<pretend artifact>',
        ...                     'artifact', use)
        >>> var.value
        <object object ...>
        >>> var.execute()
        '<pretend artifact>'
        >>> var.value
        '<pretend artifact>'

        See Also
        --------
        factory
        value
        """
        if self.is_deferred:
            self.value = self.factory()
        return self.value

    def save(self, filepath: str, ext: str = None) -> str:
        """Save the value of this variable to a filepath.
        The final path is returned.

        Warning
        -------
        For use by interface drivers only. Do not use in a written usage
        example.

        Parameters
        ----------
        filepath : path
            The filepath to save to.
        ext : str
            The extension to append. May be 'ext' or '.ext'. If the extension
            is already present on filepath, it is not added.

        Returns
        -------
        path
            Path saved to, including the extension if added.
        """
        value = self.execute()
        return value.save(filepath, ext=ext)

    def to_interface_name(self) -> str:
        """Convert this variable to an interface-specific name.

        Warning
        -------
          For use by interface drivers only. Do not use in a written usage
          example.

        This method should generally be overriden by a driver to be
        interface-specific.

        Examples
        --------
        >>> class MyUsageVariable(UsageVariable):
        ...     def to_interface_name(self):
        ...         return '<option> ' + self.name.replace('_', '-')
        >>> var = MyUsageVariable('foo_bar', lambda: ..., 'artifact', use)
        >>> var.to_interface_name()
        '<option> foo-bar'

        """
        return self.name

    def assert_has_line_matching(self, path: str, expression: str,
                                 key: str = None):
        """Communicate that the result of this variable should match a regex.

        The default implementation is to do nothing.

        Parameters
        ----------
        path : str
            The relative path in a result's /data/ directory to check.

        expression : str
            The regular expression to evaluate for a line within `path`.

        key : str
            The key to match against a given semantic type
            if the output is a ResultCollection.

        Note
        ----
        Should not be called on non-artifact variables.

        Examples
        --------
        >>> bar, = use.action(
        ...     use.UsageAction(plugin_id='dummy_plugin',
        ...                     action_id='params_only_method'),
        ...     use.UsageInputs(name='foo', age=42),
        ...     use.UsageOutputNames(out='bar4')
        ... )
        >>> bar.assert_has_line_matching('mapping.tsv', r'foo\\s42')
        ...
        >>> # A factory which will be used in the example to generate data.
        >>> def factory():
        ...     import rachis
        ...     # This type is only available during testing.
        ...     # A real example would use a real type.
        ...     a = rachis.ResultCollection(
        ...         {'Foo': rachis.Artifact.import_data('SingleInt', 1),
        ...          'Bar': rachis.Artifact.import_data('SingleInt', 2)})
        ...     return a
        ...
        >>> int_collection = use.init_artifact_collection('int_collection6', factory)
        >>> bar, = use.action(
        ...     use.UsageAction(plugin_id='dummy_plugin',
        ...                     action_id='dict_of_ints'),
        ...     use.UsageInputs(ints=int_collection),
        ...     use.UsageOutputNames(output='bar5')
        ... )
        >>> bar.assert_has_line_matching('file1.txt', r'1', 'Foo')
        >>> bar.assert_has_line_matching('file1.txt', r'2', 'Bar')
        >>> bar.assert_has_line_matching('file2.txt', r'1', 'Foo')
        >>> bar.assert_has_line_matching('file2.txt', r'2', 'Bar')
        """  # noqa: E501
        pass

    def assert_output_type(self, semantic_type: str, key: str = None):
        """Communicate that this variable should have a given semantic type.

        The default implementation is to do nothing.

        Parameters
        ----------
        semantic_type : QIIME 2 Semantic Type or str
            The semantic type to match.

        key : str
            The key to match against a given semantic type
            if the output is a ResultCollection.

        Note
        ----
        Should not be called on non-artifact variables.

        Examples
        --------
        >>> bar, = use.action(
        ...     use.UsageAction(plugin_id='dummy_plugin',
        ...                     action_id='params_only_method'),
        ...     use.UsageInputs(name='foo', age=42),
        ...     use.UsageOutputNames(out='bar6')
        ... )
        >>> bar.assert_output_type('Mapping')
        ...
        >>> # A factory which will be used in the example to generate data.
        >>> def factory():
        ...     import rachis
        ...     # This type is only available during testing.
        ...     # A real example would use a real type.
        ...     a = rachis.ResultCollection(
        ...         {'Foo': rachis.Artifact.import_data('SingleInt', 1),
        ...          'Bar': rachis.Artifact.import_data('SingleInt', 2)})
        ...     return a
        ...
        >>> int_collection = use.init_artifact_collection('int_collection7', factory)
        >>> bar, = use.action(
        ...     use.UsageAction(plugin_id='dummy_plugin',
        ...                     action_id='dict_of_ints'),
        ...     use.UsageInputs(ints=int_collection),
        ...     use.UsageOutputNames(output='bar7')
        ... )
        >>> bar.assert_output_type(semantic_type='SingleInt', key='Foo')
        ...
        """  # noqa: E501
        pass


class Usage:
    """The base implementation of a Usage diver.

    Typically a usage driver will override some method. For example,
    :meth:`action`, to perform some interesting functionality.

    >>> def action(self, action, inputs, outputs):
    ...    # First a driver should call super, to generate variables
    ...    variables = super().action(action, inputs, output)
    ...
    ...    # then something can be done with them such as:
    ...    for key, var in variables._asdict().items():
    ...        self.some_stateful_object[key] = var.execute()
    ...    # or perhaps the inputs are more interesting:
    ...    self.other_state.update(
    ...        inputs.map_variables(lambda x: x.execute()))
    ...
    ...    # always remember to return what the super()'ed call returns
    ...    return variables

    This is the typical "sandwich pattern" for overriding the methods which
    communicate some action to perform.

    1. Call ``super().method()`` and collect the results
    2. Do something interesting
    3. Return the results

    There are many methods available for a driver implementation to override.
    For examples of the above pattern, see the source code for the built-in
    implementations of: :class:`DiagnosticUsage`, :class:`ExecutionUsage`, and
    :class:`rachis.plugins.ArtifactAPIUsage`
    """
    # Users and Interfaces are free to override this as an escape hatch.
    # the structure below is a common format for user-agent strings
    USER_AGENT = f'rachis/{rachis.__version__} (+https://rachis.org)'

    # these are here for namespace/import convenience
    UsageAction: Type[UsageAction] = UsageAction
    UsageInputs: Type[UsageInputs] = UsageInputs
    UsageOutputNames: Type[UsageOutputNames] = UsageOutputNames
    # NOTE: not exporting UsageOutputs here because example writers shouldn't
    # be instantiating those on their own, anyway.

    def __init__(self, asynchronous: bool = False):
        """Constructor for Usage.

        Warning
        -------
          For use by interface drivers only. Do not use in a written usage
          example.

        """

        self.asynchronous: bool = asynchronous
        """Whether the execution should be represented via `.asynchronous()`
        calls. This can typically be ignored in subclasses.

        Warning
        -------
        For use by interface drivers only.
        Do not use in a written usage example.
        """
        self.namespace: Set[str] = set()
        """A set of names which may collide in a given context.
        A driver should add strings to this set as is needed; any variables
        created will have their interface name compared to, and added to this
        set.

        Warning
        -------
        For use by interface drivers only.
        Do not use in a written usage example.

        See Also
        --------
        UsageVariable.to_interface_name
        """

    def _usage_variable(self, name, factory, var_type):
        variable = self.usage_variable(name, factory, var_type)
        var_name = variable.to_interface_name()
        if var_name in self.namespace:
            raise ValueError(
                '%r namespace collision (%r)' % (variable, var_name))
        self.namespace.add(var_name)
        return variable

    def usage_variable(self, name: str, factory: Callable[[], Any],
                       var_type: T_VAR_TYPES) -> UsageVariable:
        """Initialize a UsageVariable class (called by the base implementation)

        Warning
        -------
          For use by interface drivers only. Do not use in a written usage
          example.

        This should be overriden to specialize the class used by your driver.

        Examples
        --------
        >>> def usage_variable(self, name, factory, var_type):
        ...     return MyUsageVariable(name, factory, var_type, self,
        ...                            my_extra_thing='magic')

        """
        return UsageVariable(name, factory, var_type, self)

    def render(self, flush: bool = False) -> Any:
        """Return some rendering of the state up to this point.

        Warning
        -------
          For use by interface drivers only. Do not use in a written usage
          example.

        The default implementation raises `NotImplementedError`.

        Parameters
        ----------
        flush: bool
            Whether to "flush" (forget) the rendered state after calling, or
            not. This is useful if the driver is calling `render` many times.

        Returns
        -------
        Any
            The output is driver specific, but usually a string or list.

        """
        raise NotImplementedError

    def init_artifact(self, name: str,
                      factory: Callable[[], rachis.Artifact]) -> UsageVariable:
        """Communicate that an artifact will be needed.

        Driver implementations may use this to intialize data for an example.

        Parameters
        ----------
        name : str
            The canonical name of the variable to be returned.
        factory : Callable which returns :class:`rachis.sdk.Artifact`
            A function which takes no parameters, and returns an artifact.
            This function may do anything internally to create the artifact.

        Returns
        -------
        UsageVariable
            This particular return class can be changed by a driver which
            overrides :meth:`usage_variable`.

        Examples
        --------
        >>> # A factory which will be used in the example to generate data.
        >>> def factory():
        ...     import rachis
        ...     # This type is only available during testing.
        ...     # A real example would use a real type.
        ...     a = rachis.Artifact.import_data('IntSequence1', [1, 2, 3])
        ...     return a
        ...
        >>> my_artifact = use.init_artifact('my_artifact', factory)
        >>> my_artifact
        <ExecutionUsageVariable name='my_artifact', var_type='artifact'>
        """
        return self._usage_variable(name, factory, 'artifact')

    def init_artifact_collection(
        self, name: str,
            factory: Callable[[], rachis.ResultCollection]) -> UsageVariable:
        """Communicate that a result collection containing artifacts will be needed.

        Driver implementations may use this to intialize data for an example.

        Parameters
        ----------
        name : str
            The canonical name of the variable to be returned.
        factory : Callable which returns :class:`rachis.sdk.ResultCollection`
            A function which takes no parameters, and returns
            a result collection that contains artifacts.
            This function may do anything internally to create
            the result collection.

        Returns
        -------
        UsageVariable
            This particular return class can be changed by a driver which
            overrides :meth:`usage_variable`.

        Examples
        --------
        >>> # A factory which will be used in the example to generate data.
        >>> def factory():
        ...     import rachis
        ...     # This type is only available during testing.
        ...     # A real example would use a real type.
        ...     a = rachis.ResultCollection(
        ...         {'Foo': rachis.Artifact.import_data('IntSequence1', [1, 2, 3]),
        ...          'Bar': rachis.Artifact.import_data('IntSequence1', [4, 5, 6])})
        ...     return a
        ...
        >>> int_seq_collection = use.init_artifact_collection('int_seq_collection', factory)
        >>> int_seq_collection
        <ExecutionUsageVariable name='int_seq_collection', var_type='artifact_collection'>
        """  # noqa: E501
        return self._usage_variable(name, factory, 'artifact_collection')

    def init_metadata(self, name: str,
                      factory: Callable[[], rachis.Metadata]) -> UsageVariable:
        """Communicate that metadata will be needed.

        Driver implementations may use this to intialize data for an example.

        Parameters
        ----------
        name : str
            The canonical name of the variable to be returned.
        factory : Callable which returns :class:`rachis.Metadata`
            A function which takes no parameters, and returns metadata.
            This function may do anything internally to create the metadata.

        Returns
        -------
        UsageVariable
            Variable of type 'metadata'.

        Examples
        --------
        >>> # A factory which will be used in the example to generate data.
        >>> def factory():
        ...     import rachis
        ...     import pandas as pd
        ...     df = pd.DataFrame({'a':[1, 2, 3]}, index=['a', 'b', 'c'])
        ...     df.index.name = 'id'
        ...     md = rachis.Metadata(df)
        ...     return md
        ...
        >>> my_metadata = use.init_metadata('my_metadata', factory)
        >>> my_metadata
        <ExecutionUsageVariable name='my_metadata', var_type='metadata'>
        """
        return self._usage_variable(name, factory, 'metadata')

    def init_format(self, name: str,
                    factory: Callable[[], 'rachis.core.format.FormatBase'],
                    ext: str = None) -> UsageVariable:
        """Communicate that a file/directory format will be needed.

        Driver implementations may use this to intialize data for an example.

        Parameters
        ----------
        name : str
            The canonical name of the variable to be returned.
        factory : Callable which returns a file or directory format.
            A function which takes no parameters, and returns a format.
            This function may do anything internally to create the format.
        ext : str
            The extension to prefer if the format is preserved on disk.

        Returns
        -------
        UsageVariable
            Variable of type 'format'.

        Examples
        --------
        >>> # A factory which will be used in the example to generate data.
        >>> def factory():
        ...     from rachis.core.testing.format import IntSequenceFormat
        ...     from rachis.plugin.util import transform
        ...     ff = transform([1, 2, 3], to_type=IntSequenceFormat)
        ...
        ...     ff.validate()  # good practice
        ...     return ff
        ...
        >>> my_ints = use.init_format('my_ints', factory, ext='.hello')
        >>> my_ints
        <ExecutionUsageVariable name='my_ints', var_type='format'>
        """
        return self._usage_variable(name, factory, 'format')

    def _request_url(self, url):
        import urllib.request
        import urllib.error

        headers = {'User-Agent': self.USER_AGENT}
        try:
            request = urllib.request.Request(url, headers=headers)
            data = urllib.request.urlopen(request)
        except urllib.error.URLError as ex:
            raise ValueError(
                'Could not obtain URL: %s\n Exception: %s' %
                (url, str(ex)))

        return data

    def init_artifact_from_url(self, name: str, url: str,
                               ) -> UsageVariable:
        """Obtain an artifact from a url.

        Driver implementations may use this to intialize data for an example.

        Parameters
        ----------
        name : str
            The canonical name of the variable to be returned.
        url : str
            The url of the Artifact that should be downloaded for the
            example. If a Rachis epoch (e.g., 2022.11) is part of the URL, as
            might be the case if obtaining an Artifact from docs.qiime2.org,
            it can be templated in by including `{rachis.__release__}` in an
            F-string defining the URL.

        Returns
        -------
        UsageVariable
            This particular return class can be changed by a driver which
            overrides :meth:`usage_variable`.
        """
        # The following example needs to use an Artifact that the test suite's
        # plugin manager can handle.
        # Examples
        # --------
        # >>> import rachis
        # >>> url = (f'https://data.qiime2.org/{rachis.__release__}/data/'
        # ...        'tutorials/moving-pictures/table.qza')
        # >>> mvp_table = use.init_artifact_from_url('mvp_table', url)
        # >>> mvp_table
        # <ExecutionUsageVariable name='mvp_table', var_type='artifact'>
        def factory():
            import tempfile
            import rachis

            data = self._request_url(url)

            with tempfile.NamedTemporaryFile() as f:
                f.write(data.read())
                f.flush()
                try:
                    result = rachis.Artifact.load(f.name)
                except ValueError as ex:
                    raise ValueError(
                        'Could not load Artifact from URL data: %s\n'
                        ' Original exception: %s'
                        % (url, str(ex)))

            return result

        return self.init_artifact(name, factory)

    def init_metadata_from_url(self, name: str, url: str,
                               ) -> UsageVariable:
        """Obtain metadata from a url.

        Driver implementations may use this to intialize example metadata.

        Parameters
        ----------
        name : str
            The canonical name of the variable to be returned.
        url : str
            The url of the Artifact that should be downloaded for the
            example. If a Rachis epoch (e.g., 2022.11) is part of the URL, as
            might be the case if obtaining an Artifact from docs.qiime2.org,
            it can be templated in by including `{rachis.__release__}` in an
            F-string defining the URL.

        Returns
        -------
        UsageVariable
            This particular return class can be changed by a driver which
            overrides :meth:`usage_variable`.

        Examples
        --------
        >>> import rachis
        >>> url = ('https://data.qiime2.org/usage-examples/moving-pictures/'
        ...        'sample-metadata.tsv')
        >>> print(url)
        https://data.qiime2.org/usage...
        >>> md = use.init_metadata_from_url('md', url)
        >>> md
        <ExecutionUsageVariable name='md', var_type='metadata'>
        """
        # the print statement in the above doc string provides an illustration
        # of how F-strings are interpreted
        def factory():
            import tempfile

            data = self._request_url(url)

            with tempfile.NamedTemporaryFile() as f:
                f.write(data.read())
                f.flush()
                try:
                    md = rachis.Metadata.load(f.name)
                except rachis.metadata.io.MetadataFileError as ex:
                    raise ValueError(
                        'Could not load Metadata from URL data: %s\n'
                        ' Original exception: %s'
                        % (url, str(ex)))

                return md

        return self.init_metadata(name, factory)

    def import_from_format(self, name: str, semantic_type: str,
                           variable: UsageVariable,
                           view_type: 'rachis.core.format.FormatBase' = None
                           ) -> UsageVariable:
        """Communicate that an import should be done.

        Parameters
        ----------
        name : str
            The name of the resulting variable.
        semantic_type : str
            The semantic type to import as.
        variable : UsageVariable
            A variable of type 'format' which possesses a factory to
            materialize the actual data to be imported.
        view_type : format or str
            The view type to import as, in the event it is different from
            the default.

        Returns
        -------
        UsageVariable
            Variable of type 'artifact'.

        Examples
        --------
        >>> # A factory which will be used in the example to generate data.
        >>> def factory():
        ...     from rachis.core.testing.format import IntSequenceFormat
        ...     from rachis.plugin.util import transform
        ...     ff = transform([1, 2, 3], to_type=IntSequenceFormat)
        ...
        ...     ff.validate()  # good practice
        ...     return ff
        ...
        >>> to_import = use.init_format('to_import', factory, ext='.hello')
        >>> to_import
        <ExecutionUsageVariable name='to_import', var_type='format'>
        >>> ints = use.import_from_format('ints',
        ...                               semantic_type='IntSequence1',
        ...                               variable=to_import,
        ...                               view_type='IntSequenceFormat')
        >>> ints
        <ExecutionUsageVariable name='ints', var_type='artifact'>

        See Also
        --------
        init_format
        """
        assert_usage_var_type(variable, 'format')

        def factory():
            from rachis import Artifact

            fmt = variable.execute()
            artifact = Artifact.import_data(
                semantic_type, str(fmt), view_type=view_type)

            return artifact
        return self._usage_variable(name, factory, 'artifact')

    def construct_artifact_collection(
        self, name: str, members: Union[dict, list]
    ) -> UsageVariable:
        '''
        Return a UsageVariable of type artifact_collection given a list or dict
        of its members.

        Parameters
        ----------
        name : str
            The name of the resulting variable.
        members: list or dict
            The desired members of the ResultCollection.

        Returns
        -------
        UsageVariable
            Of type artifact_collection.

        Examples
        --------
        >>> mapping_1, = use.action(
        ...     use.UsageAction('dummy_plugin', 'params_only_method'),
        ...     use.UsageInputs(name='c', age=100),
        ...     use.UsageOutputNames(out='mapping_1')
        ... )
        >>> mapping_1
        <ExecutionUsageVariable name='mapping_1', var_type='artifact'>
        >>> collection_1 = use.construct_artifact_collection(
        ...     'collection_1', {'a': mapping_1, 'b': mapping_1}
        ... )
        >>> collection_1
        <ExecutionUsageVariable name='collection_1', var_type='artifact_collection'>
        '''  # noqa: E501

        # make sure members is dict to avoid repeated type checking
        if type(members) is list:
            members = {str(i): member for i, member in enumerate(members)}

        if not all(
            member.var_type == 'artifact' for member in members.values()
        ):
            raise ValueError('Expected only artifacts in the collection.')

        str_ns = {str(name) for name in self.namespace}
        diff = set(
            str(member.to_interface_name()) for member in members.values()
        ) - str_ns
        if diff:
            msg = (
                f'{diff} not found in driver\'s namespace. Make sure '
                'that all ResultCollection members have been properly '
                'created.'
            )
            raise ValueError(msg)

        def factory():
            from rachis import ResultCollection
            # NOTE: these usage variables are assumed to have been
            # materialized at this point
            members_dict = {
                key: member.execute() for key, member in members.items()
            }

            return ResultCollection(members_dict)

        return self._usage_variable(name, factory, 'artifact_collection')

    def get_artifact_collection_member(
            self, name: str, variable: UsageVariable, key: str
    ) -> UsageVariable:
        '''
        Accesses and returns a member of a ResultCollection as a UsageVariable.

        Parameters
        ----------
        name : str
            The name of the resulting variable.
        variable : UsageVariable
            The UsageVariable of type artifact_collection from which to access
            the desired member.
        key : str
            The key of the desired member in the ResultCollection.

        Returns
        -------
        UsageVariable
            Of type artifact.

        Examples
        --------
        >>> mapping_2, = use.action(
        ...     use.UsageAction('dummy_plugin', 'params_only_method'),
        ...     use.UsageInputs(name='c', age=100),
        ...     use.UsageOutputNames(out='mapping_2')
        ... )
        >>> mapping_2
        <ExecutionUsageVariable name='mapping_2', var_type='artifact'>
        >>> collection_2 = use.construct_artifact_collection(
        ...     'collection_2', {'a': mapping_2, 'b': mapping_2}
        ... )
        >>> collection_2
        <ExecutionUsageVariable name='collection_2', var_type='artifact_collection'>
        >>> first_member = use.get_artifact_collection_member(
        ...     'first_member', collection_2, 'a'
        ... )
        >>> first_member
        <ExecutionUsageVariable name='first_member', var_type='artifact'>
        '''  # noqa: E501
        def factory():
            return variable.execute()[key]

        return self._usage_variable(name, factory, 'artifact')

    def merge_metadata(self, name: str,
                       *variables: UsageVariable) -> UsageVariable:
        """Communicate that these metadata should be merged.

        Parameters
        ----------
        name : str
            The name of the resulting variable.
        *variables : UsageVariable
            Multiple variables of type 'metadata' to merge.

        Returns
        -------
        UsageVariable
            Variable of type 'metadata'.

        Raises
        ------
        AssertionError
            If a variable is not of type 'metadata'.

        Examples
        --------
        >>> def factory1():
        ...     import rachis
        ...     import pandas as pd
        ...     df = pd.DataFrame({'a':[0]}, index=['0'])
        ...     df.index.name = 'id'
        ...     md = rachis.Metadata(df)
        ...     return md
        ...
        >>> def factory2():
        ...     import rachis
        ...     import pandas as pd
        ...     df = pd.DataFrame({'b':[10]}, index=['0'])
        ...     df.index.name = 'id'
        ...     md = rachis.Metadata(df)
        ...     return md
        ...
        >>> some_artifact, = use.action(
        ...     use.UsageAction('dummy_plugin', 'params_only_method'),
        ...     use.UsageInputs(name='c', age=100),
        ...     use.UsageOutputNames(out='some_artifact'))
        ...
        >>> md1 = use.init_metadata('md1', factory1)
        >>> md2 = use.init_metadata('md2', factory2)
        >>> md3 = use.view_as_metadata('md3', some_artifact)
        >>> merged = use.merge_metadata('merged', md1, md2, md3)
        >>> merged
        <ExecutionUsageVariable name='merged', var_type='metadata'>

        See Also
        --------
        init_metadata
        view_as_metadata
        """
        if len(variables) < 2:
            raise ValueError('Must provide two or more Metadata inputs.')

        for variable in variables:
            assert_usage_var_type(variable, 'metadata')

        def factory():
            mds = [v.execute() for v in variables]
            return mds[0].merge(*mds[1:])
        return self._usage_variable(name, factory, 'metadata')

    def get_metadata_column(self, name: str, column_name: str,
                            variable: UsageVariable) -> UsageVariable:
        """Communicate that a column should be retrieved.

        Parameters
        ----------
        name : str
            The name of the resulting variable.
        column_name : str
            The column to retrieve.
        variable : UsageVariable
            The metadata to retrieve the column from. Must be a variable of
            type 'metadata'.

        Returns
        -------
        UsageVariable
            Variable of type 'column'.

        Raises
        ------
        AssertionError
            If the variable is not of type 'metadata'.

        Examples
        --------
        >>> def factory():
        ...     import rachis
        ...     import pandas as pd
        ...     df = pd.DataFrame({'column_a':[1, 2, 3]},
        ...                       index=['a', 'b', 'c'])
        ...     df.index.name = 'id'
        ...     return rachis.Metadata(df)
        ...
        >>> md_for_column = use.init_metadata('md_for_column', factory)
        >>> md_for_column
        <ExecutionUsageVariable name='md_for_column', var_type='metadata'>
        >>> my_column = use.get_metadata_column('my_column', 'column_a',
        ...                                     md_for_column)
        >>> my_column
        <ExecutionUsageVariable name='my_column', var_type='column'>

        See Also
        --------
        init_metadata
        """
        assert_usage_var_type(variable, 'metadata')

        def factory():
            return variable.execute().get_column(column_name)
        return self._usage_variable(name, factory, 'column')

    def view_as_metadata(self, name: str,
                         variable: UsageVariable) -> UsageVariable:
        """Communicate that an artifact should be views as metadata.

        Parameters
        ----------
        name : str
            The name of the resulting variable.
        variable : UsageVariable
            The artifact to convert to metadata. Must be a variable of
            type 'artifact'.

        Returns
        -------
        UsageVariable
            Variable of type 'metadata'.

        Raises
        ------
        AssertionError
            If the variable is not of type 'artifact'.

        Examples
        --------
        >>> artifact_for_md, = use.action(
        ...     use.UsageAction('dummy_plugin', 'params_only_method'),
        ...     use.UsageInputs(name='c', age=100),
        ...     use.UsageOutputNames(out='artifact_for_md'))
        >>> artifact_for_md
        <ExecutionUsageVariable name='artifact_for_md', var_type='artifact'>
        >>> metadata = use.view_as_metadata('metadata', artifact_for_md)
        >>> metadata
        <ExecutionUsageVariable name='metadata', var_type='metadata'>

        See Also
        --------
        init_artifact
        get_metadata_column
        """
        assert_usage_var_type(variable, 'artifact')

        def factory():
            from rachis import Metadata
            return variable.execute().view(Metadata)
        return self._usage_variable(name, factory, 'metadata')

    def peek(self, variable: UsageVariable):
        """Communicate that an artifact should be peeked at.

        Default implementation is to do nothing.

        Parameters
        ----------
        variable : UsageVariable
            A variable of 'artifact' type which should be peeked.

        Raises
        ------
        AssertionError
            If the variable is not of type 'artifact'.

        Examples
        --------
        >>> def factory():
        ...     import rachis
        ...     return rachis.Artifact.import_data('IntSequence1', [1, 2, 3])
        ...
        >>> a_boo = use.init_artifact('a_boo', factory)
        >>> use.peek(a_boo)
        """
        assert_usage_var_type(variable, 'artifact', 'visualization')

    def comment(self, text: str):
        """Communicate that a comment should be made.

        Default implementation is to do nothing.

        Parameters
        ----------
        text : str
            The inspired commentary.

        Examples
        --------
        >>> use.comment("The thing is, they always try to walk it in...")
        """
        pass

    def help(self, action: 'rachis.sdk.usage.UsageAction'):
        """Communicate that help text should be displayed.

        Default implementation is to do nothing.

        Parameters
        ----------
        action : UsageAction
            The particular action that should have help-text rendered.

        Examples
        --------
        >>> use.help(use.UsageAction('dummy_plugin', 'split_ints'))
        """
        pass

    def action(self,
               action: 'rachis.sdk.usage.UsageAction',
               inputs: 'rachis.sdk.usage.UsageInputs',
               outputs: 'rachis.sdk.usage.UsageOutputNames'
               ) -> 'rachis.sdk.usage.UsageOutputs':
        """Communicate that some action should be performed.

        Parameters
        ----------
        action : UsageAction
            The action to perform.
        inputs : UsageInputs
            The inputs to provide. These are a map of parameter names to
            arguments. Arguments may be primitive literals, or variables.
        outputs : UsageOutputNames
            Defines what to name each output variable. The keys much match the
            action's output signature.

        Returns
        -------
        UsageOutputs
            A wrapper around the usual :class:`rachis.sdk.Results` object.
            Unpacking this output can be seen in the examples below.

        Examples
        --------
        >>> results = use.action(
        ...     use.UsageAction(plugin_id='dummy_plugin',
        ...                     action_id='params_only_method'),
        ...     use.UsageInputs(name='foo', age=42),
        ...     use.UsageOutputNames(out='bar')
        ... )
        >>> results
        UsageOutputs (name = value)
        --------------------------------------------------------------
        out = <ExecutionUsageVariable name='bar', var_type='artifact'>

        >>> # "out" happens to be the name of this output, it isn't a general
        >>> # name for all results.
        >>> results.out
        <ExecutionUsageVariable name='bar', var_type='artifact'>

        >>> # unpack as an iterator
        >>> bar, = results
        >>> bar
        <ExecutionUsageVariable name='bar', var_type='artifact'>
        >>> bar is results.out
        True


        """
        if not isinstance(action, UsageAction):
            raise ValueError('Invalid value for `action`: expected %r, '
                             'received %r.' % (UsageAction, type(action)))

        if not isinstance(inputs, UsageInputs):
            raise ValueError('Invalid value for `inputs`: expected %r, '
                             'received %r.' % (UsageInputs, type(inputs)))

        if not isinstance(outputs, UsageOutputNames):
            raise ValueError('Invalid value for `outputs`: expected %r, '
                             'received %r.' % (UsageOutputNames,
                                               type(outputs)))

        action_f = action.get_action()

        @functools.lru_cache(maxsize=None)
        def memoized_action():
            execed_inputs = inputs.map_variables(lambda v: v.execute())
            if self.asynchronous:
                return action_f.asynchronous(**execed_inputs).result()
            return action_f(**execed_inputs)

        usage_results = []
        # outputs will be ordered by the `UsageOutputNames` order, not the
        # signature order - this makes it so that the example writer doesn't
        # need to be explicitly aware of the signature order
        for param_name, var_name in outputs.items():
            qiime_type = action_f.signature.outputs[param_name].qiime_type
            if is_collection_type(qiime_type):
                if str(qiime_type) == 'Collection[Visualization]':
                    var_type = 'visualization_collection'
                else:
                    var_type = 'artifact_collection'
            elif is_visualization_type(qiime_type):
                var_type = 'visualization'
            elif is_semantic_type(qiime_type):
                var_type = 'artifact'
            else:
                raise ValueError('unknown output type: %r' % (qiime_type,))

            def factory(name=param_name):
                results = memoized_action()
                result = getattr(results, name)
                return result

            variable = self._usage_variable(var_name, factory, var_type)
            usage_results.append(variable)

        results = UsageOutputs(outputs.keys(), usage_results)
        cache_info = memoized_action.cache_info
        cache_clear = memoized_action.cache_clear
        # manually graft on cache operations
        object.__setattr__(results, '_cache_info', cache_info)
        object.__setattr__(results, '_cache_reset', cache_clear)
        return results

    def action_not_found(self,
                         action: 'rachis.sdk.usage.UsageAction',
                         inputs: 'rachis.sdk.usage.UsageInputs',
                         outputs: 'rachis.sdk.usage.UsageOutputNames'
                         ) -> 'rachis.sdk.usage.UsageOutputs':
        if not isinstance(action, UsageAction):
            raise ValueError('Invalid value for `action`: expected %r, '
                             'received %r.' % (UsageAction, type(action)))

        if not isinstance(inputs, UsageInputs):
            raise ValueError('Invalid value for `inputs`: expected %r, '
                             'received %r.' % (UsageInputs, type(inputs)))

        if not isinstance(outputs, UsageOutputNames):
            raise ValueError('Invalid value for `outputs`: expected %r, '
                             'received %r.' % (UsageOutputNames,
                                               type(outputs)))

        usage_results = []
        for _, var_name in outputs.items():
            var_type = self._find_var_type_from_prov(
                action.node, action.node.action._action_dict)

            variable = self._usage_variable(var_name, lambda x: x, var_type)
            usage_results.append(variable)

        results = UsageOutputs(outputs.keys(), usage_results)
        return results

    def _find_var_type_from_prov(self,
                                 node,
                                 action_yaml):
        action_type = action_yaml['action']['type']

        if action_type == 'visualizer':
            # Visualizers can only output visualizations
            if type(action_yaml['action']['output-name']) is List:
                var_type = 'visualization_collection'
            else:
                var_type = 'visualization'
        elif action_type == 'method':
            # Methods can only output artifacts
            if type(action_yaml['action']['output-name']) is List:
                var_type = 'artifact_collection'
            else:
                var_type = 'artifact'
        elif action_type == 'pipeline':
            # If we are a pipeline we need to recurse until we find our root
            alias_uuid = action_yaml['action']['alias-of']
            alias_path = node._archiver.provenance_dir / 'artifacts' \
                / alias_uuid / 'action' / 'action.yaml'

            from rachis.core.archive.provenance import load_action_stream
            with alias_path.open() as fh:
                alias_yaml = load_action_stream(fh)

            var_type = self._find_var_type_from_prov(node, alias_yaml)
        elif action_type == 'import':
            # Imports are always an Artifact
            var_type = 'artifact'
        elif action_type == 'report':
            # Reports are always a Visualization
            var_type = 'visualization'
        else:
            # What have you done
            raise ValueError(f'Unknown action type found: {action_type}')

        return var_type


class DiagnosticUsage(Usage):
    @dataclasses.dataclass(frozen=True)
    class DiagnosticUsageRecord:
        """A dataclass storing the invoked method name and variable/param."""
        source: str
        variable: Any

    def __init__(self):
        """Constructor for DiagnosticUsage. No parameters.

        Warning
        -------
        For SDK use only. Do not use in a written usage example.
        """
        super().__init__()
        self._recorder = []

    def render(self, flush: bool = False) -> List[DiagnosticUsageRecord]:
        """Produce a list of :class:`DiagnosticUsage.DiagnosticUsageRecord`'s
        for testing.

        Warning
        -------
          For SDK use only. Do not use in a written usage example.

        Parameters
        ----------
        flush : bool
            Whether to reset the current state of the records.

        """
        records = self._recorder
        if flush:
            self._recorder = []
        return records

    def _append_record(self, source, variable):
        self._recorder.append(self.DiagnosticUsageRecord(source, variable))

    def init_artifact(self, name, factory):
        variable = super().init_artifact(name, factory)
        self._append_record('init_artifact', variable)
        return variable

    def init_artifact_collection(self, name, factory):
        variable = super().init_artifact_collection(name, factory)
        self._append_record('init_artifact_collection', variable)
        return variable

    def init_metadata(self, name, factory):
        variable = super().init_metadata(name, factory)
        self._append_record('init_metadata', variable)
        return variable

    def init_format(self, name, factory, ext=None):
        variable = super().init_format(name, factory, ext=ext)
        self._append_record('init_format', variable)
        return variable

    def import_from_format(self, name, semantic_type,
                           variable, view_type=None):
        variable = super().import_from_format(
            name, semantic_type, variable, view_type=view_type)
        self._append_record('import_from_format', variable)
        return variable

    def merge_metadata(self, name, *variables):
        variable = super().merge_metadata(name, *variables)
        self._append_record('merge_metadata', variable)
        return variable

    def get_metadata_column(self, name, column_name, variable):
        variable = super().get_metadata_column(name, column_name, variable)
        self._append_record('get_metadata_column', variable)
        return variable

    def view_as_metadata(self, name, artifact_variable):
        variable = super().view_as_metadata(name, artifact_variable)
        self._append_record('view_as_metadata', variable)
        return variable

    def peek(self, variable):
        self._append_record('peek', variable)

    def comment(self, text):
        self._append_record('comment', text)

    def help(self, action):
        self._append_record('help', action)

    def action(self, action, input_opts, output_opts):
        variables = super().action(action, input_opts, output_opts)
        self._append_record('action', variables)
        return variables


class ExecutionUsageVariable(UsageVariable):
    """A specialized implementation for :class:`ExecutionUsage`."""

    # Utility method for key handling within result collections
    def _collection_key_util(self, data, key):
        if self.var_type not in self.COLLECTION_VAR_TYPES:
            raise TypeError("Key can only be provided for output of type"
                            " artifact_collection or visualization_collection."
                            " Output of type %s was provided."
                            % (self.var_type))
        if key not in data.keys():
            raise ValueError("Provided key %s not found in output" % (key))

        data = data[key]
        return data

    def assert_has_line_matching(self, path, expression, key=None):
        assert_usage_var_type(self, 'artifact', 'visualization',
                              'artifact_collection',
                              'visualization_collection')

        data = self.value

        if key is not None:
            key = str(key)

        if key:
            data = self._collection_key_util(data=data, key=key)

        hits = sorted(data._archiver.data_dir.glob(path))
        if len(hits) != 1:
            raise ValueError('Value provided for path (%s) did not produce '
                             'exactly one hit: %s' % (path, hits))

        target = hits[0].read_text()
        match = re.search(expression, target, flags=re.MULTILINE)
        if match is None:
            raise AssertionError('Expression %r not found in %s.' %
                                 (expression, path))

    def assert_output_type(self, semantic_type, key=None):
        data = self.value
        name = self.name

        if key is not None:
            key = str(key)

        if key:
            data = self._collection_key_util(data=data, key=key)
            name = "%s[%s]" % (self.name, key)

        if str(data.type) != str(semantic_type):
            raise AssertionError("Output %r has type %s, which does not match"
                                 " expected output type of %s"
                                 % (name, data.type, semantic_type))


class ExecutionUsage(Usage):
    def __init__(self, asynchronous=False):
        """Constructor for ExecutionUsage.

        Warning
        -------
        For SDK use only. Do not use in a written usage example.

        Parameters
        ----------
        asynchronous : bool
            Whether to execute actions via
            :meth:`rachis.sdk.Action.asynchronous` or
            :meth:`rachis.sdk.Action.__call__`
        """
        super().__init__(asynchronous)
        # This is here for testing-purposes
        self._recorder = dict()

    def render(self, flush: bool = False) -> dict:
        """Produce a dict of canonically named, evaluated usage variables.

        Warning
        -------
        For SDK use only. Do not use in a written usage example.

        Parameters
        ----------
        flush : bool
            Whether to reset the current state of the dict.

        Returns
        -------
        dict
            Evaluated variables named by their variable's canonical name.

        See Also
        --------
        UsageVariable.execute
        UsageVariable.name

        """
        records = self._recorder
        if flush:
            self._recorder = dict()
        return records

    def usage_variable(self, name, factory, var_type):
        return ExecutionUsageVariable(name, factory, var_type, self)

    def init_artifact(self, name, factory):
        variable = super().init_artifact(name, factory)

        variable.execute()
        self._recorder[variable.name] = variable

        return variable

    def init_artifact_collection(self, name, factory):
        variable = super().init_artifact_collection(name, factory)

        variable.execute()
        self._recorder[variable.name] = variable

        return variable

    def init_metadata(self, name, factory):
        variable = super().init_metadata(name, factory)

        variable.execute()
        self._recorder[variable.name] = variable

        return variable

    def init_format(self, name, factory, ext=None):
        variable = super().init_format(name, factory, ext=ext)

        variable.execute()
        self._recorder[variable.name] = variable

        return variable

    def import_from_format(self, name, semantic_type,
                           variable, view_type=None):
        variable = super().import_from_format(
            name, semantic_type, variable, view_type=view_type)

        variable.execute()
        self._recorder[variable.name] = variable

        return variable

    def merge_metadata(self, name, *variables):
        variable = super().merge_metadata(name, *variables)

        variable.execute()
        self._recorder[variable.name] = variable

        return variable

    def get_metadata_column(self, name, column_name, variable):
        variable = super().get_metadata_column(name, column_name, variable)

        variable.execute()
        self._recorder[variable.name] = variable

        return variable

    def view_as_metadata(self, name, artifact_variable):
        variable = super().view_as_metadata(name, artifact_variable)

        variable.execute()
        self._recorder[variable.name] = variable

        return variable

    def action(self, action, input_opts, output_opts):
        variables = super().action(action, input_opts, output_opts)

        for variable in variables:
            variable.execute()
            self._recorder[variable.name] = variable

        return variables
