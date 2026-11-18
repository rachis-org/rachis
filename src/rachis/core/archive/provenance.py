# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import os
import time
import collections
import collections.abc
import platform
import importlib.metadata
import uuid
import copy
import shutil
import sys
import warnings
from datetime import datetime, timezone
from typing import Any, List, NamedTuple, Set, Union
from pathlib import Path

import distutils
import yaml
import tzlocal
import dateutil.relativedelta as relativedelta

import rachis
import rachis.core.util as util
from rachis.core.cite import Citations


IMPORTLIB_DISTRIBUTIONS_CACHE = None


def _ts_to_date(ts):
    time_zone = timezone.utc
    try:
        time_zone = tzlocal.get_localzone()
    except ValueError:
        pass
    return datetime.fromtimestamp(ts, tz=time_zone)


# Used to give PyYAML something to recognize for custom tags
ForwardRef = collections.namedtuple('ForwardRef', ['reference'])
NoProvenance = collections.namedtuple('NoProvenance', ['uuid'])
MetadataPath = collections.namedtuple('MetadataPath', ['path'])
ColorPrimitive = collections.namedtuple('ColorPrimitive', ['hex'])
LiteralString = collections.namedtuple('LiteralString', ['string'])
CitationKey = collections.namedtuple('CitationKey', ['key'])


class OrderedKeyValue(collections.OrderedDict):
    pass


# Used for yaml that looks like:
#   - key1: value1
#   - key2: value2
yaml.add_representer(OrderedKeyValue, lambda dumper, data:
                     dumper.represent_list([
                        {k: v} for k, v in data.items()]))


# Controlling the order of dictionaries (even if semantically irrelevant) is
# important to making it look nice.
yaml.add_representer(collections.OrderedDict, lambda dumper, data:
                     dumper.represent_dict(data.items()))


# YAML libraries aren't good at writing a clean version of this, and typically
# the fact that it is a set is irrelevant to tools that use provenance
# so add a custom tag and treat it like a sequence. Then code doesn't need to
# special case set vs list in their business logic when it isn't important.
yaml.add_representer(set, lambda dumper, data:
                     dumper.represent_sequence('!set', data))


# LiteralString uses the | character and has literal newlines
yaml.add_representer(LiteralString, lambda dumper, data:
                     dumper.represent_scalar('tag:yaml.org,2002:str',
                                             data.string, style='|'))


# Make our timestamps pretty (unquoted).
yaml.add_representer(datetime, lambda dumper, data:
                     dumper.represent_scalar('tag:yaml.org,2002:timestamp',
                                             data.isoformat()))


# Forward reference to something else in the document, namespaces are
# delimited by colons (:).
yaml.add_representer(ForwardRef, lambda dumper, data:
                     dumper.represent_scalar('!ref', data.reference))


# This tag represents an artifact without provenance, this is to support
# archive format v0. Ideally this won't be seen in the wild in practice.
yaml.add_representer(NoProvenance, lambda dumper, data:
                     dumper.represent_scalar('!no-provenance', str(data.uuid)))


# A reference to Metadata and MetadataColumn whose data can be found at the
# relative path indicated as its value
yaml.add_representer(MetadataPath, lambda dumper, data:
                     dumper.represent_scalar('!metadata', data.path))

# A color primitive.
yaml.add_representer(ColorPrimitive, lambda dumper, data:
                     dumper.represent_scalar('!color', data.hex))

yaml.add_representer(CitationKey, lambda dumper, data:
                     dumper.represent_scalar('!cite', data.key))


def citation_key_constructor(loader, node) -> str:
    """
    A constructor for !cite yaml tags, returning a bibtex key as a str.
    All we need for now is a key string we can match in citations.bib,
    so _we're not parsing these into component substrings_.

    If that need arises in future, these are spec'ed in provenance.py as:
    <domain>|<package>:<version>|[<identifier>|]<index>

    and frequently look like this (note no identifier):
    framework|rachis:2020.6.0.dev0|0
    """
    value = loader.construct_scalar(node)
    return value


def color_constructor(loader, node) -> str:
    """
    Constructor for !color tags, returning an str.
    Color was a primitive type representing a 3 or 6 digit color hex code,
    matching ^#(?:[0-9a-fA-F]{3}){1,2}$

    Per E. Bolyen,these were unused by any plugins. They were removed in
    e58ed5f8ba453035169d560e0223e6a37774ae08, which was released in 2019.4
    """
    return loader.construct_scalar(node)


class MetadataInfo(NamedTuple):
    """
    A namedtuple representation of the data in one !metadata yaml tag.

    Attributes
    ----------
    input_artifact_uuids : list of str
        The uuids of any artifacts viewed as metadata.
    relative_fp : str
        The filepath of the metadata file relative to the action.yaml file.
    md5sum_hash : str
        The md5sum hash of the contents of the corresponding metadata file,
        needed by rachis.core.cache to tell if two metadata inputs are equal.
    """
    input_artifact_uuids: List[str]
    relative_fp: str
    md5sum_hash: str


def metadata_path_constructor(loader, node) -> MetadataInfo:
    """
    A constructor for !metadata yaml tags, which come in the form
    [<uuid_ref>[,<uuid_ref>][...]:]<relative_filepath>

    Returns a MetadataInfo object containing a list of UUIDs, and the relative
    filepath where the metadata was written into the zip archive

    Most commonly, we see:
    !metadata 'sample_metadata.tsv'

    In cases where Artifacts are used as metadata, we see:
    !metadata '415409a4-371d-4c69-9433-e3eaba5301b4:feature_metadata.tsv'

    In cases where multiple Artifacts as metadata were merged,
    it is possible for multiple comma-separated uuids to precede the ':'
    !metadata '<uuid1>,<uuid2>,...,<uuidn>:feature_metadata.tsv'

    The metadata files (including "Artifact metadata") are saved in the same
    dir as `action.yaml`. The UUIDs listed must be incorporated into our
    provenance graph as parents, so are returned in list form.

    NOTES
    -----
    Assumes `loader` has been passed a filehandle to an action.yaml file.
    If instead of a filehandle e.g. file contents are passed, will break
    because `loader.name` will no longer be a useful path that we can use
    to find the corresponding metadata file.
    """
    raw = loader.construct_scalar(node)
    if ':' in raw:
        artifact_uuids, rel_fp = raw.split(':')
        artifact_uuids = artifact_uuids.split(',')
    else:
        artifact_uuids = []
        rel_fp = raw

    action_fp = Path(loader.name)
    metadata_fp = action_fp.parent / rel_fp
    md5sum_hash = util.checksum(metadata_fp, checksum_type='md5')

    return MetadataInfo(artifact_uuids, rel_fp, md5sum_hash)


def no_provenance_constructor(loader, node) -> str:
    """
    Constructor for !no-provenance tags. These tags are written by QIIME 2 when
    an input has no /provenance dir, as in the case of v0 archives that have
    been used in analyses in QIIME2 V1+. They look like this:

    action:
       inputs:
       -   table: !no-provenance '34b07e56-27a5-4f03-ae57-ff427b50aaa1'

    For now at least, this constructor warns but otherwise disregards the
    no-provenance-ness of these. The v0 parser deals with them directly anyway.
    """
    uuid = loader.construct_scalar(node)
    warnings.warn(f"Artifact {uuid} was created prior to provenance tracking. "
                  + "Provenance data will be incomplete.", UserWarning)
    return uuid


def ref_constructor(loader, node) -> Union[str, List[str]]:
    """
    A constructor for !ref yaml tags. These tags describe yaml values that
    reference other namespaces within the document, using colons to separate
    namespaces. For example:
    !ref 'environment:plugins:sample-classifier'

    At present, ForwardRef tags are only used in the framework to 'link' the
    plugin name to the plugin version and other details in the 'execution'
    namespace of action.yaml

    This constructor explicitly handles this type of !ref by extracting and
    returning the plugin name to simplify parsing, while supporting the return
    of a generic list of 'keys' (e.g. ['environment', 'framework', 'version'])
    in the event ForwardRef is used more broadly in future.
    """
    value = loader.construct_scalar(node)
    keys = value.split(':')
    if keys[0:2] == ['environment', 'plugins']:
        plugin_name = keys[2]
        return plugin_name
    else:
        return keys


def set_constructor(loader, node) -> Set[Any]:
    """
    A constructor for !set yaml tags, returning a python set object
    """
    value = loader.construct_sequence(node)
    return set(value)


# NOTE: New yaml tag constructors must be added to this registry, or tags will
# raise ConstructorErrors
CONSTRUCTOR_REGISTRY = {
    '!cite': citation_key_constructor,
    '!color': color_constructor,
    '!metadata': metadata_path_constructor,
    '!no-provenance': no_provenance_constructor,
    '!ref': ref_constructor,
    '!set': set_constructor,
}

for key in CONSTRUCTOR_REGISTRY:
    yaml.SafeLoader.add_constructor(key, CONSTRUCTOR_REGISTRY[key])


class ZipActionYamlLoader(yaml.SafeLoader):
    pass


def zipfile_metadata_constructor(loader, node):
    return loader.construct_scalar(node)


ZipActionYamlLoader.add_constructor('!metadata', zipfile_metadata_constructor)


class ProvenanceCapture:
    ANCESTOR_DIR = 'artifacts'
    ACTION_DIR = 'action'
    TEMP_ANNOTATIONS_DIR = 'annotations'
    ACTION_FILE = 'action.yaml'
    CITATION_FILE = 'citations.bib'

    def __init__(self):
        self.start = time.time()
        self.uuid = uuid.uuid4()
        self.end = None
        self.plugins = collections.OrderedDict()

        # For the purposes of this dict, `return` is a special case for output
        # we expect to transform this later when serializing, but this lets
        # us treat all transformations uniformly.
        self.transformers = collections.OrderedDict()
        self.citations = Citations()
        self._framework_citations = []

        for idx, citation in enumerate(rachis.__citations__):
            citation_key = self.make_citation_key('framework')
            self.citations[citation_key.key] = citation
            self._framework_citations.append(citation_key)

        self._build_paths()

    @property
    def _destructor(self):
        return self.path._destructor

    def _build_paths(self):
        self.path = rachis.core.path.ProvenancePath()

        self.ancestor_dir = self.path / self.ANCESTOR_DIR
        self.ancestor_dir.mkdir()

        self.action_dir = self.path / self.ACTION_DIR
        self.action_dir.mkdir()

        self.temp_annotations_dir = self.path / self.TEMP_ANNOTATIONS_DIR
        self.temp_annotations_dir.mkdir()

    def add_ancestor(self, artifact):
        other_path = artifact._archiver.provenance_dir
        if other_path is None:
            # The artifact doesn't have provenance (e.g. version 0)
            # it would be possible to invent a metadata.yaml, but we won't know
            # the framework version for the VERSION file. Even if we did
            # it won't accomplish a lot and there shouldn't be enough
            # version 0 artifacts in the wild to be important in practice.
            # NOTE: this implies that it is possible for an action.yaml file to
            # contain an artifact UUID that is not in the artifacts/ directory.
            return NoProvenance(artifact.uuid)

        destination = self.ancestor_dir / str(artifact.uuid)
        # If it exists, then the artifact is already in the provenance
        # (and so are its ancestors)
        if not destination.exists():
            # Handle root node of ancestor
            shutil.copytree(
                str(other_path), str(destination),
                ignore=shutil.ignore_patterns(self.ANCESTOR_DIR + '*'))

            # Handle ancestral nodes of ancestor
            grandcestor_path = other_path / self.ANCESTOR_DIR
            if grandcestor_path.exists():
                for grandcestor in grandcestor_path.iterdir():
                    destination = self.ancestor_dir / grandcestor.name
                    if not destination.exists():
                        shutil.copytree(str(grandcestor), str(destination))

        # preserve ancestral annotations
        annotations_dir = artifact._archiver.annotations_dir
        if annotations_dir and annotations_dir.exists():
            for annotation in annotations_dir.iterdir():
                destination = self.temp_annotations_dir / annotation.name
                if not destination.exists():
                    shutil.copytree(str(annotation), str(destination))

        return str(artifact.uuid)

    def make_citation_key(self, domain, package=None, identifier=None,
                          index=0):
        if domain == 'framework':
            package, version = 'rachis', rachis.__version__
        else:
            package, version = package.name, package.version
        id_block = [] if identifier is None else [identifier]

        return CitationKey('|'.join(
            [domain, package + ':' + version] + id_block + [str(index)]))

    def make_software_entry(self, version, website, citations=()):
        entry = collections.OrderedDict()

        entry['version'] = version
        entry['website'] = website
        if citations:
            entry['citations'] = citations

        return entry

    def reference_plugin(self, plugin):
        plugin_citations = []
        for idx, citation in enumerate(plugin.citations):
            citation_key = self.make_citation_key('plugin', plugin, index=idx)
            self.citations[citation_key.key] = citation
            plugin_citations.append(citation_key)

        self.plugins[plugin.name] = self.make_software_entry(
            plugin.version, plugin.website, plugin_citations)

        return ForwardRef('environment:plugins:' + plugin.name)

    def capture_env(self):
        global IMPORTLIB_DISTRIBUTIONS_CACHE
        if IMPORTLIB_DISTRIBUTIONS_CACHE is None:
            IMPORTLIB_DISTRIBUTIONS_CACHE = collections.OrderedDict(
                (d.metadata["Name"], d.metadata["Version"]) for d in
                importlib.metadata.distributions()
            )
        return IMPORTLIB_DISTRIBUTIONS_CACHE

    def transformation_recorder(self, name):
        section = self.transformers[name] = []

        def recorder(transformer_record, input_name, input_record, output_name,
                     output_record):
            entry = collections.OrderedDict()
            entry['from'] = input_name
            entry['to'] = output_name
            citation_keys = []

            if transformer_record is not None:
                plugin = transformer_record.plugin
                entry['plugin'] = self.reference_plugin(plugin)

                for idx, citation in enumerate(transformer_record.citations):
                    citation_key = self.make_citation_key(
                        'transformer', plugin,
                        '%s->%s' % (input_name, output_name), idx)
                    self.citations[citation_key.key] = citation
                    citation_keys.append(citation_key)

            records = []
            if input_record is not None:
                records.append(input_record)
            if output_record is not None:
                records.append(output_record)
            for record in records:
                self.reference_plugin(record.plugin)
                for idx, citation in enumerate(record.citations):
                    citation_key = self.make_citation_key(
                        'view', record.plugin, record.name, idx)
                    self.citations[citation_key.key] = citation
                    citation_keys.append(citation_key)

            if citation_keys:
                entry['citations'] = citation_keys

            # Don't create duplicate transformer records. These were happening
            # with collections of inputs. If we have a method that takes a
            # List[IntSequence1] with view type of list, we need to transform
            # every IntSequence1 into a list to match the view type. This would
            # add a transformation record for every IntSequence1 in the list.
            # This ensures we only end up with one record of a given type for a
            # given input while still allowing multiple unique records.
            # NOTE: This does redundant work creating the record, do we care?
            if entry not in section:
                section.append(entry)

        return recorder

    def make_execution_section(self):
        execution = collections.OrderedDict()
        execution['uuid'] = str(self.uuid)
        execution['runtime'] = runtime = collections.OrderedDict()
        runtime['start'] = start = _ts_to_date(self.start)
        runtime['end'] = end = _ts_to_date(self.end)
        runtime['duration'] = \
            util.duration_time(relativedelta.relativedelta(end, start))

        if not isinstance(self, (ImportProvenanceCapture,
                                 ReportProvenanceCapture)):
            execution['execution_context'] = collections.OrderedDict(
                {k: v for k, v in self.execution_context.items()})

        return execution

    def make_transformers_section(self):
        transformers = collections.OrderedDict()
        data = self.transformers.copy()
        output = data.pop('return', None)
        if data:
            transformers['inputs'] = data
        if output is not None:
            transformers['output'] = output
        return transformers

    def make_env_section(self):
        env = collections.OrderedDict()
        env['platform'] = platform.platform()
        # There is a trailing whitespace in sys.version, strip so that YAML can
        # use literal formatting.
        env['python'] = LiteralString('\n'.join(line.strip() for line in
                                      sys.version.split('\n')))
        env['framework'] = self.make_software_entry(
            rachis.__version__, rachis.__website__, self._framework_citations)
        env['plugins'] = self.plugins

        # sort pkgs alphabetically, ignoring upper/lower casing
        unsorted_packages = self.capture_env()
        sorted_packages = collections.OrderedDict(
            sorted(unsorted_packages.items(), key=lambda x: x[0].lower())
        )
        env['python-packages'] = sorted_packages

        return env

    def write_action_yaml(self):
        settings = dict(default_flow_style=False, indent=4)
        with (self.action_dir / self.ACTION_FILE).open(mode='w') as fh:
            fh.write(yaml.dump({'execution': self.make_execution_section()},
                               **settings))
            fh.write('\n')
            fh.write(yaml.dump({'action': self.make_action_section()},
                               **settings))
            if self.transformers:  # pipelines don't have these
                fh.write('\n')
                fh.write(yaml.dump(
                    {'transformers': self.make_transformers_section()},
                    **settings))
            fh.write('\n')
            fh.write(yaml.dump({'environment': self.make_env_section()},
                               **settings))

    def write_citations_bib(self):
        self.citations.save(str(self.path / self.CITATION_FILE))

    def finalize(self, final_path, node_members):
        self.end = time.time()

        for member in node_members:
            shutil.copy(str(member), str(self.path))

        self.write_action_yaml()
        self.write_citations_bib()

        # Certain networked filesystems will experience a race
        # condition on `rename`, so fall back to copying.
        try:
            os.rename(self.path, final_path)
        except (FileExistsError, OSError) as err:
            if isinstance(err, FileExistsError) or isinstance(err, OSError) \
                 and err.errno == 18:
                distutils.dir_util.copy_tree(str(self.path), str(final_path))
                distutils.dir_util.remove_tree(str(self.path))
            else:
                raise err

    def fork(self):
        forked = copy.copy(self)
        # Unique state for each output of an action
        forked.plugins = forked.plugins.copy()
        forked.transformers = forked.transformers.copy()
        forked.citations = forked.citations.copy()
        # create a copy of the backing dir so factory (the hard stuff is
        # mostly done by this point)
        forked._build_paths()
        distutils.dir_util.copy_tree(str(self.path), str(forked.path))

        return forked


class ImportProvenanceCapture(ProvenanceCapture):
    def __init__(self, format=None, checksums=None):
        super().__init__()
        self.format_name = format.__name__ if format is not None else None
        self.checksums = checksums

    def make_action_section(self):
        action = collections.OrderedDict()
        action['type'] = 'import'
        if self.format_name is not None:
            action['format'] = self.format_name
        if self.checksums is not None:
            action['manifest'] = [
                collections.OrderedDict([('name', name), ('md5sum', md5sum)])
                for name, md5sum in self.checksums.items()]

        return action


class ActionProvenanceCapture(ProvenanceCapture):
    def __init__(self, action_type, plugin_id, action_id, execution_context):
        from rachis.sdk import PluginManager

        super().__init__()
        self._plugin = PluginManager().get_plugin(id=plugin_id)
        self.action = self._plugin.actions[action_id]
        self.action_type = action_type
        self.inputs = OrderedKeyValue()
        self.parameters = OrderedKeyValue()
        self.output_name = ''
        self.execution_context = execution_context

        self._action_citations = []
        for idx, citation in enumerate(self.action.citations):
            citation_key = self.make_citation_key(
                'action', self._plugin,
                ':'.join([self.action_type, self.action.id]), idx)
            self.citations[citation_key.key] = citation
            self._action_citations.append(citation_key)

    def handle_metadata(self, name, value):
        if value is None:
            return None

        uuid_ref = ""
        if value.artifacts:
            uuids = []
            for artifact in value.artifacts:
                uuids.append(str(artifact.uuid))
                self.add_ancestor(artifact)
            uuid_ref = ",".join(uuids) + ":"

        relpath = name + '.tsv'
        value.save(str(self.action_dir / relpath))

        return MetadataPath(uuid_ref + relpath)

    def add_parameter(self, name, type_expr, parameter):
        type_map = {
            'Color': ColorPrimitive,
            'Metadata': lambda x: self.handle_metadata(name, x),
            'MetadataColumn': lambda x: self.handle_metadata(name, x)
            # TODO: handle collection primitives (not currently used)
        }

        # Make sure if we get a Collection of params the items are put into
        # provenance in the right order
        if isinstance(parameter, dict):
            parameter = [{k: v} for k, v in parameter.items()]

        handler = type_map.get(type_expr.to_ast().get('name'), lambda x: x)
        self.parameters[name] = handler(parameter)

    def add_input(self, name, input):
        if input is None:
            self.inputs[name] = None
        elif isinstance(input, rachis.sdk.result.ResultCollection):
            # If we took a Collection input, we will have a ResultCollection,
            # and we want the keys to line up with the processed values we were
            # given, so we can maintain the order of the artifacts
            self.inputs[name] = \
                [{k: self.add_ancestor(v)} for k, v in input.items()]
        elif isinstance(input, collections.abc.Iterable):
            self.inputs[name] = type(input)(
                [self.add_ancestor(artifact) for artifact in input])
        else:
            self.inputs[name] = self.add_ancestor(input)

    def make_action_section(self):
        action = collections.OrderedDict()
        action['type'] = self.action_type
        action['plugin'] = self.reference_plugin(self._plugin)
        action['action'] = self.action.id
        action['inputs'] = self.inputs
        action['parameters'] = self.parameters
        action['output-name'] = self.output_name

        if self._action_citations:
            action['citations'] = self._action_citations

        return action

    def fork(self, name):
        forked = super().fork()
        forked.output_name = name
        return forked


class PipelineProvenanceCapture(ActionProvenanceCapture):
    def make_action_section(self):
        action = super().make_action_section()
        action['alias-of'] = str(self.alias.uuid)

        return action

    def fork(self, name, alias):
        forked = super().fork(name)
        forked.alias = alias
        forked.add_ancestor(alias)
        return forked


class ReportProvenanceCapture(ProvenanceCapture):
    def __init__(self):
        super().__init__()
        self.inputs = OrderedKeyValue()

    def add_input(self, name, input):
        self.inputs[name] = self.add_ancestor(input)

    def make_action_section(self):
        action = collections.OrderedDict()
        action['type'] = 'report'
        action['inputs'] = self.inputs

        return action
