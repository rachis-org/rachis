# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from .model import (TextFileFormat, BinaryFileFormat, DirectoryFormat,
                    ValidationError, SingleFileDirectoryFormat)
from .plugin import Plugin
from .util import get_available_cores, NP_RNG_BITS, get_np_random_seed
from rachis.core.cite import Citations, CitationRecord
from rachis.core.axis import Coord, ForEach
from rachis.core.type import (SemanticType, Int, Str, Float, Metadata,
                              MetadataColumn, Categorical, Numeric, Properties,
                              Range, Start, End, Choices, Bool, Set, List,
                              Collection, Visualization, TypeMap, TypeMatch,
                              Jobs, Threads, CaptureHolder)
from .context import IContext


__all__ = ['TextFileFormat', 'BinaryFileFormat', 'DirectoryFormat', 'Plugin',
           'SemanticType', 'Set', 'List', 'Collection', 'Bool', 'Int', 'Str',
           'Float', 'Metadata', 'MetadataColumn', 'Categorical', 'Numeric',
           'Properties', 'Range', 'Start', 'End', 'Choices', 'Visualization',
           'Jobs', 'Threads', 'TypeMap', 'TypeMatch', 'ValidationError',
           'Citations', 'CitationRecord', 'get_available_cores',
           'NP_RNG_BITS', 'get_np_random_seed', 'SingleFileDirectoryFormat',
           'CaptureHolder', 'IContext', 'Coord', 'ForEach']

# IMPORTANT:
# Autodoc cannot find a docstring unless it is defined in the same module that
# is referenced. So if you say something like
# .. autodata:: rachis.plugin.Set
# then the docstring has to be present at rachis.plugin (aka here)
# Otherwise, you would have to say `rachis.core.type.collections.Set` which is
# NOT what we want other developers to be importing.

Set = Set
"""**Deprecated** - use List or Collection instead

A set of unique elements without a defined order except when used with
a semantic type, in which case, the views are provided to the plugin as a list.
"""

List = List
"""An alias for a :py:data:`.Collection` with numeric auto-incrementing keys.

Examples
--------
>>> from rachis.plugin import List, Str

A regular list:

>>> ['a', 'b', 'c'] in List[Str]
True

A collection is also compatible:

>>> {'arbitrary_key': 'a'} in List[Str]
True
"""

Collection = Collection
"""An ordered set of key-value pairs. Compatible with lists and dictionaries.

The keys of a collection are always strings, and the values are defined by the
variant provided to the type-field.

Whenever a list is provided, it will be coerced into a dictionary with
auto-incrementing integer keys.

Examples
--------
>>> from rachis.plugin import Collection, Str

A regular dictionary:

>>> {'key1': 'a', 'key2': 'b'} in Collection[Str]
True

A list:

>>> ['a', 'b'] in Collection[Str]
True
"""


Visualization = Visualization
"""The type of a Rachis Visualization.

This is not a semantic type as it represents a terminal/non-composable output.

An output with this type provides no assurances about the structure of the data
as it is meant for human interpretation.

Examples
--------
>>> from rachis.plugin import Visualization
>>> Visualization
Visualization
"""

Int = Int
"""
An integer without any particular bounds.
It can use the predicates :py:class:`.Range`, :py:func:`.Start`, and
:py:func:`.End`

Examples
--------
>>> from rachis.plugin import Int, Range, Start, End

No bounds on the value:

>>> -2 in Int
True

Integers between 0 (inclusive) and 5 (exclusive):

>>> 0 in Int % Range(0, 5)
True
>>> 5 in Int % Range(0, 5)
False

Same as above:

>>> 0 in Int % (Start(0) & End(5))
True
>>> 5 in Int % (Start(0) & End(5))
False

"""

Float = Float
"""
A 64 bit floating point number.
It can use the predicates :py:class:`.Range`, :py:func:`.Start`, and
:py:func:`.End`

Examples
--------
>>> from rachis.plugin import Float, Range, Start, End

No bounds on the value:

>>> -0.2 in Float
True

Proportion between 0 (exclusive) and 1 (inclusive):

>>> 0.0 in Float % Range(0, 1, inclusive_start=False, inclusive_end=True)
False
>>> 1.0 in Float % Range(0, 1, inclusive_start=False, inclusive_end=True)
True

Same as above:

>>> 0.0 in Float % (Start(0, inclusive=False) & End(1, inclusive=True))
False
>>> 1.0 in Float % (Start(0, inclusive=False) & End(1, inclusive=True))
True
"""

Bool = Bool
"""
A boolean value (``True``/``False``).
It can use the predicate :py:class:`.Choices` (but this is only interesting
when using :py:class:`.TypeMap`)

Examples
--------
>>> from rachis.plugin import Bool, Choices

Normal values:

>>> True in Bool
True
>>> False in Bool
True

Constrained (for :py:class:`.TypeMap`):

>>> False in Bool % Choices(False)
True
>>> True in Bool % Choices(False)
False
"""

Str = Str
"""
A string of Unicode characters (i.e. text).
It can use the predicate :py:class:`.Choices` to create strict enumeration.

Examples
--------
>>> from rachis.plugin import Str, Choices

Arbitrary string:

>>> "Hello World" in Str
True

Enumeration of options:

>>> "apple" in Str % Choices("apple", "orange", "banana")
True
>>> "airplane" in Str % Choices("apple", "orange", "banana")
False

Warning
-------
Do not use :py:data:`.Str` for filepaths. Not all interfaces have a consistent
(or user-navigable) representation of a filesystem. Data should be represented
as an artifact which will have a :py:func:`.SemanticType` and allows the
interface to store (and manipulate) your data as it sees fit.
"""

Metadata = Metadata
"""Tabular metadata where unique identifiers can be associated with columns.

This is the type that represents :py:class:`rachis.Metadata`.

Examples
--------
>>> import pandas as pd
>>> import rachis
>>> from rachis.plugin import Metadata

Note the distinct module paths:

>>> md = rachis.Metadata(pd.DataFrame([{'num':1, 'cat': 'a'}],
...                                   index=pd.Series(['s1'], name='id')))
>>> md in Metadata
True
"""

MetadataColumn = MetadataColumn
"""A column of a :py:class:`rachis.Metadata`.

Has two variants: :py:data:`.Categorical` and :py:data:`.Numeric`.

Examples
--------
>>> import pandas as pd
>>> import rachis
>>> from rachis.plugin import Metadata, Categorical, Numeric
>>> md = rachis.Metadata(pd.DataFrame([{'num':1, 'cat': 'a'}],
...                                   index=pd.Series(['s1'], name='id')))
>>> md
Metadata
--------
1 ID x 2 columns
num: ColumnProperties(type='numeric', missing_scheme='blank')
cat: ColumnProperties(type='categorical', missing_scheme='blank')
...

Categorical column:

>>> md.get_column('cat') in MetadataColumn[Categorical]
True
>>> md.get_column('num') in MetadataColumn[Categorical]
False

Numeric column:

>>> md.get_column('num') in MetadataColumn[Numeric]
True
>>> md.get_column('cat') in MetadataColumn[Numeric]
False

Any column:

>>> md.get_column('cat') in MetadataColumn[Categorical | Numeric]
True
>>> md.get_column('num') in MetadataColumn[Categorical | Numeric]
True
"""

Categorical = Categorical
"""The categorical variant for :py:data:`.MetadataColumn`.

Has no meaning unless used within :py:data:`.MetadataColumn`.
"""

Numeric = Numeric
"""The Numeric variant for :py:data:`.MetadataColumn`.

Has no meaning unless used within :py:data:`.MetadataColumn`.
"""

Jobs = Jobs
"""
The number of jobs to submit as an integer that is greater than zero
(exclusive).

It does not support any predicate expressions.

Examples
--------
>>> from rachis.plugin import Jobs

Positive integer for the number of jobs to use:

>>> 20 in Jobs
True

Zero is not a valid value:

>>> 0 in Jobs
False
"""

Threads = Threads
"""
The number of logical threads to use (OS threads/CPUs/Cores).

Valid inputs are an integer that is non-negative or the string ``"auto"``.
``0`` and ``"auto"`` will indicate that the number of logical threads should be
dictated by system resources.

It does not support any predicate expressions.

Examples
--------
>>> from rachis.plugin import Threads

Positive integer for the number of logical threads to use:

>>> 12 in Threads
True

Zero/auto to let the system decide:

>>> 0 in Threads
True
>>> "auto" in Threads
True
"""
