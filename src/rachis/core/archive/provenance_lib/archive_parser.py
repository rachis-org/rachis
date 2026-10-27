# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------
import abc
import pandas as pd
import warnings
import yaml

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

import bibtexparser as bp
import networkx as nx

from ._checksum_validator import (
    ValidationCode, ChecksumDiff, validate_checksums
)
from .util import parse_version
from ..provenance import MetadataInfo
from rachis.core.archive import Archiver
from rachis.sdk import PluginManager


@dataclass
class Config():
    '''
    Dataclass that stores user-selected configuration options.

    Attributes
    ----------
    perform_checksum_validation : bool
        Whether to opt in or out of checksum validation.
    parse_study_metadata : bool
        Whether to parse study metadata stored in provenance.
    recurse : bool
        Whether to recursively parse nested directories that contain artifacts.
    verbose : bool
        Whether to print status messages to stdout during processing.
    '''
    perform_checksum_validation: bool = True
    parse_study_metadata: bool = True
    recurse: bool = False
    verbose: bool = False


@dataclass
class ParserResults():
    '''
    Results generated and returned by a ParserVx.

    Attributes
    ----------
    parsed_artifact_uuids : set of str
        The uuids of the artifacts directly parsed by a parser. Does not
        include the uuids of artifact parsed from provenance. When parsing
        a single archive this is a single member set of that uuid. When
        parsing a directory, it is the set of all artifact uuids in that
        directory.
    prov_digraph : nx.Digraph
        The directed acyclic graph representation of the parsed provenance as
        an nx.DiGraph object.
    provenance_is_valid : ValidationCode
        A flag indicating the level of checksum validation.
    checksum_diff : ChecksumDiff or None
        A tuple of three dictionaries indicating the uuids of files that have
        been 1) added 2) removed or 3) changed in the archive since the
        archive was checksummed.
        None if no checksum validation was perfomed, e.g. when opted out or
        impossible because archive version did not support checksums, or when
        checksums.md5 missing from archive where it was expected.
        Interpretable only in conjunction with provenance_is_valid.
    '''
    parsed_artifact_uuids: Set[str]
    prov_digraph: nx.DiGraph
    provenance_is_valid: ValidationCode
    checksum_diff: Optional[ChecksumDiff]


class ProvNode:
    '''
    One node of a provenance DAG, describing one Rachis Result.
    '''

    @property
    def type(self) -> str:
        return self._result_md.type

    @property
    def format(self) -> Optional[str]:
        return self._result_md.format

    @property
    def archive_version(self) -> str:
        return self._archive_version

    @property
    def framework_version(self) -> str:
        return self._framework_version

    @property
    def has_provenance(self) -> bool:
        if '.' in self.archive_version:
            return float(self.archive_version) >= 7.0
        else:
            return int(self.archive_version) > 1

    @property
    def citations(self) -> Dict:
        citations = {}
        if hasattr(self, '_citations'):
            citations = self._citations.citations
        return citations

    @property
    def metadata(self) -> Optional[Dict[str, pd.DataFrame]]:
        '''
        A dict containing {parameter_name: metadata_dataframe} pairs where
        parameter_name is the registered name of the parameter the Metadata
        or MetadataColumn was passed to.

        Returns an empty dict if this action takes no Metadata or
        MetadataColumn.

        Returns None if this action has no metadata because the archive has no
        provenance, or the user opted out of metadata parsing.
        '''
        self._metadata: Optional[Dict[str, pd.DataFrame]]

        md = None
        if hasattr(self, '_metadata'):
            md = self._metadata
        return md

    @property
    def _parents(self) -> Optional[List[Dict[str, str]]]:
        '''
        A list of single-item {Type: UUID} dicts describing this
        action's inputs, including Artifacts passed as Metadata parameters.

        Returns [] if this action is an Import.

        NOTE: This property is private because it is slightly unsafe,
        reporting original node IDs that are not updated if the user renames
        nodes using the networkx API instead of ProvDAG.relabel_nodes.
        ProvDAG and its extensions should use the networkx.DiGraph itself to
        work with ancestry when possible.
        '''
        if not self.has_provenance:
            return None

        inputs = self.action._action_details.get('inputs')
        parents = []
        if inputs is not None:
            # Inputs are a list of single-item dicts
            for input in inputs:
                (name, value), = input.items()
                # value is usually a uuid, but may be a collection of uuids
                # the following are specced in rachis/core/type/collection
                if type(value) in (set, list, tuple):
                    for i in range(len(value)):
                        # Make these unique in case the single-item dicts get
                        # merged into a single dict downstream
                        if type(value[i]) is dict:
                            unq_name, = value[i].keys()
                            v, = value[i].values()
                        else:
                            unq_name = f'{name}_{i}'
                            v = value[i]
                        parents.append({unq_name: v})
                elif value is not None:
                    parents.append({name: value})
                else:
                    # skip None-by-default optional inputs
                    pass

        return parents + self._artifacts_passed_as_md

    def __init__(
        self,
        cfg: Config,
        archiver: Archiver,
        *args,
        archive_version: str | None = None,
        framework_version: str | None = None,
        uuid: str | None = None
    ):
        '''
        Constructs a ProvNode from an Archiver.

        Parameters
        ----------
        cfg : Config
            A dataclass that stores four boolean flags: whether to perform
            checksum validation, whether to parse study metadata, whether to
            recursively parse nested directories, and whether to enable verbose
            mode.
        archiver : Archiver
            The Archiver representing the Result we are parsing or its parent.
        archive_version : str | None
            The archive version of Archiver we are parsing.
        framework_version : str | None
            The framework version used to create the Archiver we are parsing.
        uuid : str | None
            None if we are parsing the root Archiver. The uuid of the Artifact
            we are parsing within provenance if set
        '''
        self.cfg = cfg
        self._archiver = archiver

        # TODO: Maybe make sure both are set or both are None?
        if archive_version is None or framework_version is None:
            archive_version, framework_version = parse_version(archiver, uuid)

        self._archive_version = archive_version
        _archive_version = float(archive_version)
        self._framework_version = framework_version

        #  Set up the base path we are looking under for files
        self._uuid = uuid if uuid else str(archiver.uuid)
        if uuid is None:
            base_path = archiver.path
        else:
            base_path = \
                archiver.path / 'provenance' / 'artifacts' / self._uuid

        # Parse the action.yaml
        if _archive_version >= 2:
            if uuid:
                action_path = base_path / 'action' / 'action.yaml'
            else:
                action_path = \
                    base_path / 'provenance' / 'action' / 'action.yaml'
            self.action = _Action(action_path)

        # Parse the root metadata
        metadata_path = base_path / 'metadata.yaml'
        self._result_md = _ResultMetadata(metadata_path)

        # Parse the citations
        if _archive_version >= 4:
            if uuid:
                citation_path = base_path / 'citations.bib'
            else:
                citation_path = base_path / 'provenance' / 'citations.bib'
            self._citations = _Citations(citation_path)

        if self.has_provenance:
            all_metadata_fps, self._artifacts_passed_as_md = \
                self._get_metadata_from_Action()
            if cfg.parse_study_metadata:
                self._metadata = self._parse_metadata(all_metadata_fps)

    @property
    def _action_present_(self):
        '''
        This property can only be accessed after a PluginManager has been
        instantiated which is why it isn't initialized with the object
        '''
        if not hasattr(self, '_action_present'):
            pm = PluginManager.reuse_existing()

            plugin_obj = pm._plugin_by_id.get(self.action.plugin)
            if plugin_obj:
                action_obj = plugin_obj.actions.get(self.action.action_name)
                if action_obj:
                    self._action_present = True
                else:
                    self._action_present = False
            else:
                self._action_present = False

        return self._action_present

    def _get_metadata_from_Action(
            self) -> Tuple[Dict[str, str], List[Dict[str, str]]]:
        '''
        Gathers data related to Metadata and MetadataColumn-based metadata
        files from the parsed action.yaml file.

        Captures filepath and parameter-name data for all study metadata
        files, so that these can be located for parsing, and then associated
        with the correct parameters during replay. It captures uuids for all
        artifacts passed to this action as metadata so they can be included as
        parents of this node.

        Returns
        -------
        tuple of (all_metadata, artifacts_as_metadata)
            Where all_metadata is a dict of
            {parameter_name: filename}.
            Where artifacts_as_metadata is a list of single-items dict of the
            structure {'artifact_passed_as_metadata': <uuid>}.

        Notes
        -----
        When Artifacts are passed as Metadata, they are captured in
        action['parameters'], rather than in action['inputs'] with the other
        Artifacts. Semantic Type data is thus not captured. This function
        returns a filler 'Type' for all UUIDs discovered here:
        'artifact_passed_as_metadata'. Because Artifacts passed (viewed) as
        Metadata retain their provenance, downstream Artifacts are linked to
        their real parent Artifact nodes with the proper Type information.
        '''
        all_metadata = dict()
        artifacts_as_metadata = []
        if (all_params :=
                self.action._action_details.get('parameters')) is not None:
            for param in all_params:
                param_val, = param.values()
                if isinstance(param_val, MetadataInfo):
                    param_name, = param.keys()
                    md_fp = param_val.relative_fp
                    all_metadata.update({param_name: md_fp})

                    artifacts_as_metadata += [
                        {'artifact_passed_as_metadata': uuid} for uuid in
                        param_val.input_artifact_uuids
                    ]

        return all_metadata, artifacts_as_metadata

    def _parse_metadata(
        self, metadata_fps: Dict[str, str]
    ) -> Dict[str, pd.DataFrame]:
        '''
        Parses all metadata files captured from Metadata and MetadataColumns
        (identifiable by !metadata tags) into pd.DataFrames.

        Parameters
        ----------
        metadata_fps : dict
            A dict of parameter names to metadata filenames for metadata
            parameters.

        Returns
        -------
        dict
            A dict of parameter names to dataframe objects that is loaded from
            the corresponding metadata file.

            An empty dict if there is no metadata.
        '''
        if metadata_fps == {}:
            return {}

        pfx = self._archiver.path / 'provenance'
        if str(self._archiver.uuid) == self._uuid:
            pfx = pfx / 'action'
        else:
            pfx = pfx / 'artifacts' / self._uuid / 'action'

        all_md = dict()
        for param_name in metadata_fps:
            filepath = pfx / metadata_fps[param_name]
            with filepath.open() as stream:
                try:
                    df = pd.read_csv(stream, sep='\t')
                except pd.errors.EmptyDataError:
                    df = pd.DataFrame()
            all_md[param_name] = df

        return all_md

    def __repr__(self) -> str:
        return repr(self._result_md)

    __str__ = __repr__

    def __hash__(self) -> int:
        return hash(self._uuid)

    def __eq__(self, other) -> bool:
        return (
            self.__class__ == other.__class__ and self._uuid == other._uuid
        )


class _Action:
    '''Provenance data from action.yaml for a single Rachis Result.'''

    @property
    def action_id(self) -> str:
        '''The UUID of the Action itself.'''
        return self._execution_details['uuid']

    @property
    def action_type(self) -> str:
        '''
        The type of Action represented e.g. Method, Pipeline, et al.
        '''
        return self._action_details['type']

    @property
    def runtime(self) -> timedelta:
        '''The elapsed run time of the Action, as a datetime object.'''
        end = self._execution_details['runtime']['end']
        start = self._execution_details['runtime']['start']
        return end - start

    @property
    def runtime_str(self) -> str:
        ''' The elapsed run time of the Action in seconds and microseconds.'''
        return self._execution_details['runtime']['duration']

    @property
    def action_name(self) -> str:
        '''
        The name of the action itself. Imports return 'import'.
        '''
        if self.action_type == 'import':
            return 'import'
        return self._action_details.get('action')

    @property
    def plugin(self) -> str:
        '''
        The plugin which executed this Action. Returns 'framework' if this is
        an import.
        '''
        if self.action_type == 'import':
            return 'framework'

        plugin = self._action_details.get('plugin')
        return plugin.replace('-', '_')

    @property
    def inputs(self) -> dict:
        '''
        Creates a dict of artifact inputs to this action.

        Returns
        -------
        dict
            A mapping of input name to the data type passed for that input
            (either uuid, list of uuid, or dict), see below for details.

        Notes
        -----
        One of three structures may be encountered when parsing this section of
        action.yaml, described below:

        case 1:

            inputs:
            - some_input_name: some_uuid
            - some_other_input_name: some_other_uuid
            (...)

        case 2:

            inputs:
            - some_input_name:
                - some_uuid
                - some_other_uuid
            (...)

        case 3 (result collection):

            inputs:
            - result_collection_name:
                - some_key: some_uuid
                - some_other_key: some_other_uuid
            (...)

            and thus is a different structure entirely.
        '''
        inputs = self._action_details.get('inputs')
        results = {}
        if inputs is not None:
            for input_ in inputs:
                nest_lvl_1 = next(iter(input_.values()))
                if type(nest_lvl_1) is list and type(nest_lvl_1[0]) is dict:
                    # result collection
                    rc = {}
                    for member in nest_lvl_1:
                        rc.update(member)

                    input_name = next(iter(input_))
                    results.update({input_name: rc})
                else:
                    # not result collection
                    results.update(input_)

        return results

    @property
    def input_result_collections(self):
        '''
        Collects all result collections passed as inputs (if any). Used for
        constructing the result collection namespace.

        Returns
        -------
        list of str
            A list of the names of the result collections passed as input.
            The names are as registered in the method registration.
        '''
        result_collection_names = []
        for key, value in self.inputs:
            if type(value) is dict:
                result_collection_names.append(key)

        return result_collection_names

    @property
    def parameters(self) -> dict:
        '''Returns a dict of parameters passed to this action.'''
        params = self._action_details.get('parameters')
        results = {}
        if params is not None:
            for item in params:
                results.update(item.items())
        return results

    @property
    def output_name(self) -> Optional[str]:
        '''
        Gets the output name of the node.

        Returns
        -------
        str or None
            The name of the output as parsed from action.yaml, or None if there
            is no output-name section.
        '''
        output_name = self._action_details.get('output-name')

        if type(output_name) is list:
            output_name = output_name[0]

        return output_name

    @property
    def result_collection_key(self) -> Optional[str]:
        '''
        Gets the result collection key if the artifact is part of a result
        collection.

        Returns
        -------
        str
            The result collection key if the artifact was output as part of
            a result collection, none otherwise.

        Notes
        -----
        We know if the artifact comes from a ResultCollection because
        outputs from a ResultCollection look like:

        output-name:
        - output
        - key
        - position/total positions
        '''
        output_name = self._action_details.get('output-name')

        if type(output_name) is not list:
            return None

        return output_name[1]

    @property
    def format(self) -> Optional[str]:
        '''Returns this action's format field if any.'''
        return self._action_details.get('format')

    @property
    def transformers(self) -> Optional[Dict]:
        '''Returns this action's transformers dictionary if any.'''
        return self._action_dict.get('transformers')

    def __init__(self, fp: str):
        from rachis.core.archive.provenance import load_action_stream
        source = fp.open() if hasattr(fp, 'open') else open(fp)
        with source as action_fh:
            self._action_dict = load_action_stream(action_fh)

        self._action_details = self._action_dict['action']
        self._execution_details = self._action_dict['execution']

    def __repr__(self):
        return (
            f'_Action(action_id={self.action_id}, type={self.action_type},'
            f' plugin={self.plugin}, action={self.action_name})'
        )


class _Citations:
    '''
    Citations for a single Rachis Result, as a dict of citation dicts keyed
    on the citation's bibtex ID.
    '''

    def __init__(self, fp: str):
        with (fp.open() if hasattr(fp, 'open') else open(fp)) as fh:
            bib_db = bp.loads(fh.read())
        self.citations = bib_db.get_entry_dict()

    def __repr__(self):
        keys = list(self.citations.keys())
        return f'Citations({keys})'


class _ResultMetadata:
    '''Basic metadata about a single Rachis Result from metadata.yaml.'''

    def __init__(self, md_fp: str):
        source = md_fp.open() if hasattr(md_fp, 'open') else open(md_fp)
        with source as md_fh:
            _md_dict = yaml.safe_load(md_fh)
        self.uuid = _md_dict['uuid']
        self.type = _md_dict['type']
        self.format = _md_dict['format']

    def __repr__(self):
        return (
            f'UUID:\t\t{self.uuid}\n'
            f'Type:\t\t{self.type}\n'
            f'Data Format:\t{self.format}'
        )


class Parser(metaclass=abc.ABCMeta):
    @classmethod
    @abc.abstractmethod
    def get_parser(cls, artifact_data: Any) -> 'Parser':
        '''
        Return the appropriate Parser.

        As of time of writing only really needed for ArchiveParser because it
        must dispatch different parsers based on Archive version
        '''

    @abc.abstractmethod
    def parse_prov(self, cfg: Config, data: Any) -> ParserResults:
        '''
        Parse provenance to return a ParserResults.
        '''


class ArchiveParser(Parser):
    @classmethod
    def get_parser(cls, artifact_data: Archiver):
        # NOTE: I would love to set result, archive_version, and
        # framework_version as instance state here; however, to maintain the
        # legacy API, I am not
        archive_version, _ = parse_version(artifact_data)

        if archive_version in FORMAT_REGISTRY:
            return FORMAT_REGISTRY[archive_version]()

        # Minor versions should more or less support future minor versions
        if '.' in archive_version:
            major, minor = archive_version.split('.')
            minor = int(minor)

            for minor_version in range(minor, -1, -1):
                ver = f'{major}.{minor_version}'
                if ver in FORMAT_REGISTRY:
                    return FORMAT_REGISTRY[ver]()

        raise KeyError('No matching parser found for version: '
                       f'{archive_version}')

    def _digraph_from_archive_contents(
        self, archive_contents: Dict[str, 'ProvNode']
    ) -> nx.DiGraph:
        '''
        Builds a networkx.DiGraph from a {UUID: ProvNode} dictionary.

        1. Create an empty nx.digraph.
        2. Gather nodes and their required attributes and add them to the
           DiGraph.
        3. Add edges to graph (including all !no-provenance nodes)
        4. Create guaranteed node attributes for these no-provenance nodes,
           which wouldn't otherwise have them.

        Parameters
        ----------
        archive_contents : dict of {str to ProvNode}
            A dictionary of node uuids to their representative ProvNode
            objects.

        Returns
        -------
        nx.DiGraph
            The directed, acyclic graph representation of the provenance of
            the archive. Edge directionality is from parent to child. Parents
            may have multiple children and children may have multiple parents.
        '''
        dag = nx.DiGraph()
        nodes = []
        for node_uuid, node in archive_contents.items():
            node_info = {
                'node_data': node,
                'has_provenance': node.has_provenance
            }
            nodes.append((node_uuid,  node_info))
        dag.add_nodes_from(nodes)

        edges = []
        for node_uuid, attrs in dag.nodes(data=True):
            if parents := attrs['node_data']._parents:
                for parent in parents:
                    parent_uuid, = parent.values()
                    edges.append((parent_uuid, node_uuid))
        dag.add_edges_from(edges)

        return dag

    def parse_prov(cls, cfg: Config, data: Any) -> ParserResults:
        raise NotImplementedError(
            'Use a subclass that usefully defines parse_prov for some format.'
        )


class ParserV0(ArchiveParser):
    '''
    Parser for V0 archives. V0 archives have no ancestral provenance.
    '''
    def parse_prov(self, cfg: Config, archiver: Archiver):
        '''
        Parses an artifact's provenance into a directed acyclic graph.

        In the case of v0 archives, the only provenance information is that
        which is attached to the artifact itself; information about ancestor
        nodes does not exist. The parsed dag contains only a single node.

        In the case of v1 archives, ancestor nodes do exist in the
        archive. However, because the corresponding action.yaml does not track
        output names, when two outputs share the same semantic type, it is not
        possible to untangle provenance. Instead of wrangling with this and
        in consideration of the expected rarity of v1 archives, it was decided
        to treat v1 archives as v0 archives.

        Parameters
        ----------
        cfg : Config
            A dataclass that stores four boolean flags: whether to perform
            checksum validation, whether to parse study metadata, whether to
            recursively parse nested directories, and whether to enable verbose
            mode.
        archiver : Archiver
            The Archiver representing the Result we are parsing.

        Returns
        -------
        ParserResults
            A dataclass that stores the parsed artifact uuids, the parsed
            networkx graph, the provenance-is-valid flag, and the
            checksum diff.
        '''
        uuid = str(archiver.uuid)
        if cfg.perform_checksum_validation:
            provenance_is_valid, checksum_diff = \
                self._validate_checksums(archiver)
        else:
            provenance_is_valid = ValidationCode.VALIDATION_OPTOUT
            checksum_diff = None

        warnings.warn(
            f'Artifact {uuid} was created prior to provenance '
            'tracking. Provenance data will be incomplete.',
            UserWarning
        )

        archive_version, framework_version = parse_version(archiver)

        nodes = {
            uuid: ProvNode(
                cfg, archiver, archive_version=archive_version,
                framework_version=framework_version)
        }
        graph = self._digraph_from_archive_contents(nodes)

        return ParserResults(
            {uuid},
            graph,
            provenance_is_valid,
            checksum_diff
        )

    def _validate_checksums(
            self, archiver: Archiver
    ) -> Tuple[ValidationCode, Optional[ChecksumDiff]]:
        '''
        Return the ValidationCode and ChecksumDiff for an archive. Because
        checksums were not introduced, until ArchiveFormat version 5,
        uses the PREDATES_CHECKSUMS flag and returns None to indicate that
        checksum diffing was not performed.

        Parameters
        ----------
        archiver : Archiver
            The Archiver we are validating. Ignored here but
            needed in signature for inheritance.

        Returns
        -------
        tuple of (ValidationCode, None)
            The validation code and None to indicate missing ChecksumDiff.
        '''
        return (ValidationCode.PREDATES_CHECKSUMS, None)


class ParserV1(ParserV0):
    '''
    Parser for V1 archives. Although action.yaml was introduced for this
    archive version, we are pretending that it was introduced in V2 because of
    difficulties untangling provenance without output names. V1 archives are
    treated as having no provenance, like V0 archives.
    '''
    pass


class ParserV2(ParserV1):
    '''
    Parser for V2 archives. Introduces action/action.yaml to provenance.
    Directory structure identical to V1, action.yaml changes to support
    Pipelines.
    '''
    def parse_prov(self, cfg: Config, archiver: Archiver):
        '''
        Parses an artifact's provenance into a directed acyclic graph.

        For each artifact in provenance, gathers all corresponding
        provenance-relevant files and constructs a ProvNode. Once all
        ProvNodes are constructed, creates the provenance graph.

        Parameters
        ----------
        cfg : Config
            A dataclass that stores four boolean flags: whether to perform
            checksum validation, whether to parse study metadata, whether to
            recursively parse nested directories, and whether to enable verbose
            mode.
        archiver : Archiver
            The Archiver representing the Result we are parsing.

        Returns
        -------
        ParserResults
            A dataclass that stores the parsed artifact uuids, the parsed
            networkx graph, the provenance-is-valid flag, and the
            checksum diff.
        '''
        uuid = str(archiver.uuid)
        if cfg.perform_checksum_validation:
            provenance_is_valid, checksum_diff = \
                self._validate_checksums(archiver)
        else:
            provenance_is_valid = ValidationCode.VALIDATION_OPTOUT
            checksum_diff = None

        archive_version, framework_version = parse_version(archiver)

        # make a provnode for each UUID
        archive_contents = {
            uuid: ProvNode(
                cfg, archiver, archive_version=archive_version,
                framework_version=framework_version)
        }

        # If this is the Result of an import, or an Action with no inputs,
        # it won't have this dir.
        ancestors = archiver.provenance_dir / 'artifacts'
        if ancestors.exists():
            for fp in ancestors.iterdir():
                node_uuid = fp.name

                if node_uuid in archive_contents:
                    continue

                archive_version, _ = parse_version(archiver, node_uuid)
                archive_contents[node_uuid] = ProvNode(
                    cfg, archiver, archive_version=archive_version,
                    framework_version=framework_version, uuid=node_uuid
                )

        graph = self._digraph_from_archive_contents(archive_contents)

        return ParserResults(
            {uuid},
            graph,
            provenance_is_valid,
            checksum_diff
        )


class ParserV3(ParserV2):
    '''
    Parser for V3 archives. Directory structure identical to V1 & V2,
    action.yaml now supports variadic inputs, so !set tags in action.yaml.
    '''
    pass


class ParserV4(ParserV3):
    '''
    Parser for V4 archives. Adds citations to directory structure, changes to
    action.yaml including transformers.
    '''
    pass


class ParserV5(ParserV4):
    '''
    Parser for V5 archives. Adds checksum validation with checksums.md5.
    '''
    def _validate_checksums(
            self, archiver: Archiver
    ) -> Tuple[ValidationCode, Optional[ChecksumDiff]]:
        '''
        Checksum support added for v5, so perform checksum validation.

        Parameters
        ----------
        archiver : Archiver
            The Archiver we are validating.

        Returns
        -------
        tuple of (ValidationCode, ChecksumDiff or None)
            Where ValidationCode is one of valid, invalid, predates checksums,
            optout.
            Where ChecksumDiff contains filepaths of all changed, added, and
            removed files since last checksumming.
            If checksums.md5 is missing from archive the archive, an invalid
            code is returned and a ChecksumDiff of None is returned.

        Notes
        -----
        Because a ChecksumDiff of None here has a different interpetation
        than in pre-V5 archive parsers, the ChecksumDiff should only be
        intepreted in conjuction with the ValidationCode.
        '''
        return validate_checksums(archiver)


class ParserV6(ParserV5):
    '''
    Parser for V6 archives. Adds support for output collections, adds
    execution_context field to action.yaml.
    '''
    pass


class ParserV7(ParserV6):
    '''Parser for V7 archives.

    New Features
    ------------
    - CPU flags under `action.yaml`
    - Total size of all files in `data` directory under `metadata.yaml`
    - A new `conda-env.yaml` file that contains a list of all dependencies
      in a user's current environment

    Notes
    -----
    The `annotations` directory has been excluded from
    the parser's view since this is essentially an optional output.
    Please see `core -> archive -> format -> v7_0`
    for more details on Annotations.

    '''
    pass


FORMAT_REGISTRY = {
    # NOTE: update for new format versions in rachis.core.archive.Archiver
    '0': ParserV0,
    '1': ParserV1,
    '2': ParserV2,
    '3': ParserV3,
    '4': ParserV4,
    '5': ParserV5,
    '6': ParserV6,
    '7.0': ParserV7,
    '7.1': ParserV7
}
