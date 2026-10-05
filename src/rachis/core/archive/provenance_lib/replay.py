# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------
import bibtexparser as bp
from bibtexparser.bwriter import BibTexWriter
import networkx as nx
import os
import pathlib
import importlib.resources
import shutil
import tempfile
from uuid import uuid4
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple, Union

from .archive_parser import ProvNode
from .parse import ProvDAG
from .usage_drivers import build_header, build_footer
from ..provenance import MetadataInfo

from rachis.sdk import PluginManager
from rachis.sdk.usage import Usage, UsageVariable
from rachis.sdk.util import camel_to_snake


@dataclass
class ReplayConfig:
    '''
    Dataclass that stores various user-selected configuration options and
    other bits of information relevant to provenance replay.

    Parameters
    ----------
    use : Usage
        The usage driver to be used for provenance replay.
    dump_recorded_metadata : bool
        If True, replay should write the metadata recorded in provenance to
        disk in .tsv format.
    use_recorded_metadata : bool
        If True, replay should use the metadata recorded in provenance.
    md_context_has_been_printed : bool
        A flag set by default and used internally, allows context to be
        printed once and only once.
    no_provenance_context_has_been_printed : bool
        Indicates whether the no-provenance context documentation has been
        printed.
    header : bool
        If True, an introductory how-to header should be rendered in the
        script.
    verbose : bool
        If True, progress is reported to stdout.
    md_out_dir : str
        The directory where captured metadata should be written.
    '''
    def __init__(
        self,
        use: Usage,
        dump_recorded_metadata: bool = True,
        use_recorded_metadata: bool = False,
        md_context_has_been_printed: bool = False,
        no_provenance_context_has_been_printed: bool = False,
        header: bool = True,
        verbose: bool = False,
        md_out_dir: str = ''
    ):
        self.use = use
        self.dump_recorded_metadata = dump_recorded_metadata
        self.use_recorded_metadata = use_recorded_metadata
        self.md_context_has_been_printed = md_context_has_been_printed
        self.no_provenance_context_has_been_printed = \
            no_provenance_context_has_been_printed
        self.header = header
        self.verbose = verbose
        self.md_out_dir = md_out_dir
        self.pm = PluginManager.reuse_existing()


@dataclass
class ActionCollections():
    '''
    std_actions are all normal provenance-tracked q2 actions, arranged as:

    {
        <action_id>: {
            <output_node_uuid>: 'output_name',
            <output_node_2_uuid:> 'output_name_2'
        },
        <action_2_id> : ...
     }

    no_provenance_nodes can't be organized by action, and in some cases we
    don't know anything but UUID for them, so we can fit these in a list.
    '''
    std_actions: Dict[str, Dict[str, str]] = field(default_factory=dict)
    no_provenance_nodes: List[str] = field(default_factory=list)


@dataclass
class UsageVariableRecord:
    name: str
    variable: UsageVariable = None


@dataclass
class ResultCollectionRecord:
    collection_uuid: str
    members: Dict[str, str]


class ReplayNamespaces:
    '''
    A dataclass collection of objects that each track some useful bit of
    information relevant to replay/usage namespaces.

    Attributes
    ----------
    _usg_var_ns : dict
        The central usage variable namespace that ensures no namespace clashes.
        Maps artifact uuid to `UsageVariableRecord`.
    _action_ns : set of str
        A collection of unique action strings that look like
        `{plugin}_{action}_{sequential int}`.
    result_collection_ns : dict
        Used to keep track of result collection members during usage rendering.
        Structure is as follows:

        {
            action-id: {
                output-name: {
                    'collection_uuid': uuid,
                    'artifacts': {
                        uuid: key-in-collection,
                        (...),
                    }
                },
                (...),
            }
        }

        where the action-id and the output-name uniquely identify a result
        collection that came from some action, the `collection_uuid` key stores
        a uuid for the entire collection needed for querying the
        `usg_var_namespace`, and the `artifacts` key stores all result
        collection members along with their keys so they can be accessed
        properly.
    '''
    def __init__(self, dag=None):
        self._usg_var_ns = {}
        self._action_ns = set()
        if dag:
            self.result_collection_ns = \
                self.make_result_collection_namespace(dag)
            self.artifact_uuid_to_rc_uuid, self.rc_contents_to_rc_uuid = \
                self.make_result_collection_mappings()
        else:
            self.result_collection_ns = {}
            self.artifact_uuid_to_rc_uuid = {}
            self.rc_contents_to_rc_uuid = {}

    def add_usg_var_record(self, uuid, name, variable=None):
        '''
        Given a uuid, name, and optionally a usage variable, create a usage
        variable record and add it to the namespace.

        Parameters
        ----------
        uuid : str
            The uuid of the the artifact or result collection.
        name : str
            The not-yet-unique name of the artifact or result collection.
        variable : UsageVariable or None
            The optional UsageVariable instance to add to the record.

        Returns
        -------
        str
            The now-unique name of the artifact or result collection.
        '''
        unique_name = self._make_unique_name(name)

        self._usg_var_ns[uuid] = UsageVariableRecord(unique_name, variable)

        return unique_name

    def update_usg_var_record(self, uuid, variable):
        '''
        Given a uuid update the record to contain the passed usage variable.
        The record is assumed to already be present in the namespace.

        Parameters
        ----------
        uuid : str
            The uuid of the artifact or result collection for which to update
            the usage variable instance.
        variable : UsageVariable
            The usage variable to add to the record.
        '''
        self._usg_var_ns[uuid].variable = variable

    def get_usg_var_record(self, uuid):
        '''
        Given a uuid, return the corresponding usage variable record, or none
        if the uuid is not in the namespace.

        Parameters
        ----------
        uuid : str
            The uuid of the artifact or result collection for which to return
            the record.

        Returns
        -------
        UsageVariableRecord or None
            The record if the uuid was found, otherwise None.
        '''
        try:
            return self._usg_var_ns[uuid]
        except KeyError:
            return None

    def get_usg_var_uuid(self, name: str) -> str:
        '''
        Given a usage variable name, return its uuid, or raise KeyError if the
        name is not in the namespace.

        Parameters
        ----------
        name : str
            The name of the usage variable record of interest.

        Returns
        -------
        str
            The corresponding uuid of the record.

        Raises
        ------
        KeyError
            If the name is not found in the namespace.
        '''
        for uuid, record in self._usg_var_ns.items():
            if name == record.name:
                return uuid

        raise KeyError(
            f'The queried name \'{name}\' does not exist in the namespace.'
        )

    def _make_unique_name(self, name: str) -> str:
        '''
        Appends `_<some int>` to name, such that the returned name won't
        collide with any variable names that already exist in `usg_var_ns`.

        Parameters
        ----------
        name : str
            The variable name to make unique.

        Returns
        -------
        str
            The unique integer-appended variable name.
        '''
        counter = 0
        unique_name = f'{name}_{counter}'
        names = [record.name for record in self._usg_var_ns.values()]

        # no-provenance nodes are stored with angle brackets around them
        while unique_name in names or f'<{unique_name}>' in names:
            counter += 1
            unique_name = f'{name}_{counter}'

        return unique_name

    def make_result_collection_namespace(self, dag: nx.digraph) -> dict:
        '''
        Constructs the result collections namespaces from the parsed digraph.

        Parameters
        ----------
        dag : nx.digraph
            The digraph representing the parsed provenance.

        Returns
        -------
        dict
            The result collection namespace.
        '''
        rc_ns = {}
        for node in dag:
            provnode = dag.get_node_data(node)
            rc_key = provnode.action.result_collection_key
            if rc_key:
                # output result collection
                action_id = provnode.action.action_id
                output_name = provnode.action.output_name
                if action_id not in rc_ns:
                    rc_ns[action_id] = {}
                if output_name not in rc_ns[action_id]:
                    artifacts = {rc_key: provnode._uuid}
                    rc_ns[action_id][output_name] = ResultCollectionRecord(
                        collection_uuid=str(uuid4()), members=artifacts
                    )
                else:
                    rc_ns[action_id][output_name].members[rc_key] = \
                        provnode._uuid

        return rc_ns

    def make_result_collection_mappings(self) -> Tuple[Dict]:
        '''
        Builds two mappings:
            - one from artifact uuid to a tuple of the uuid of the result
              collection of which it is a member and its key in the collection
            - one from the hash of the result collection contents (both with
              and without keys) to the uuid of the result collection

        Returns
        -------
        tuple of dict
            The two result collection mappings.
        '''
        a_to_c = {}  # artifact uuid -> collection uuid
        c_to_c = {}  # hash of collection contents -> collection uuid
        for action_id in self.result_collection_ns:
            for output_name in self.result_collection_ns[action_id]:
                record = self.result_collection_ns[action_id][output_name]
                for key, uuid in record.members.items():
                    a_to_c[uuid] = (record.collection_uuid, key)

                hashed_contents = self.hash_result_collection(record.members)
                hashed_contents_with_keys = \
                    self.hash_result_collection_with_keys(record.members)

                c_to_c[hashed_contents] = record.collection_uuid
                c_to_c[hashed_contents_with_keys] = record.collection_uuid

        return a_to_c, c_to_c

    def hash_result_collection_with_keys(self, members: Dict) -> int:
        '''
        Hashes the contents of a result collection. Useful for finding
        corresponding usage variables when rendering the replay of result
        collections. Order of the input result collection is not taken into
        account (the result collections are ordered alphabetically by key).

        Parameters
        ----------
        members : dict
            The contents of a result collection, looks like:

            {
                'a': some-uuid,
                'b': some-other-uuid,
                (...)
            }

        Returns
        -------
        int
            The hashed contents.
        '''
        sorted_members = {key: members[key] for key in sorted(members)}
        hashable_members_with_keys = tuple(
            (key, value) for key, value in sorted_members.items()
        )

        return hash(hashable_members_with_keys)

    def hash_result_collection(self, members: Union[Dict, List]) -> int:
        '''
        Hashes a list of uuids. Useful for finding corresponding result
        collections that may have been cast to list of uuids. If a dict is
        input it is first converted to a list of values (uuids).

        Parameters
        ----------
        members : dict or list
            The contents of a result collection, either as a dict or list.

        Returns
        -------
        int
            The hashed contents.
        '''
        if type(members) is dict:
            members = list(members.values())

        sorted_members = list(sorted(members))
        hashable_members = tuple(uuid for uuid in sorted_members)

        return hash(hashable_members)

    def add_rc_member_to_ns(self, uuid, name, use):
        '''
        Accesses a result collection member of interest and adds it the
        central usage variable namespace.

        Parameters
        ----------
        uuid : str
            The uuid of the artifact of interest.
        name : str
            The desired name of the to-be-made usage variable.
        use : Usage
            The currently executing usage driver.
        '''
        collection_uuid, key = self.artifact_uuid_to_rc_uuid[uuid]
        collection_var = self.get_usg_var_record(collection_uuid).variable

        var_name = self.add_usg_var_record(uuid, name)

        usg_var = use.get_artifact_collection_member(
            var_name, collection_var, key
        )

        self.update_usg_var_record(uuid, usg_var)

    def uniquify_action_name(self, plugin: str, action: str) -> str:
        '''
        Creates a unique name by concatenating plugin, action, and a counter,
        and adds this name to _action_ns before returning it.

        Parameters
        ----------
        plugin : str
            The name of the plugin.
        action : str
            The name of the action.
        action_namespace : set of str
            The collection of unqiue action names.

        Returns
        -------
        str
            The unique action name.
        '''
        counter = 0
        plg_action_name = f'{plugin}_{action}_{counter}'
        while plg_action_name in self._action_ns:
            counter += 1
            plg_action_name = f'{plugin}_{action}_{counter}'
        self._action_ns.add(plg_action_name)

        return plg_action_name


def replay_provenance(
    usage_driver: Usage,
    payload: Union[str, ProvDAG],
    out_fp: str,
    validate_checksums: bool = True,
    parse_metadata: bool = True,
    recurse: bool = False,
    use_recorded_metadata: bool = False,
    suppress_header: bool = False,
    verbose: bool = False,
    dump_recorded_metadata: bool = True,
    md_out_dir: str = ''
):
    '''
    Renders usage examples describing a ProvDAG, producing an interface-
    specific executable.

    ProvDAG inputs retain their original config values. The
    `validate_checksums`, `parse_metadata`, `recurse`, and `verbose` parameters
    are disregarded if the payload is a ProvDAG.

    Parameters
    ----------
    usage_driver : Usage
        The type of Usage driver to be used. Currently intended to be either
        `ReplayPythonUsage` or `ReplayCLIUsage`.
    payload : str or ProvDAG
        A filepath to an artifact or directory containing artifacts, or the
        ProvDAG to be parsed.
    out_fp : str
        The filepath at which to write the rendered executable.
    validate_checksums : bool
        Whether to perform checksum validation on the input artifact.
    parse_metadata : bool
        Whether to parse study metadata recorded in provenance.
    recurse : bool
        Whether to recursively parse nested directories containing artifacts.
    use_recorded_metadata : bool
        Whether to use the metadata recorded in provenance.
    suppress_header : bool
        Whether to forgo rendering the header and footer that are included
        by default in replay scripts.
    verbose : bool
        Whether to print status messages during processing.
    dump_recorded_metadata : bool
        Whether to write the metadata recorded in provenance to disk.
    md_out_dir : str
        The directory in which to write the recorded metadata if desired.
    '''
    if type(payload) is ProvDAG:
        parse_metadata = payload.cfg.parse_study_metadata

    if not parse_metadata:
        if use_recorded_metadata:
            raise ValueError(
                'Metadata not parsed for replay. Re-run with parse_metadata, '
                'or set use_recorded_metadata to False.'
            )
        if dump_recorded_metadata:
            raise ValueError(
                'Metadata not parsed, so cannot be written to disk. Re-run '
                'with parse_metadata, or set dump_recorded_metadata to False.'
            )
        if md_out_dir:
            raise ValueError(
                'Metadata not parsed, so cannot be written to disk. Re-run '
                'with parse_metadata, or do not pass a metadata output '
                'filepath argument.'
            )

    if use_recorded_metadata and not dump_recorded_metadata:
        raise NotImplementedError(
            'In order to produce a replay script that uses metadata '
            'captured in provenance, that metadata must first be written to '
            'disk. Re-run with dump-recorded-metadata set to True, or '
            'use-recorded-metadata set to False.'
        )

    dag = ProvDAG(
        payload, validate_checksums, parse_metadata, recurse, verbose
    )
    cfg = ReplayConfig(
        use=usage_driver(),
        use_recorded_metadata=use_recorded_metadata,
        dump_recorded_metadata=dump_recorded_metadata,
        verbose=verbose,
        md_out_dir=md_out_dir
    )

    ns = ReplayNamespaces(dag)

    build_usage_examples(dag, cfg, ns)
    if not suppress_header:
        cfg.use.build_header()
        cfg.use.build_footer(dag)

    if cfg.dump_recorded_metadata:
        print('metadata written to recorded_metadata/')

    output = cfg.use.render(flush=True)
    with open(out_fp, mode='w') as out_fh:
        out_fh.write(output)


def build_usage_examples(
    dag: ProvDAG, cfg: ReplayConfig, ns: ReplayNamespaces
):
    '''
    Builds a chained usage example representing the analysis `dag`.

    Parameters
    ----------
    dag : ProvDAG
       The dag representation of parsed provenance.
    cfg : ReplayConfig
        Replay configuration options.
    ns : ReplayNamespaces
        Info tracking usage and result collection namespaces.
    '''
    sorted_nodes = nx.topological_sort(dag.collapsed_view)
    actions = group_by_action(dag, sorted_nodes, ns)

    for node_id in actions.no_provenance_nodes:
        node = dag.get_node_data(node_id)
        build_no_provenance_node_usage(node, node_id, ns, cfg)

    for action_id in (std_actions := actions.std_actions):
        # we are replaying actions not nodes, so any associated node works
        try:
            some_node_id = next(iter(std_actions[action_id]))
            node = dag.get_node_data(some_node_id)
        except KeyError:
            # we have result collection
            some_output_name = next(iter(ns.result_collection_ns[action_id]))
            some_node_id = next(iter(
                ns.result_collection_ns[action_id][
                    some_output_name].members.values()
            ))
            node = dag.get_node_data(some_node_id)

        if node.action.action_type == 'import':
            build_import_usage(node, ns, cfg)
        elif node.format != 'report':
            build_action_usage(node, ns, std_actions, action_id, cfg)


def group_by_action(
    dag: ProvDAG, nodes: Iterator[str], ns: ReplayNamespaces
) -> ActionCollections:
    '''
    This groups the nodes from a DAG by action, returning an ActionCollections
    aggregating the outputs related to each action.

    Takes an iterator of UUIDs, allowing us to influence the ordering of the
    grouping.

    In cases where a captured output_name is unavailable, we substitute the
    output data's Semantic Type, snake-cased because it will be used as
    a variable name if this data is rendered by ArtifactAPIUsage.

    Parameters
    ----------
    dag : ProvDAG
        The dag representation of parsed provenance.
    nodes : iterator of str
        An iterator over node uuids.
    ns : ReplayNamespaces
        Info tracking usage and result collection namespaces.

    Returns
    -------
    ActionCollections
        The outputs grouped by action.
    '''
    actions = ActionCollections()
    for node_id in nodes:
        if dag.node_has_provenance(node_id):
            node = dag.get_node_data(node_id)
            action_id = node.action.action_id
            output_name = node.action.output_name
            if output_name is None:
                output_name = camel_to_snake(node.type)

            if node.action.result_collection_key:
                rc_record = ns.result_collection_ns[action_id][output_name]
                node_id = rc_record.collection_uuid

            if action_id not in actions.std_actions:
                actions.std_actions[action_id] = {node_id: output_name}
            else:
                # for result collections we overwrite this but don't care
                actions.std_actions[action_id][node_id] = output_name
        else:
            actions.no_provenance_nodes.append(node_id)

    return actions


def build_no_provenance_node_usage(
    node: Optional[ProvNode],
    uuid: str,
    ns: ReplayNamespaces,
    cfg: ReplayConfig
):
    '''
    Given a ProvNode (with no provenance), make sure comments will be rendered
    explaining this, add an empty usage variable to the namespace and log the
    node. Returns nothing, modifying the passed usage instance in place.

    Parameters
    ----------
    node : ProvNode or None
        Either a no-provenance node, or None indicating that only `uuid`
        is available.
    uuid : str
        The uuid of the node/result.
    ns : ReplayNamespaces
        Info tracking usage and result collection namespaces.
    cfg : ReplayConfig
        Replay configuration options. Contains the modified usage driver.
    '''
    if not cfg.no_provenance_context_has_been_printed:
        cfg.no_provenance_context_has_been_printed = True
        cfg.use.comment(
            'One or more nodes have no provenance, so full replay is '
            'impossible. Any commands we were able to reconstruct have been '
            'rendered, with the string descriptions below replacing actual '
            'inputs.'
        )
        cfg.use.comment(
            'Original Node ID                       String Description'
        )
    if node is None:
        # the node is a !no-provenance input and we have only UUID
        var_name = 'no-provenance-node'
    else:
        var_name = camel_to_snake(node.type)

    ns.add_usg_var_record(uuid, var_name)

    # make a usage variable for downstream consumption
    empty_var = cfg.use.usage_variable(
        ns.get_usg_var_record(uuid).name, lambda: None, 'artifact'
    )
    ns.update_usg_var_record(uuid, empty_var)

    # log the no-prov node
    usg_var = ns.get_usg_var_record(uuid).variable
    cfg.use.comment(f"{uuid}   {usg_var.to_interface_name()}")


def build_import_usage(
    node: ProvNode, ns: ReplayNamespaces, cfg: ReplayConfig
):
    '''
    Given a ProvNode, adds an import usage example for it, roughly
    resembling the below. Returns nothing, modifying the passed usage instance
    in place.

    raw_seqs = use.init_format('raw_seqs', lambda: None, ext='fastq.gz')
    imported_seqs = use.import_from_format(
        'emp_single_end_sequences',
        'EMPSingleEndSequences',
        raw_seqs
    )
    The `lambda: None` is a placeholder for some actual data factory,
    and should not impact the rendered usage.

    Parameters
    ----------
    node : ProvNode
        The imported node of interest.
    ns : ReplayNamespaces
        Info tracking usage and result collection namespaces.
    cfg : ReplayConfig
        Replay configuration options. Contains the modified usage driver.
    '''
    format_id = node._uuid + '_f'
    ns.add_usg_var_record(format_id, camel_to_snake(node.type) + '_f')

    format_for_import = cfg.use.init_format(
        ns.get_usg_var_record(format_id).name, lambda: None
    )

    var_name = ns.add_usg_var_record(node._uuid, camel_to_snake(node.type))

    use_var = cfg.use.import_from_format(
        var_name, node.type, format_for_import
    )
    ns.update_usg_var_record(node._uuid, use_var)


def build_action_usage(
    node: ProvNode,
    ns: ReplayNamespaces,
    std_actions: Dict[str, Dict[str, str]],
    action_id: str,
    cfg: ReplayConfig
):
    '''
    Adds an action usage example to `use` for some ProvNode.
    Returns nothing, modifying the passed usage instance in place.

    use.action(
        use.UsageAction(plugin_id='diversity_lib',
                        action_id='pielou_evenness'),
        use.UsageInputs(table=ft),
        use.UsageOutputNames(vector='pielou_vector')
    )

    Parameters
    ----------
    node : ProvNode
        The node the creating action of which is of interest.
    ns : ReplayNamespaces
        Info tracking usage and result collection namespaces.
    std_actions : dict
        Expalained in ActionCollections.
    action_id : str
        The uuid of the action.
    cfg : ReplayConfig
        Replay configuration options. Contains the modified usage driver.
    '''
    command_specific_md_context_has_been_printed = False
    plugin = node.action.plugin
    action = node.action.action_name
    plg_action_name = ns.uniquify_action_name(plugin, action)

    inputs = _collect_action_inputs(cfg.use, ns, node)

    # Process outputs before params so we can access the unique output name
    # from the namespace when dumping metadata to files below
    # NOTE: artifact collection members are not detected here; we don't care
    # because we don't need to name them until they are used individually as
    # inputs to another action
    raw_outputs = std_actions[action_id].items()
    outputs = _uniquify_output_names(ns, raw_outputs)

    for param_name, param_val in node.action.parameters.items():
        # We can currently assume that None arguments are only passed to params
        # as default values, so we can skip these parameters entirely in replay
        if param_val is None:
            continue

        if isinstance(param_val, MetadataInfo):
            # we only need this identifier to be unique; the rendered interface
            # name will not contain this identifier
            unique_md_id = node._uuid + '_' + param_name

            md_fn = ns.add_usg_var_record(
                unique_md_id, camel_to_snake(param_name)
            )
            if cfg.dump_recorded_metadata:
                md_with_ext = md_fn + '.tsv'
                dump_recorded_md_file(
                    cfg, node, plg_action_name, param_name, md_with_ext
                )

            if cfg.use_recorded_metadata:
                # the local dir and fp where md will be saved (if at all) is:
                md_fn = f'{plg_action_name}/{md_fn}'
                md = init_md_from_recorded_md(
                    node, param_name, unique_md_id, ns, cfg, md_fn
                )
            else:
                if not cfg.md_context_has_been_printed:
                    cfg.md_context_has_been_printed = True
                    cfg.use.comment(
                        "Replay attempts to represent metadata inputs "
                        "accurately, but metadata .tsv files are merged "
                        "automatically by some interfaces, rendering "
                        "distinctions between file inputs invisible in "
                        "provenance. We output the recorded metadata to disk "
                        "to enable visual inspection.")

                if not command_specific_md_context_has_been_printed:
                    if cfg.md_out_dir:
                        fp = f'{cfg.md_out_dir}/{plg_action_name}'
                    else:
                        fp = f'./recorded_metadata/{plg_action_name}/'

                    cfg.use.comment(
                        "The following command may have received additional "
                        "metadata .tsv files. To confirm you have covered "
                        "your metadata needs adequately, review the original "
                        f"metadata, saved at '{fp}'")

                if not param_val.input_artifact_uuids:
                    md = init_md_from_md_file(
                        node, param_name, unique_md_id, ns, cfg
                    )
                else:
                    md = init_md_from_artifacts(param_name, param_val, ns, cfg)

            param_val = md

        inputs.update({param_name: param_val})

    usg_var = cfg.use.action(
        cfg.use.UsageAction(
            plugin_id=plugin, action_id=action, node=node),
        cfg.use.UsageInputs(**inputs),
        cfg.use.UsageOutputNames(**outputs)
    )

    # add the usage variable(s) to the namespace
    for res in usg_var:
        uuid_key = ns.get_usg_var_uuid(res.name)
        ns.update_usg_var_record(uuid_key, res)


def _collect_action_inputs(
    use: Usage, ns: ReplayNamespaces, node: ProvNode
) -> dict:
    '''
    Returns a dict containing the action Inputs for a ProvNode.
    Dict structure: {input_name: input_var} or {input_name: [input_var1, ...]}.

    Parameters
    ----------
    use : Usage
        The currently executing usage driver.
    ns : ReplayNamespaces
        Info tracking usage and result collection namespaces.
    node : ProvNode
        The node the creating action of which's inputs are of interest.

    Returns
    -------
    dict
        Mapping input names to their corresponding usage variables.
    '''
    inputs_dict = {}
    for input_name, input_value in node.action.inputs.items():
        # Currently we can only have a None as a default value, so we can skip
        # this as it was not provided
        if input_value is None:
            continue

        # Received a single artifact
        if type(input_value) is str:
            if ns.get_usg_var_record(input_value) is None:
                ns.add_rc_member_to_ns(input_value, input_name, use)

            resolved_input = ns.get_usg_var_record(input_value).variable

        # Received a list of artifacts
        elif type(input_value) is list:
            # may be rc cast to list so search for equivalent rc
            # if not then follow algorithm for single str for each
            input_hash = ns.hash_result_collection(input_value)
            if collection_uuid := ns.rc_contents_to_rc_uuid.get(input_hash):
                # corresponding rc found
                resolved_input = ns.get_usg_var_record(
                    collection_uuid
                ).variable
            else:
                # find each artifact and assemble into a list
                input_list = []
                for input_value in input_value:
                    if ns.get_usg_var_record(input_value) is None:
                        ns.add_rc_member_to_ns(input_value, input_name, use)

                    input_list.append(
                        ns.get_usg_var_record(input_value).variable
                    )

                resolved_input = input_list

        # Received a dict of artifacts (ResultCollection)
        elif type(input_value) is dict:
            # search for equivalent rc if not found then create new rc
            rc = input_value
            input_hash = ns.hash_result_collection_with_keys(rc)
            if collection_uuid := ns.rc_contents_to_rc_uuid.get(input_hash):
                # corresponding rc found
                resolved_input = ns.get_usg_var_record(
                    collection_uuid
                ).variable
            else:
                # build new rc
                new_rc = {}
                for key, input_value in rc.items():
                    if ns.get_usg_var_record(input_value) is None:
                        ns.add_rc_member_to_ns(input_value, input_name, use)

                    new_rc[key] = ns.get_usg_var_record(input_value).variable

                # make new rc usg var
                new_collection_uuid = uuid4()
                var_name = ns.add_usg_var_record(
                    new_collection_uuid, input_name
                )
                usg_var = use.construct_artifact_collection(var_name, new_rc)
                ns.update_usg_var_record(new_collection_uuid, usg_var)
                resolved_input = ns.get_usg_var_record(
                    new_collection_uuid
                ).variable

        # If we ever mess with inputs again and add a new type here this should
        # trip otherwise we should never see it
        else:
            msg = f"Got a '{input_value}' as input which is of type" \
                  f" '{type(input_value)}'. Supported types are str, list," \
                  " and dict."
            raise ValueError(msg)

        inputs_dict[input_name] = resolved_input

    return inputs_dict


def _uniquify_output_names(
    ns: ReplayNamespaces, raw_outputs: dict
) -> dict:
    '''
    Returns a dict containing the uniquified output names from a ProvNode.
    Dict structure: {output_name: uniquified_output_name}.

    Parameters
    ----------
    ns : ReplayNamespaces
        Info tracking usage and result collection namespaces.
    raw_outputs : dict
        Mapping of node uuid to output-name as seen in action.yaml.

    Returns
    -------
    dict
        Mapping of original output-name to output-name after being made unique.
    '''
    outputs = {}
    for uuid, output_name in raw_outputs:
        var_name = ns.add_usg_var_record(uuid, output_name)
        outputs.update({output_name: var_name})

    return outputs


def init_md_from_recorded_md(
    node: ProvNode,
    param_name: str,
    md_id: str,
    ns: ReplayNamespaces,
    cfg: ReplayConfig,
    md_fn: str
) -> UsageVariable:
    '''
    Initializes and returns a Metadata UsageVariable from Metadata parsed
    from provenance.

    Parameters
    ----------
    node : ProvNode
        The node the creating action of which was passed metadata.
    param_name : str
        The name of the parameter to which metadata was passed.
    md_id : str
        Looks like: f'{node uuid}_{param name}'.
    ns : ReplayNamespaces
        Namespaces associated with provenance replay.
    cfg : ReplayConfig
        Replay configuration options. Contains the executing usage driver.
    md_fn : str
        Looks like: f'{plugin}_{action}_{counter}/{unique param name}.

    Returns
    -------
    UsageVariable
        Of type metadata or metadata column.

    Raises
    ------
    ValueError
        If the node has no metadata.
    '''
    if not node.metadata:
        raise ValueError(
            'This function should only be called if the node has metadata.'
        )

    md_df = node.metadata[param_name]

    def factory():
        from rachis import Metadata
        return Metadata(md_df)

    cwd = pathlib.Path.cwd()
    if cfg.md_out_dir:
        fn = str(cwd / cfg.md_out_dir / md_fn)
    else:
        fn = str(cwd / 'recorded_metadata' / md_fn)

    md = cfg.use.init_metadata(
        ns.get_usg_var_record(md_id).name, factory, dumped_md_fn=fn
    )
    plugin = node.action.plugin
    action = node.action.action_name

    if node._action_present_ and \
            param_is_metadata_column(cfg, param_name, plugin, action):
        mdc_id = node._uuid + '_mdc'
        mdc_name = ns.get_usg_var_record(md_id).name + '_mdc'
        var_name = ns.add_usg_var_record(mdc_id, mdc_name)
        md = cfg.use.get_metadata_column(var_name, '<column name>', md)

    return md


def init_md_from_md_file(
    node: ProvNode,
    param_name: str,
    md_id: str,
    ns: ReplayNamespaces,
    cfg: ReplayConfig
) -> UsageVariable:
    '''
    Initializes and returns a Metadata UsageVariable with no real data,
    mimicking a user passing md as a .tsv file.

    Parameters
    ----------
    node : ProvNode
        The node the creating action of which was passed a metadata file.
    param_name : str
        The parameter name to which the metadata file was passed.
    md_id : str
        Looks like: f'{node uuid}_{param name}'.
    ns : ReplayNamespaces
        Namespaces associated with provenance replay.
    cfg : ReplayConfig
        Replay configuration options. Contains the executing usage driver.

    Returns
    -------
    UsageVariable
        Of type metadata or metadata column.
    '''
    plugin = node.action.plugin
    action = node.action.action_name
    md = cfg.use.init_metadata(ns.get_usg_var_record(md_id).name, lambda: None)

    if node._action_present_ and \
            param_is_metadata_column(cfg, param_name, plugin, action):
        mdc_id = node._uuid + '_mdc'
        mdc_name = ns.get_usg_var_record(md_id).name + '_mdc'
        var_name = ns.add_usg_var_record(mdc_id, mdc_name)
        md = cfg.use.get_metadata_column(var_name, '<column name>', md)

    return md


def init_md_from_artifacts(
        param_name: str, md_inf: MetadataInfo, ns: ReplayNamespaces,
        cfg: ReplayConfig
) -> UsageVariable:
    '''
    Initializes and returns a Metadata UsageVariable with no real data,
    mimicking a user passing one or more QIIME 2 Artifacts as metadata.

    We expect these usage vars are already in the namespace as artifacts if
    we're reading them in as metadata.

    Parameters
    ----------
    param_name : str
        The name of this param in the action signature.
    md_inf : MetadataInfo
        Named tuple with fields `input_artifact_uuids` which is a list of
        uuids and `relative_fp` which is the filename of the metadata file.
        These are parsed from a !metadata tag in action.yaml.
    ns : ReplayNamespaces
        Info tracking usage and result collection namespaces.
    cfg: ReplayConfig
        Replay configuration options. Contains the executing usage driver.

    Returns
    -------
    UsageVariable
        Of type metadata.

    Raises
    ------
    ValueError
        If no input artifact uuids are present in MetadataInfo.
    '''
    if not md_inf.input_artifact_uuids:
        raise ValueError(
            'This funtion should not be used if '
            'MetadataInfo.input_artifact_uuids is empty.'
        )

    md_files_in = []
    for artifact_uuid in md_inf.input_artifact_uuids:
        amd_id = artifact_uuid + '_a'
        var = ns.get_usg_var_record(artifact_uuid)

        if var is None:
            # Our var was part of a ResultCollection. If a var is part of a
            # collection and is used as a metadata param like this, it is never
            # specifically added to the ns, so do it here
            ns.add_rc_member_to_ns(artifact_uuid, param_name, cfg.use)
            var = ns.get_usg_var_record(artifact_uuid)

        var_name = var.variable.name + '_a'

        if ns.get_usg_var_record(amd_id) is None:
            var_name = ns.add_usg_var_record(amd_id, var_name)
            art_as_md = cfg.use.view_as_metadata(
                var_name, ns.get_usg_var_record(artifact_uuid).variable
            )
            ns.update_usg_var_record(amd_id, art_as_md)
        else:
            art_as_md = ns.get_usg_var_record(amd_id).variable

        md_files_in.append(art_as_md)

    if len(md_inf.input_artifact_uuids) > 1:
        # we can't uniquify this normally, because one uuid can be merged with
        # combinations of others
        merge_id = '-'.join(md_inf.input_artifact_uuids)
        var_name = ns.add_usg_var_record(merge_id, 'merged_artifacts')
        merged_md = cfg.use.merge_metadata(
            var_name, *md_files_in
        )
        ns.update_usg_var_record(merge_id, merged_md)

    return art_as_md


def dump_recorded_md_file(
    cfg: ReplayConfig,
    node: ProvNode,
    action_name: str,
    md_id: str,
    fn: str
):
    '''
    Writes one metadata DataFrame pointed to by `md_id` to a .tsv file.
    Each action gets its own directory containing relevant md files.

    Raises a ValueError if the node has no metadata

    Parameters
    ----------
    cfg : ReplayConfig
        Replay configuration options. Contains the executing usage driver.
    node : ProvNode
        The node the creating action of which recorded metadata as input. Used
        here only to ensure that metadata was in fact recorded.
    action_name : str
        Looks like: f'{plugin}_{action}_{counter}'.
    md_id : str
        Looks like: f'{node uuid}_{param name}'.
    fn : str
        Looks like: f'{unique param name}.tsv'.

    Raises
    ------
    ValueError
        If the passed node does not have metadata in its creating action.
    '''
    if node.metadata is None:
        raise ValueError(
            'This function should only be called if the node has metadata.'
        )

    if cfg.md_out_dir:
        md_out_dir_base = pathlib.Path(cfg.md_out_dir)
    else:
        cwd = pathlib.Path.cwd()
        md_out_dir_base = cwd / 'recorded_metadata'

    action_dir = md_out_dir_base / action_name
    action_dir.mkdir(parents=True, exist_ok=True)

    md_df = node.metadata[md_id]
    out_fp = action_dir / (fn)
    md_df.to_csv(out_fp, sep='\t', index=False)


def param_is_metadata_column(
    cfg: ReplayConfig, param: str, plugin: str, action: str
) -> bool:
    '''
    Returns True if the parameter name `param` is registered as a
    MetadataColumn.

    Parameters
    ----------
    cfg : ReplayConfig
        Replay configuration options. Contains the plugin manager object.
    param : str
        The name of the parameter of interest.
    plugin : str
        The plugin that the relevant action belongs to.
    action : str
        The action that has the parameter of interest.

    Returns
    -------
    bool
        Indicating whether the parameter of interest is a MetadataColumn.

    Raises
    ------
    KeyError
        - If the plugin of interest is not registered with the plugin manager.
        - If the action of interest is not registered with the plugin.
        - If the parameter is not in the signature of the action.
    '''
    plugin = cfg.pm.get_plugin(id=plugin)

    try:
        action_f = plugin.actions[action]
    except KeyError:
        raise KeyError(
            f'No action registered with name {action} in plugin {plugin}.'
        )

    try:
        param_spec = action_f.signature.parameters[param]
    except KeyError:
        raise KeyError(
            f'No parameter registered with name {param} in action {action}.'
        )

    # HACK, but it works without relying on Q2's type system
    return 'MetadataColumn' in str(param_spec.qiime_type)


def collect_citations(
    dag: ProvDAG, deduplicate: bool = True
) -> bp.bibdatabase.BibDatabase:
    '''
    Returns a BibDatabase of all unique citations from a ProvDAG.
    If `deduplicate` is True references will be heuristically deduplicated.

    Parameters
    ----------
    dag : ProvDAG
        The ProvDAG object whose nodes contain citations to collect.
    deduplicate : bool
        Whether to deduplicate redundant citations.

    Returns
    -------
    bp.bibdatabase.BibDatabase
        A BibDatabase object containing the collected citations in bibtex
        format.
    '''
    bdb = bp.bibdatabase.BibDatabase()
    citations = []
    for node_uuid in dag:
        node = dag.get_node_data(node_uuid)
        # Skip no-prov nodes, which never have citations anyway
        if node is not None:
            node_citations = list(node.citations.values())
            citations.extend(node_citations)
    if deduplicate:
        citations = dedupe_citations(citations)

    bdb.entries = citations
    return bdb


class BibContent():
    '''
    A hashable data container capturing common bibtex fields

    Has many fields because keeping true duplicates is preferable to
    deduplicating two true non-duplicates.

    Parameters
    ----------
    entry : dict
        A dictionary of bibtex entries.
    '''
    def __init__(self, entry):
        self.title = entry.get('title'),
        self.author = entry.get('author'),
        self.journal = entry.get('journal')
        self.booktitle = entry.get('booktitle')
        self.year = entry.get('year')
        self.pages = entry.get('pages')

    def __eq__(self, other):
        return (
            type(self) is type(other) and
            self.title == other.title and
            self.author == other.author and
            self.journal == other.journal and
            self.booktitle == other.booktitle and
            self.year == other.year and
            self.pages == other.pages
        )

    def __hash__(self):
        return hash(
            str(self.title)
            + str(self.author)
            + str(self.journal)
            + str(self.journal)
            + str(self.booktitle)
            + str(self.year)
            + str(self.pages)
        )


def dedupe_citations(citations: List[Dict]) -> List[Dict]:
    '''
    Deduplicates citations based on bibtex id, bibtex content, and DOI.
    Citations are not guaranteed to be truly unique after deduplicating based
    on these values.

    Ensures only one Rachis framework citation.

    Parameters
    ----------
    citations : list of dict
        The possibly redundant citations, each dict is a bibtex citation.

    Returns
    -------
    list of dict
        The deduplicated citations.
    '''
    deduped_citations = []
    is_framework_cited = False
    id_set = set()
    doi_set = set()
    content_set = set()
    for entry in citations:
        citation_id = entry['ID']

        if 'framework|qiime2' in citation_id:
            if not is_framework_cited:
                with importlib.resources.open_text(
                        'rachis', 'citations.bib') as bibtex_file:
                    q2_entry = bp.load(bibtex_file).entries.pop()

                q2_entry['ID'] = citation_id
                id_set.add(citation_id)
                deduped_citations.append(q2_entry)
                is_framework_cited = True
            continue

        # dedupe on id
        if citation_id in id_set:
            continue

        # dedupe on content
        entry_content = BibContent(entry)
        if entry_content in content_set:
            continue
        else:
            content_set.add(entry_content)

        # dedupe on doi if present
        doi = entry.get('doi')
        if doi is None:
            id_set.add(citation_id)
            deduped_citations.append(entry)
        elif doi not in doi_set:
            id_set.add(citation_id)
            doi_set.add(doi)
            deduped_citations.append(entry)

    return deduped_citations


def replay_citations(
    dag: ProvDAG,
    out_fp: str,
    deduplicate: bool = True,
    suppress_header: bool = False
):
    '''
    Writes a bibtex file containing all citations from a ProvDAG to disk.
    If `deduplicate` is True citations will be deduplicated, see
    `dedupe_citations()` for details.

    Parameters
    ----------
    dag : ProvDAG
        The provenance graph from which to collect citations.
    out_fp : str
        The filepath to which to write the citations.
    deduplicate : bool
        Whether to deduplicate the collected citations.
    suppress_header : bool
        Whether to forgo adding a header and footer to the output file.
    '''
    bib_db = collect_citations(dag, deduplicate=deduplicate)
    boundary = '#' * 79
    header = []
    footer = []
    extra = [
        '',
        '# This bibtex-formatted citation file can be imported into '
        'popular citation ',
        '# managers like Zotero and Mendeley, simplifying management and '
        'formatting.'
    ]
    if not suppress_header:
        header = build_header(boundary=boundary, extra_text=extra) + ['\n']
        footer = build_footer(dag=dag, boundary=boundary)
    if bib_db.entries_dict == {}:
        bib_db = 'No citations were registered to the used Actions.'
        with open(out_fp, 'w') as bibfile:
            bibfile.write(bib_db)
    else:
        with open(out_fp, 'w') as bibfile:
            bibfile.write('\n'.join(header))
            bibfile.write(BibTexWriter().write(bib_db))
            bibfile.write('\n'.join(footer))


def replay_supplement(
    usage_drivers: List[Usage],
    payload: Union[str, ProvDAG],
    out_fp: str,
    validate_checksums: bool = True,
    parse_metadata: bool = True,
    use_recorded_metadata: bool = False,
    recurse: bool = False,
    deduplicate: bool = True,
    suppress_header: bool = False,
    verbose: bool = True,
    dump_recorded_metadata: bool = True
):
    '''
    Produces a zipfile package of useful documentation for in silico
    reproducibility of some QIIME 2 Result(s) from a ProvDAG, a QIIME 2
    Artifact, or a directory of Artifacts.

    Package includes:
    - replay scripts for all supported interfaces
    - a bibtex-formatted collection of all citations

    ProvDAG inputs retain their original config values. The
    `validate_checksums`, `parse_metadata`, `recurse`, and `verbose` parameters
    are disregarded if the payload is a ProvDAG.

    Parameters
    ----------
    usage_drivers : list of Usage
        The types of Usage drivers to use. Currently intended to consist of
        `ReplayPythonUsage`, `ReplayCLIUsage`.
    payload : str or ProvDAG
        A filepath to an artifact or directory containing artifacts, or the
        ProvDAG to be parsed.
    out_fp : str
        The filepath to which to write the zip file.
    validate_checksums : bool
        Whether to perform checksum validation on the input artifact.
    parse_metadata : bool
        Whether to parse study metadata recorded in provenance.
    use_recorded_metadata : bool
        Whether to use the metadata recorded in provenance.
    recurse : bool
        Whether to recursively parse nested directories containing artifacts.
    deduplicate : bool
        Whether to deduplicate citations collected from provenance.
    suppress_header : bool
        Whether to forgo rendering the header and footer that are included
        by default in replay scripts.
    verbose : bool
        Whether to print status messages during processing.
    dump_recorded_metadata : bool
        Whether to write the metadata recorded in provenance to disk.
    '''
    dag = ProvDAG(
        artifact_data=payload,
        validate_checksums=validate_checksums,
        parse_metadata=parse_metadata,
        recurse=recurse,
        verbose=verbose
    )
    with tempfile.TemporaryDirectory() as tempdir:
        tempdir_path = pathlib.Path(tempdir)
        arc_root = tempdir_path / pathlib.Path(out_fp).stem
        os.makedirs(arc_root)

        drivers_to_filenames = {
            'ReplayPythonUsage': 'python3_replay.py',
            'ReplayCLIUsage': 'cli_replay.sh',
        }

        for usage_driver in usage_drivers:
            if usage_driver.__name__ not in drivers_to_filenames:
                continue

            rel_fp = drivers_to_filenames[usage_driver.__name__]
            md_out_dir = arc_root / 'recorded_metadata'
            tmp_fp = arc_root / rel_fp
            replay_provenance(
                usage_driver=usage_driver,
                payload=dag,
                out_fp=str(tmp_fp),
                use_recorded_metadata=use_recorded_metadata,
                suppress_header=suppress_header,
                verbose=verbose,
                dump_recorded_metadata=dump_recorded_metadata,
                md_out_dir=md_out_dir
            )
            print(
                f'The {usage_driver.__name__} replay script was written to '
                f'{rel_fp}.'
            )

        citations_fp = arc_root / 'citations.bib'
        replay_citations(
            dag,
            out_fp=str(citations_fp),
            deduplicate=deduplicate,
            suppress_header=suppress_header
        )
        print('The citations bibtex file was written to citations.bib.')

        out_fp = pathlib.Path(os.path.realpath(out_fp))
        if out_fp.suffix == '.zip':
            out_fp = out_fp.with_suffix('')

        shutil.make_archive(out_fp, 'zip', tempdir)
        print(f'The reproducibility package was written to {out_fp}.zip.')
