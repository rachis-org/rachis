# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import os
import re
import shutil
import warnings
import tempfile
import collections
import distutils.dir_util
import pathlib
import json
from typing import Union, get_args, get_origin

from rachis.core.format import report
import rachis.plugin
import rachis.sdk
import rachis.core.type
import rachis.core.transform as transform
import rachis.core.archive as archive
import rachis.plugin.model as model
import rachis.core.util as util
import rachis.core.exceptions as exceptions

from rachis.sdk.iresult import IResult

# Note: Result, Artifact, and Visualization classes are in this file to avoid
# circular dependencies between Result and its subclasses. Result is tightly
# coupled to Artifact and Visualization because it is a base class and a
# factory, so having the classes in the same file helps make this coupling
# explicit.


ResultMetadata = collections.namedtuple('ResultMetadata',
                                        ['uuid', 'type', 'format'])


class Result(IResult):
    """Base class for QIIME 2 result classes (Artifact and Visualization).

    This class is not intended to be instantiated. Instead, it acts as a public
    factory and namespace for interacting with Artifacts and Visualizations in
    a generic way. It also acts as a base class for code reuse and provides an
    API shared by Artifact and Visualization.

    """

    # Subclasses must override to provide a file extension.
    extension = None

    @classmethod
    def _is_valid_type(cls, type_):
        """Subclasses should override this method."""
        return True

    @classmethod
    def peek(cls, filepath):
        return ResultMetadata(*archive.Archiver.peek(filepath))

    @classmethod
    def extract(cls, filepath, output_dir):
        """Unzip contents of Artifacts and Visualizations."""
        return archive.Archiver.extract(filepath, output_dir)

    @classmethod
    def load(cls, filepath):
        """Factory for loading Artifacts and Visualizations."""
        from rachis.core.cache import get_cache

        # Check if the data is already in the cache (if the uuid is in
        # cache.data) and load it from the cache if it is. Avoids unzipping the
        # qza again if we already have it.
        cache = get_cache()
        peek = cls.peek(filepath)
        archiver = cache._load_uuid(peek.uuid)

        if not archiver:
            try:
                archiver = archive.Archiver.load(filepath)
            except OSError as e:
                if e.errno == 28:
                    temp = tempfile.tempdir
                    raise ValueError(f'There was not enough space left on '
                                     f'{temp!r} to extract the artifact '
                                     f'{filepath!r}. (Try setting $TMPDIR to '
                                     'a directory with more space, or '
                                     f'increasing the size of {temp!r})')
                else:
                    raise e

        if Artifact._is_valid_type(archiver.type):
            result = Artifact.__new__(Artifact)
        elif Visualization._is_valid_type(archiver.type):
            result = Visualization.__new__(Visualization)
        else:
            raise TypeError(
                "Cannot load filepath %r into an Artifact or Visualization "
                "because type %r is not supported."
                % (filepath, archiver.type))

        if type(result) is not cls and cls is not Result:
            raise TypeError(
                "Attempting to load %s with `%s.load`. Use `%s.load` instead."
                % (type(result).__name__, cls.__name__,
                   type(result).__name__))

        result._archiver = archiver

        return result

    @classmethod
    def _from_archiver(cls, archiver):
        if Artifact._is_valid_type(archiver.type):
            result = Artifact.__new__(Artifact)
        elif Visualization._is_valid_type(archiver.type):
            result = Visualization.__new__(Visualization)
        else:
            raise TypeError(
                "Cannot load filepath %r into an Artifact or Visualization "
                "because type %r is not supported."
                % (archiver.path, archiver.type))

        if type(result) is not cls and cls is not Result:
            raise TypeError(
                "Attempting to load %s with `%s.load`. Use `%s.load` instead."
                % (type(result).__name__, cls.__name__,
                   type(result).__name__))

        result._archiver = archiver
        return result

    @property
    def type(self):
        return self._archiver.type

    @property
    def uuid(self):
        return self._archiver.uuid

    @property
    def format(self):
        return self._archiver.format

    @property
    def archive_version(self):
        return self._archiver.archive_version

    @property
    def citations(self):
        return self._archiver.citations

    def __init__(self):
        raise NotImplementedError(
            "%(classname)s constructor is private, use `%(classname)s.load`, "
            "`%(classname)s.peek`, or `%(classname)s.extract`."
            % {'classname': self.__class__.__name__})

    def __new__(cls):
        result = object.__new__(cls)
        result._archiver = None
        return result

    def __repr__(self):
        return ("<%s: %r uuid: %s>"
                % (self.__class__.__name__.lower(), self.type, self.uuid))

    def __hash__(self):
        return hash(self.uuid)

    def __eq__(self, other):
        # Checking the UUID is mostly sufficient but requiring an exact type
        # match makes it safer in case `other` is a subclass or a completely
        # different type that happens to have a `.uuid` property. We want to
        # ensure (as best as we can) that the UUIDs we are comparing are linked
        # to the same type of QIIME 2 object.
        return (
            type(self) is type(other) and
            self.uuid == other.uuid
        )

    def __ne__(self, other):
        return not (self == other)

    def export_data(self, output_dir):
        distutils.dir_util.copy_tree(
            str(self._archiver.data_dir), str(output_dir))
        # Return None for now, although future implementations that include
        # format tranformations may return the invoked transformers
        return None

    @property
    def _destructor(self):
        return self._archiver._destructor

    def save(self, filepath, ext=None):
        """Save to a file.

        Parameters
        ----------
        filepath : str
            Path to save file at.

        extension : str
            Preferred file extension (.qza, .qzv, .txt, etc).
            If no preferred extension input is included,
            Artifact extension will default to .qza and
            Visualization extension will default to .qzv.
            Including a period in the extension is
            optional, and any additional periods delimiting
            the filepath and the extension will be reduced
            to a single period.

        Returns
        -------
        str
            Filepath and extension (if provided) that the
            file was saved to.

        See Also
        --------
        load

        """
        if ext is None:
            ext = self.extension

        # This accounts for edge cases in the filename extension
        # and ensures that there is only a single period in the ext.
        # Caste to str incase we received a pathlib.Path or similar
        filepath = str(filepath)
        filepath = filepath.rstrip('.')
        ext = '.' + ext.lstrip('.')

        if not filepath.endswith(ext):
            filepath += ext

        self._archiver.save(filepath)
        return filepath

    def snapshot(self):
        """Retain a consistent logical archive view until context close."""
        return self._archiver.snapshot()

    def _alias(self, name, provenance, ctx, qiime_type=None):
        self._assert_alias_type_refines_realized_type(qiime_type, self.type)

        if qiime_type is None:
            qiime_type = self.type

        provenance_capture = provenance.fork(name, self)

        def clone_original(into):
            # directory is empty, this function is meant to fix that, so we
            # can rmdir so that copytree is happy
            into.rmdir()
            try:
                shutil.copytree(str(self._archiver.data_dir), str(into),
                                copy_function=rachis.util.duplicate)
            except shutil.Error:
                # Try again with full copy not links
                shutil.copytree(str(self._archiver.data_dir), str(into),
                                dirs_exist_ok=True)

        cls = type(self)
        alias = cls.__new__(cls)
        alias._archiver = archive.Archiver.from_data(
            qiime_type, self.format, clone_original, provenance_capture)

        return ctx.add_reference(alias)

    def validate(self, level=NotImplemented):
        diff = self._archiver.validate_checksums()
        if diff.changed or diff.added or diff.removed:
            error = ""

            if diff.added:
                error += "Unrecognized files:\n"
            for key in diff.added:
                error += "  - %r\n" % key
            if diff.removed:
                error += "Missing files:\n"
            for key in diff.removed:
                error += "  - %r\n" % key
            if diff.changed:
                error += "Changed files:\n"
            for (key, (exp, obs)) in diff.changed.items():
                error += "  - %r: %s -> %s\n" % (key, exp, obs)

            raise exceptions.ValidationError(error)

    def result(self):
        """ Noop to provide standardized interface with ProxyResult.
        """
        return self

    def _validate_annotation_support(self):
        # Checks for the existance of `annotations_dir` on a Result's
        # format class to guard against annotation actions being called on
        # Results with versions < 7.0.

        # Raises
        # ------
        # ValueError
        #     If the Result's format class has no `annotations_dir` and is
        #     thus a format version < 7.0.

        if self._archiver.annotations_dir is None:
            raise ValueError(
                'The Artifact or Visualization being used is associated with '
                'a QIIME 2 archive format of < 7.0. '
                'Annotation actions are only supported for QIIME 2 archive '
                'formats of 7.0 and above.'
            )

    def _iter_checksums(self, checksums_fp):
        checksum_line_regex = re.compile(r"^([0-9a-f]{128})\s\s(.+)$")
        with checksums_fp.open('r', encoding='utf-8') as fh:
            for line in fh:
                line = line.rstrip('\n')
                if not line:
                    continue
                match = checksum_line_regex.match(line)
                if match:
                    yield (match.group(1), match.group(2))

    def add_annotation(self, annotation):
        self._archiver.add_annotation(annotation)

    def get_annotation(self, name):
        return self._archiver.get_annotation(name)

    def iter_annotations(self, filter_by_type=None):
        return self._archiver.iter_annotations(filter_by_type)

    def remove_annotation(self, name):
        self._archiver.remove_annotation(name)

    def merge_annotations(self, other):
        self._archiver.merge_annotations(other)

    def verify(self, signature_name):
        self.validate()
        return self._archiver.verify(signature_name)

    def metadata_paths(self):
        return self._archiver.metadata_paths()

    def redact_metadata(self):
        self._archiver.redact_metadata()


class Artifact(Result):
    extension = '.qza'

    @classmethod
    def _is_valid_type(cls, type_):
        if rachis.core.type.is_semantic_type(type_) and type_.is_concrete():
            return True
        else:
            return False

    @classmethod
    def import_data(cls, type, view, view_type=None, validate_level='max'):
        type_, type = type, __builtins__['type']

        if validate_level not in ('min', 'max'):
            raise ValueError("Expected 'min' or 'max' for `validate_level`.")

        is_format = False
        if isinstance(type_, str):
            type_ = rachis.sdk.parse_type(type_)

        if isinstance(view_type, str):
            view_type = rachis.sdk.parse_format(view_type)
            is_format = True
        # This ensures that when view_type is provided as a python class
        # or base class, the checksum is still performed
        elif (isinstance(view_type, type) and
              issubclass(view_type, rachis.core.format.FormatBase)):
            is_format = True

        if view_type is None:
            if type(view) is str or isinstance(view, pathlib.PurePath):
                is_format = True
                pm = rachis.sdk.PluginManager()
                output_dir_fmt = pm.get_directory_format(type_)
                if pathlib.Path(view).is_file():
                    if not issubclass(output_dir_fmt,
                                      model.SingleFileDirectoryFormatBase):
                        raise rachis.plugin.ValidationError(
                            "Importing %r requires a directory, not %s"
                            % (output_dir_fmt.__name__, view))
                    view_type = output_dir_fmt.file.format
                else:
                    view_type = output_dir_fmt
            else:
                view_type = type(view)

        format_ = None
        md5sums = None
        if is_format:
            if issubclass(view_type, rachis.core.format.FormatBase):
                path = pathlib.Path(str(view))
            else:
                path = pathlib.Path(view)

            if path.is_file():
                md5sums = {path.name: util.checksum(path, checksum_type='md5')}
            elif path.is_dir():
                md5sums = util.checksum_directory(path, checksum_type='md5')
            else:
                raise rachis.plugin.ValidationError(
                    "Path '%s' does not exist." % path)
            format_ = view_type

        provenance_capture = archive.ImportProvenanceCapture(format_, md5sums)
        return cls._from_view(type_, view, view_type, provenance_capture,
                              validate_level=validate_level)

    @classmethod
    def _from_view(cls, type, view, view_type, provenance_capture,
                   validate_level='min'):
        type_raw = type
        if isinstance(type, str):
            type = rachis.sdk.parse_type(type)

        if not cls._is_valid_type(type):
            raise TypeError(
                "An artifact requires a concrete semantic type, not type %r."
                % type)

        pm = rachis.sdk.PluginManager()
        output_dir_fmt = pm.get_directory_format(type)

        if view_type is None:
            # lookup default format for the type
            view_type = output_dir_fmt

        from_type = transform.ModelType.from_view_type(view_type)
        to_type = transform.ModelType.from_view_type(output_dir_fmt)

        recorder = provenance_capture.transformation_recorder('return')
        transformation = from_type.make_transformation(to_type,
                                                       recorder=recorder)
        result = transformation(view, validate_level)

        if type_raw in pm.validators:
            validation_object = pm.validators[type]
            validation_object(data=result, level=validate_level)

        artifact = cls.__new__(cls)
        artifact._archiver = archive.Archiver.from_data(
            type, output_dir_fmt,
            data_initializer=result.path._move_or_copy,
            provenance_capture=provenance_capture)

        return artifact

    def view(self, view_type):
        return self._view(view_type)

    def _view(self, view_type, recorder=None):
        if view_type is rachis.Metadata and not self.has_metadata():
            raise TypeError(
                "Artifact %r cannot be viewed as Rachis Metadata." % self)

        from_type = transform.ModelType.from_view_type(self.format)

        if isinstance(get_origin(view_type), type(Union)):
            transformation = None
            for arg in get_args(view_type):
                to_type = transform.ModelType.from_view_type(arg)
                try:
                    transformation = from_type.make_transformation(
                        to_type, recorder=recorder)
                    if transformation:
                        break
                except Exception as e:
                    if str(e).startswith("No transformation from"):
                        continue
                    else:
                        raise e
            if not transformation:
                raise Exception(
                    "No transformation into either of %s was found" %
                    ", ".join([str(x) for x in view_type.__args__])
                )
        else:
            to_type = transform.ModelType.from_view_type(view_type)
            transformation = from_type.make_transformation(to_type,
                                                           recorder=recorder)
        result = transformation(self._archiver.data_dir)

        if view_type is rachis.Metadata:
            result._add_artifacts([self])

        to_type.set_user_owned(result, True)
        return result

    def has_metadata(self):
        """ Checks for metadata within an artifact

        Returns
        -------
        bool
           True if the artifact has metadata (i.e. can be viewed as
           ``rachis.Metadata``), False otherwise.

        """
        from_type = transform.ModelType.from_view_type(self.format)
        to_type = transform.ModelType.from_view_type(rachis.Metadata)
        return from_type.has_transformation(to_type)

    def validate(self, level='max'):
        """ Validates the data contents of an artifact

        Raises
        ------
        ValidationError
            If the artifact is invalid at the specified level of validation.
        """
        super().validate()

        self.format.validate(self.view(self.format), level)

    def get_checksums(self) -> dict[str, str]:
        return self._archiver.get_checksums()

    def validate_checksums(self):
        super().validate()


class Visualization(Result):
    extension = '.qzv'

    @classmethod
    def _is_valid_type(cls, type_):
        return type_ == rachis.core.type.Visualization

    @classmethod
    def _from_data_dir(cls, data_dir, provenance_capture):
        # shutil.copytree doesn't allow the destination directory to exist.
        def data_initializer(destination):
            return distutils.dir_util.copy_tree(
                str(data_dir), str(destination))

        viz = cls.__new__(cls)
        viz._archiver = archive.Archiver.from_data(
            rachis.core.type.Visualization, None,
            data_initializer=data_initializer,
            provenance_capture=provenance_capture)
        return viz

    @classmethod
    def make_report(cls, template, collection, descriptions=None):
        """Make a report out of existing visualizations and a template.

        Parameters
        ----------
        template : Callable[[str, dict], None]
            A template function which is given the output directory as the
            first argument and an index of subfigures as the second.

        collection : dict[str, Visualization]
            The visualizations to make available to the template. It is up
            to the template to use them, but they will exist in a specialized
            subfigures directory unique to the report format for Visualization.

        descriptions : dict[str, str], optional
            Plain-text descriptions for report entries, keyed by their names
            in ``collection``.

        Returns
        -------
        Visualization
            The newly created report as a Visualization. It will have the
            format of "report".

        """
        if descriptions is None:
            descriptions = {}

        unknown_descriptions = descriptions.keys() - collection.keys()
        if unknown_descriptions:
            raise ValueError(
                'Descriptions were provided for visualizations not in the '
                f'collection: {sorted(unknown_descriptions)!r}')

        provenance_capture = archive.ReportProvenanceCapture()
        to_reindex = {}

        def data_initializer(destination):
            index = {}
            subfigures_dir = os.path.join(destination, 'subfigures')
            for key, viz in collection.items():
                if key in descriptions and not isinstance(descriptions[key],
                                                          str):
                    raise TypeError(
                        f'Description for {key!r} must be plain text.')

                viz_uuid = str(viz.uuid)
                provenance_capture.add_input(key, viz)
                index[key] = {
                    "name": key,
                    "index": f'subfigures/{viz_uuid}/index.html',
                }
                if key in descriptions:
                    index[key]['description'] = descriptions[key]
                subfigure_root = os.path.join(subfigures_dir, viz_uuid)
                if not os.path.exists(subfigure_root):
                    shutil.copytree(viz._archiver.data_dir, subfigure_root)

                # if not for reports of reports, this would have been the end
                # of the data_initializer, however we don't want nested
                # reports to have an arbitrarily long path as this will break
                # things. Also we get de-duplication for free if we hoist
                # sub-subfigures into just subfigures of the outermost report
                if viz.format is report:
                    # reports which were provided as part of the collection
                    # will have sub-figures moved and index.json re-written
                    # to a new flattened path
                    to_reindex[viz_uuid] = set()
                    child_figs = os.path.join(subfigure_root, 'subfigures')
                    with open(os.path.join(child_figs, 'index.json')) as fh:
                        inner_index = json.load(fh)
                        index[key]['children'] = inner_index
                        # collect the set of inner subfigures which may be
                        # referenced by the inner report. The `index` key
                        # holds the "url" `subfigures/<uuid>/index.html`
                        # so we get the subfigure UUID from there.
                        to_reindex[viz_uuid] = \
                            set(map(lambda x: x['index'].split('/')[-2],
                                    util.flatten_children(inner_index)))

                    for uuid in os.listdir(child_figs):
                        if not os.path.isdir(os.path.join(child_figs, uuid)):
                            continue
                        child_root = os.path.join(child_figs, uuid)
                        flattened_dest = os.path.join(subfigures_dir, uuid)
                        if os.path.exists(child_root):
                            if os.path.exists(flattened_dest):
                                # exists already via another subfigure
                                shutil.rmtree(child_root)
                            else:
                                # hoist the sub-subfigure into a subfigure
                                shutil.move(child_root, flattened_dest)

            for report_uuid, children in to_reindex.items():
                subfigure_root = os.path.join(subfigures_dir, report_uuid)
                for uuid in children:
                    # correct any references to the original sub-subfigures
                    # they are now the hoisted/flattened path
                    util.replace_bytes_in_directory(
                        subfigure_root,
                        f'subfigures/{uuid}/index.html'.encode(),
                        f'../{uuid}/index.html'.encode(),
                        {'.json', '.jsonp', '.js', '.htm', '.html'}
                    )

            with open(os.path.join(subfigures_dir, 'index.json'), 'w') as fh:
                json.dump(index, fh, indent=2)

            template(destination, index)

        viz = cls.__new__(cls)
        viz._archiver = archive.Archiver.from_data(
            rachis.core.type.Visualization, report,
            data_initializer=data_initializer,
            provenance_capture=provenance_capture
        )
        return viz

    def get_index_paths(self, relative=True):
        result = {}
        for abspath in self._archiver.data_dir.iterdir():
            data_path = str(abspath.relative_to(self._archiver.data_dir))
            if data_path.startswith('index.'):
                if hasattr(self._archiver.root_dir, 'view'):
                    relpath = pathlib.Path('data') / abspath.relative_to(
                        self._archiver.data_dir)
                else:
                    relpath = abspath.relative_to(self._archiver.root_dir)
                ext = relpath.suffix[1:]
                if ext in result:
                    raise ValueError(
                        "Multiple index files identified with %s "
                        "extension (%s, %s). This is currently "
                        "unsupported." %
                        (ext, result[ext], relpath))
                else:
                    result[ext] = str(relpath) if relative else str(abspath)
        return result

    def _repr_mimebundle_(self, include=None, exclude=None):
        import base64
        with tempfile.NamedTemporaryFile() as fh:
            self._archiver.save(fh.name)
            b64 = base64.b64encode(fh.read()).decode('ascii')
            return {
                "text/plain": repr(self),
                "application/vnd.rachis.archive+zip": b64,
            }


class ResultCollection:
    @classmethod
    def load(cls, directory):
        """ Determines how to load a Collection of QIIME 2 Artifacts in a
            directory and dispatches to helpers
        """
        if not os.path.isdir(directory):
            raise ValueError(
                f"Given filepath '{directory}' is not a directory")

        order_fp = os.path.join(directory, '.order')

        if os.path.isfile(order_fp):
            collection = cls._load_ordered(directory, order_fp)
        else:
            warnings.warn(f"The directory '{directory}' does not contain a "
                          ".order file. The files will be read into the "
                          "collection in the order the filesystem provides "
                          "them in.")
            collection = cls._load_unordered(directory)

        return collection

    @classmethod
    def _load_ordered(cls, directory, order_fp):
        collection = cls()

        with open(order_fp, 'r') as order_fh:
            for result_name in order_fh.read().splitlines():
                result_fp = cls._get_result_fp(directory, result_name)
                collection[result_name] = Result.load(result_fp)

        return collection

    @classmethod
    def _load_unordered(cls, directory):
        collection = cls()

        for result in os.listdir(directory):
            result_fp = os.path.join(directory, result)
            result_name = result.rstrip('.qza')
            result_name = result_name.rstrip('.qzv')

            collection[result_name] = Result.load(result_fp)

        return collection

    @classmethod
    def _get_result_fp(cls, directory, result_name):
        result_fp = os.path.join(directory, result_name)

        # Check if thing in .order file exists and if not try it with .qza at
        # the end and if not try it with .qzv at the end
        if not os.path.isfile(result_fp):
            result_fp += '.qza'

            if not os.path.isfile(result_fp):
                # Get rid of the trailing .qza before adding .qzv
                result_fp = result_fp[:-4]
                result_fp += '.qzv'

                if not os.path.isfile(result_fp):
                    raise ValueError(
                        f"The Result '{result_name}' is referenced in the "
                        "order file but does not exist in the directory.")

        return result_fp

    def __init__(self, collection=None):
        if collection is None:
            self.collection = {}
        elif isinstance(collection, dict):
            rachis.sdk.util.validate_result_collection_keys(*collection.keys())

            self.collection = collection
        else:
            self.collection = {str(k): v for k, v in enumerate(collection)}

    def __contains__(self, item):
        return item in self.collection

    def __eq__(self, other):
        if isinstance(other, dict):
            return self.collection == other
        elif isinstance(other, ResultCollection):
            return self.collection == other.collection
        else:
            raise TypeError(f"Equality between '{type(other)}' and "
                            "ResultCollection is undefined.")

    def __len__(self):
        return len(self.collection)

    def __iter__(self):
        return iter(self.collection)

    def __setitem__(self, key, item):
        rachis.sdk.util.validate_result_collection_keys(key)
        self.collection[key] = item

    def __getitem__(self, key):
        return self.collection[key]

    def __repr__(self):
        return f"<{self.__class__.__name__.lower()}: {self.type}>"

    @property
    def type(self):
        inner_type = rachis.core.type.grammar.UnionExp(
            v.type for v in self.collection.values()).normalize()

        return rachis.core.type.Collection[inner_type]

    @property
    def extension(self):
        if str(self.type) == 'Collection[Visualization]':
            return '.qzv'

        return '.qza'

    def save(self, directory):
        """Saves a collection of QIIME 2 Results into a given directory with
           an order file.

           NOTE: The directory given must not exist
        """
        if os.path.exists(directory):
            raise ValueError(f"The given directory '{directory}' already "
                             "exists. A new directory must be given to save "
                             "the collection to.")

        os.makedirs(directory)

        order_string = ''
        for name, result in self.collection.items():
            result_fp = os.path.join(directory, name)
            result.save(result_fp)
            order_string += f'{name}\n'

        with open(os.path.join(directory, '.order'), 'w') as fh:
            fh.write(order_string)

        # Do this to give us a unified API with Result.save
        return directory

    def save_unordered(self, directory):
        """Saves a collection of QIIME 2 Results into a given directory without
           an order file. This is used by q2galaxy where an order file will be
           interpreted as another dataset in the collection which is not
           desirable

           NOTE: The directory given must not exist
        """
        if os.path.exists(directory):
            raise ValueError(f"The given directory '{directory}' already "
                             "exists. A new directory must be given to save "
                             "the collection to.")

        os.makedirs(directory)

        for name, result in self.collection.items():
            result_fp = os.path.join(directory, name)
            result.save(result_fp)

        # Do this to give us a unified API with Result.save
        return directory

    def keys(self):
        return self.collection.keys()

    def values(self):
        return self.collection.values()

    def items(self):
        return self.collection.items()

    def validate(self, level=None):
        for result in self.values():
            result.validate(level)

    def validate_checksums(self):
        for result in self.values():
            if isinstance(result, Artifact):
                result.validate_checksums()
            elif isinstance(result, Result):
                result.validate()

    def result(self):
        """ Noop to provide standardized interface with ProxyResultCollection.
        """
        return self


class ChecksumCache:
    _instance = None

    def __new__(cls):
        '''
        Implements a cache of artifact file checksums. Maps artifact filepaths
        beginning with their uuids to checksums.

        The cache is populated at the beginning of an action using all input
        artifacts and is queried at the end of an action to prevent redundant
        checksumming of the provenance files in the new output artifacts.
        '''
        if cls._instance is None:
            instance = super().__new__(cls)
            instance.cache: dict[pathlib.Path, str] = {}
            cls._instance = instance

        return cls._instance

    def get(self, filepath: pathlib.Path) -> str | None:
        '''
        Gets the checksum of the artifact file at `filepath`.

        Parameters
        ----------
        filepath : pathlib.Path
            The filepath including the artifact and file of interest.

        Returns
        -------
        str | None
            The checksum if found, None otherwise.
        '''
        if filepath not in self.cache:
            return None

        return self.cache[filepath]

    def has_matching_checksum_type(self, artifact: Artifact) -> bool:
        '''
        Determines whether an artifact uses the same checksumming algorithm
        as the current one.

        Parameters
        ----------
        artifact : Artifact
            The artifact for which to determine the checksum algorithm type.

        Returns
        -------
        bool
            True if the artifact uses the same checksum type as the current
            one, False otherwise.
        '''
        if not artifact._archiver.has_checksums():
            return False

        artifact_checksum_type = artifact._archiver._fmt.CHECKSUM_TYPE
        current_checksum_type = archive.Archiver.get_format_class(
            archive.Archiver.CURRENT_FORMAT_VERSION
        ).CHECKSUM_TYPE

        return artifact_checksum_type == current_checksum_type

    def cache_artifact(self, artifact: Artifact) -> None:
        '''
        Adds a single artifact's checksum file's contents to the checksum
        cache.

        If the checksum type associated with `artifact` is different than the
        checksum type associated with the current archive version, then the
        artifact is not cached. This allows the filepaths associated with
        `artifact` to be re-checksummed with the new algorithm at output write
        time, as will be needed for future validation.

        Parameters
        ----------
        artifact : Artifact
            The artifact to cache.
        '''
        if not self.has_matching_checksum_type(artifact):
            return

        for fp, checksum in artifact.get_checksums().items():
            path = pathlib.Path(fp)

            if path.is_relative_to(pathlib.Path("provenance/artifacts")):
                key = path.relative_to(pathlib.Path("provenance/artifacts"))
            elif path.is_relative_to(pathlib.Path("provenance")):
                key = (
                    pathlib.Path(str(artifact.uuid)) /
                    path.relative_to(pathlib.Path("provenance"))
                )
            else:
                # all other files are not included in the provenance of any
                # artifact generated using this artifact as input
                continue

            self.cache[key] = checksum

    def cache_result_collection(
        self, result_collection: ResultCollection
    ) -> None:
        '''
        Adds all artifacts in a result collection to the checksum cache.

        Parameters
        ----------
        result_collection : ResultCollection
            The result collection containing the artifacts to cache.
        '''
        for artifact in result_collection.values():
            self.cache_artifact(artifact)
