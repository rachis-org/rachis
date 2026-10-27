# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------
import re
import warnings

from typing import Tuple

from rachis.core.archive import Archiver

_VERSION_MATCHER = (
    r'QIIME 2\n'
    # allows for 0-6 as ints and 7.0+ as floats
    r'archive: (?:[0-6]|[7-9]\.[0-9]+|[1-9][0-9]+\.[0-9]+)$\n'
    r'framework: '
    r'(?:20[0-9]{2}|2)\.(?:[1-9][0-2]?|0)\.[0-9](?:\.dev[0-9]?)?'
    r'(?:\+[.\w]+)?\Z'
)


def parse_version(
    archiver: Archiver, nested_artifact: str | None = None
) -> Tuple[str, str]:
    '''
    Finds and parses the VERSION file inside of an archive.

    Parameters
    ----------
    archiver : Archiver
        The Archiver we are getting the version of
    nested_artifact : str | None
        the uuid of the nested result

    Returns
    -------
    tuple of (str, str)
        The archive version and framework version of the archive.
    '''
    uuid = archiver.uuid

    if nested_artifact is not None:
        version_fp = archiver.provenance_dir / 'artifacts' \
            / nested_artifact / 'VERSION'
        archiver = nested_artifact
    else:
        version_fp = archiver.path / 'VERSION'

    try:
        with version_fp.open() as v_fp:
            version_contents = v_fp.read().strip()
    except KeyError:
        raise ValueError(
            f'Malformed Archive: VERSION file for node {uuid} misplaced '
            f'or nonexistent.\nArchive {archiver.path} may be corrupt '
            'or provenance may be false.'
        )

    if not re.match(_VERSION_MATCHER, version_contents, re.MULTILINE):
        warnings.filterwarnings(
            'ignore', 'invalid escape sequence', DeprecationWarning
        )
        version_match_repr = _VERSION_MATCHER.replace(r'\n', '\n')
        raise ValueError(
            f'Malformed Archive: VERSION file out of spec in '
            f'{archiver.path}.\n'
            f'Should match this regular expression:\n{version_match_repr}\n'
            f'Actually looks like:\n{version_contents}\n'
        )

    _, archive_version, framework_version = [
        line.strip().split()[-1] for line in
        version_contents.split(sep='\n') if line
    ]
    return (archive_version, framework_version)
