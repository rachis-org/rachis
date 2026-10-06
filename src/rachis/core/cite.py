# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import os
from typing import Union, Optional, IO
import importlib.resources
import collections

import bibtexparser as bp

CitationRecord = collections.namedtuple('CitationRecord', ['type', 'fields'])
"""
A :py:func:`collections.namedtuple` of bibtex entry type and entry fields.

Parameters
----------
type : str
  The bibtex entry type (e.g. 'article')
fields: dict
  The individual key-value pairs of the bibtex entry
"""


def _make_citations_tuple(citations):
    if citations is None:
        return tuple()
    elif isinstance(citations, CitationRecord):
        citation = citations
        return (citation,)
    else:
        return tuple(citations)


class Citations(collections.OrderedDict):
    """A simple subclass of :py:class:`collections.OrderedDict`
       but iterates over values instead of keys by default."""
    @classmethod
    def load(cls, path: Union[str, os.PathLike],
             package: Optional[str] = None):
        """Load a bibtex file from a path (or relative package path)

        Parameters
        ----------
        path
          The path of a bibtex file, if `package` is provided, it will be
          relative to the python package.
        package
          The python package to load from.

        Returns
        -------
        Citations
        """
        parser = bp.bparser.BibTexParser()
        # Downstream tooling is much easier with unicode. For actual latex
        # users, use the modern biber backend instead of bibtex
        parser.customization = bp.customization.convert_to_unicode

        if package is not None:
            file = importlib.resources.open_text(package, path)
        else:
            file = path.open() if hasattr(path, 'open') else open(path)

        with file as fh:
            try:
                db = bp.load(fh, parser=parser)
            except Exception as e:
                raise ValueError("There was a problem loading the BiBTex file:"
                                 "%r" % path) from e

        entries = collections.OrderedDict()
        for entry in db.entries:
            id_ = entry.pop('ID')
            type_ = entry.pop('ENTRYTYPE')
            if id_ in entries:
                raise ValueError("Duplicate entry-key found in BibTex file: %r"
                                 % id_)
            entries[id_] = CitationRecord(type_, entry)

        return cls(entries)

    def __iter__(self):
        """Iterates over the contained :py:class:`CitationRecord`'s"""
        return iter(self.values())

    def save(self, f: Union[str, IO]):
        """Save object as bibtex to a filepath or filehandle

        Parameters
        ----------
        f
          A string (but not :py:class:`os.PathLike`) or filehandle

        Returns
        -------
        None
        """
        entries = []
        for key, citation in self.items():
            entry = citation.fields.copy()
            entry['ID'] = key
            entry['ENTRYTYPE'] = citation.type

            entries.append(entry)

        db = bp.bibdatabase.BibDatabase()
        db.entries = entries

        writer = bp.bwriter.BibTexWriter()
        writer.order_entries_by = tuple(self.keys())

        owned = False
        if type(f) is str:
            f = open(f, 'w')
            owned = True
        try:
            bp.dump(db, f, writer=writer)
        finally:
            if owned:
                f.close()
