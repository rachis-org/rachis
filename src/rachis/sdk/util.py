# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import builtins
import re
from copy import copy
from importlib.metadata import entry_points
from typing import Dict, TYPE_CHECKING

from rachis.core.format import report
import rachis.sdk
import rachis.core.type as qtype
import rachis.core.type.parse as _parse
from rachis.core.type import (
    is_semantic_type, is_primitive_type, is_collection_type, is_metadata_type,
    is_visualization_type, interrogate_collection_type, parse_primitive,
    is_union, is_metadata_column_type, is_parallel_type)

if TYPE_CHECKING:
    from rachis.sdk.usage import UsageDriver

__all__ = [
    'is_semantic_type', 'is_primitive_type', 'is_collection_type',
    'is_metadata_type', 'is_visualization_type', 'interrogate_collection_type',
    'type_from_ast', 'parse_primitive', 'parse_type', 'parse_format',
    'actions_by_input_type', 'is_union', 'is_metadata_column_type',
    'is_parallel_type'
]


class Sievable:
    """Opt a value into recursive selection.

    The default _sieve walks items() and rebuilds via an independent shallow
    copy and update(). Other structures can supply their own _sieve.
    """
    @classmethod
    def sieve(cls, value, *, isinstance):
        """Select instances of given types and return a reusable rebuild."""
        selected = []

        def visit(value):
            if builtins.isinstance(value, isinstance):
                index = len(selected)
                selected.append(value)
                return lambda replacements: replacements[index]

            if builtins.isinstance(value, Sievable):
                return value._sieve(visit)
            if builtins.isinstance(value, dict):
                mapping = value if type(value) is dict else dict(value)
                return cls._sieve_mapping(mapping, visit)
            if builtins.isinstance(value, (list, set)):
                traversal = tuple(map(visit, value))
                return lambda replacements: type(value)(
                    rebuild_item(replacements) for rebuild_item in traversal
                )
            return lambda _: value

        rebuild_root = visit(value)

        def rebuild(replacements):
            replacements = tuple(replacements)
            if len(replacements) != len(selected):
                raise ValueError('Expected one replacement per selected value')
            return rebuild_root(replacements)

        return tuple(selected), rebuild

    def __copy__(self):
        """Return a shallow copy of the same type with independent entries."""
        raise NotImplementedError

    def update(self, items):
        """Update this object from an iterable of key-value pairs."""
        raise NotImplementedError

    def _sieve(self, visit):
        return self._sieve_mapping(self, visit)

    @staticmethod
    def _sieve_mapping(mapping, visit):
        snapshot = copy(mapping)
        traversal = tuple(
            (key, visit(value)) for key, value in snapshot.items()
        )

        def rebuild(replacements):
            result = copy(snapshot)
            result.update(
                (key, rebuild_item(replacements))
                for key, rebuild_item in traversal
            )
            return result

        return rebuild


def camel_to_snake(name: str) -> str:
    """
    There are more comprehensive and faster ways of doing this (incl compiling)
    but it handles acronyms in semantic types nicely
    e.g. EMPSingleEndSequences -> emp_single_end_sequences
    c/o https://stackoverflow.com/a/1176023/9872253
    """
    # this will frequently be called on QIIME type expressions, so drop [ and ]
    name = re.sub(r'[\[\]]', '', name)
    # camel to snake
    name = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
    return re.sub('([a-z0-9])([A-Z])', r'\1_\2', name).lower()


def type_from_ast(ast, scope=None):
    """Convert a type ast (from `.to_ast()`) to a type expression.

    Parameters
    ----------
    ast : json compatible object
        The abstract syntax tree produced by `to_ast` on a type.
    scope : dict
        A dictionary to use between multiple calls to share scope between
        different types. This allows type variables from the same type map
        to be constructed from an equivalent type map. Scope should be shared
        within a given call signature, but not between call signatures.

    Returns
    -------
    type expression

    """
    return _parse.ast_to_type(ast, scope=scope)


def parse_type(string, expect=None):
    """Convert a string into a type expression

    Parameters
    ----------
    string : str
        The string type expression to convert into a TypeExpression
    expect : {'semantic', 'primitive', 'visualization'}, optional
        Will raise a TypeError if the resulting TypeExpression is not a member
        of `expect`.

    Returns
    -------
    type expression

    """
    if expect is not None and expect not in {'semantic', 'primitive',
                                             'visualization'}:
        raise ValueError("`expect` got %r, must be 'semantic', 'primitive',"
                         " 'visualization', or None." % (expect,))

    type_expr = _parse.ast_to_type(_parse.string_to_ast(string))

    if expect is None:
        pass
    elif expect == 'semantic' and qtype.is_semantic_type(type_expr):
        pass
    elif expect == 'primitive' and qtype.is_primitive_type(type_expr):
        pass
    elif expect == 'visualization' and type_expr == qtype.Visualization:
        pass
    else:
        raise TypeError("Type expression %r is not a %s type."
                        % (type_expr, expect))
    return type_expr


def parse_format(format_str):
    if format_str is None:
        return None
    if format_str == 'report':
        return report

    pm = rachis.sdk.PluginManager()
    try:
        format_record = pm.formats[format_str]
    except KeyError:
        raise TypeError("No format: %s" % format_str)
    return format_record.format


def actions_by_input_type(string):
    """Plugins and actions that have as input the artifact type (string)

    Parameters
    ----------
    string : str
        Rachis artifact type

    Returns
    -------
    list of tuples: [(plugin, [actions, ...]), ...]
    """
    commands = []
    if string is not None:
        query_type = rachis.sdk.util.parse_type(string)

        pm = rachis.sdk.PluginManager()
        for pgn, pg in pm.plugins.items():
            actions = list({a for an, a in pg.actions.items()
                            for iname, i in a.signature.inputs.items()
                            if i.qiime_type >= query_type})
            if actions:
                commands.append((pg, actions))

    return commands


def validate_result_collection_keys(*args):
    """Validate one or more strings intended for use as ResultCollection keys.

    This can be called on one or more keys provided as arguments:
    rachis.sdk.util.validate_result_collection_keys('@', 'a1@')

    Or on a list, by unpacking it in the call:
    l = ['@', 'a1@']
    rachis.sdk.util.validate_result_collection_keys(*l)
    """
    invalid_keys = []
    for key in args:
        if not isinstance(key, str) or bool(re.search(r'[^\w+-.~]', key)):
            invalid_keys.append(key)

    if len(invalid_keys) > 0:
        raise KeyError('Invalid key(s) provided for ResultCollection. '
                       'ResultCollection keys must be strings and may only '
                       'contain the following characters: A-Z, a-z, 0-9, +, '
                       '-, ., _, and ~. Offending keys include: '
                       f'{", ".join(map(str, invalid_keys))}')


def view_collection(collection, view_type):
    return {k: v.view(view_type) for k, v in collection.items()}


def get_available_usage_drivers() -> Dict[str, 'UsageDriver']:
    '''
    Discovers all usage drivers registered under the entry point group
    'rachis.usage_drivers and rachis.usage_drivers'.

    Returns
    -------
    dict of entry point name -> UsageDriver
        Where keys are the entry point names as registered in setup.py and the
        values are types of the available usage drivers (not instances
        themselves).
    '''
    return {
        **{
            entry_point.name: entry_point.load() for entry_point in
            entry_points(group='qiime2.usage_drivers')
        },
        **{
            entry_point.name: entry_point.load() for entry_point in
            entry_points(group='rachis.usage_drivers')
        },
    }
