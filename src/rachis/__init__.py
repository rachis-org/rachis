# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from rachis.sdk import Artifact, Visualization, ResultCollection
from rachis.metadata import (Metadata, MetadataColumn,
                             CategoricalMetadataColumn, NumericMetadataColumn)
from rachis.plugin import Citations
from rachis.core.cache import Cache, CacheV1, Pool, get_cache
from rachis.core.cache_v2 import CacheV2, Scope, ScopedDirectory
from rachis.core.annotate import Note
import rachis.util as util

try:
    from ._version import __version__
except ModuleNotFoundError:
    __version__ = '0.0.0+notfound'

# "Train release" version includes <year>.<month> and excludes patch numbers
# and pre/post-release tags. All versions within a train release are expected
# to be compatible.
__release__ = '.'.join(__version__.split('.')[:2])
__citations__ = tuple(Citations.load('citations.bib', package='rachis'))
__website__ = 'https://qiime2.org'

__all__ = ['Artifact', 'Visualization', 'ResultCollection', 'Metadata',
           'MetadataColumn', 'CategoricalMetadataColumn',
           'NumericMetadataColumn', 'Cache', 'CacheV1', 'CacheV2', 'Scope',
           'ScopedDirectory', 'Pool', 'get_cache', 'Note',
           'util']
