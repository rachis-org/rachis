# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import collections
import inspect
import copy
import itertools
import tempfile
import typing
import types

import rachis.sdk
import rachis.core.type as qtype
from rachis.core.archive.provenance import MetadataInfo
from rachis.core.axis import ForEach
from .grammar import TypeExp, UnionExp
from .meta import TypeVarExp
from .collection import List, Set, Collection
from .primitive import infer_primitive_type
from .visualization import Visualization
from . import meta
from .util import (is_semantic_type, is_collection_type, is_primitive_type,
                   parse_primitive, contains_generic_base)
from ..util import ImmutableBase, checksum, create_collection_name
from rachis.plugin.context import IContext


VALID_CTX_ANNOTATIONS = (
    IContext,
)


class __NoValueMeta(type):
    def __repr__(self):
        return "NOVALUE"


# This sentinel is a class so that it retains the correct memory address when
# pickled
class _NOVALUE(metaclass=__NoValueMeta):
    pass


class ParameterSpec(ImmutableBase):
    NOVALUE = _NOVALUE

    def __init__(self, qiime_type=NOVALUE, view_type=NOVALUE, default=NOVALUE,
                 description=NOVALUE):
        self.qiime_type = qiime_type
        self.view_type = view_type
        self.default = default
        self.description = description

        self._freeze_()

    def has_qiime_type(self):
        return self.qiime_type is not self.NOVALUE

    def has_view_type(self):
        return self.view_type is not self.NOVALUE

    def has_default(self):
        return self.default is not self.NOVALUE

    def has_description(self):
        return self.description is not self.NOVALUE

    def duplicate(self, **kwargs):
        qiime_type = kwargs.pop('qiime_type', self.qiime_type)
        view_type = kwargs.pop('view_type', self.view_type)
        default = kwargs.pop('default', self.default)
        description = kwargs.pop('description', self.description)
        if kwargs:
            raise TypeError("Unknown arguments: %r" % kwargs)

        return ParameterSpec(qiime_type, view_type, default, description)

    def __repr__(self):
        return ("ParameterSpec(qiime_type=%r, view_type=%r, default=%r, "
                "description=%r)" % (self.qiime_type, self.view_type,
                                     self.default, self.description))

    def __eq__(self, other):
        return (self.qiime_type == other.qiime_type and
                self.view_type == other.view_type and
                self.default == other.default and
                self.description == other.description)

    def __ne__(self, other):
        return not (self == other)


class PipelineSignature:
    builtin_args = ('ctx',)
    is_mapped = False
    supports_mapping = False

    def __init__(self, callable, inputs, parameters, outputs,
                 input_descriptions=None, parameter_descriptions=None,
                 output_descriptions=None):
        """

        Parameters
        ----------
        callable : callable
            Callable with view type annotations on parameters and return.
        inputs : dict
            Parameter name to semantic type.
        parameters : dict
            Parameter name to primitive type.
        outputs : dict or list of tuples
            Each pair/tuple contains the name of the output (str) and its QIIME
            type.
        input_descriptions : dict, optional
            Input name to description string.
        parameter_descriptions : dict, optional
            Parameter name to description string.
        output_descriptions : dict, optional
            Output name to description string.
        """
        # update type of outputs if needed
        if type(outputs) is list:
            outputs = dict(outputs)
        elif type(outputs) is set:
            raise ValueError("Plugin registration for %r cannot use a set()"
                             " to define the outputs, as the order is random."
                             % callable.__name__)

        # This is only relevant for PipelineSignatures which may or may not be
        # annotated. This will be set in parse_signature based on whether the
        # ctx is annotated or not.
        self.annotated = False

        inputs, parameters, outputs, signature_order = \
            self._parse_signature(callable, inputs, parameters, outputs,
                                  input_descriptions, parameter_descriptions,
                                  output_descriptions)

        self._assert_valid_inputs(inputs)
        self._assert_valid_parameters(parameters)
        self._assert_valid_outputs(outputs)
        self._assert_valid_views(inputs, parameters, outputs)
        self.inputs = inputs
        self.parameters = parameters
        self.outputs = outputs
        self.signature_order = signature_order
        self._configure_mapping()

    def _configure_mapping(self):
        self.input_patterns = {}
        self.output_patterns = {}
        self.input_coverage = {}
        for specs, patterns, output in (
            (self.signature_order, self.input_patterns, False),
            (self.outputs, self.output_patterns, True),
        ):
            for name, spec in specs.items():
                view = spec.view_type
                if typing.get_origin(view) is not typing.Annotated:
                    continue
                view, *metadata = typing.get_args(view)
                found = [m for m in metadata if isinstance(m, ForEach)]
                if not found:
                    continue
                if not self.supports_mapping or len(found) != 1:
                    raise TypeError('Use one ForEach per mapped method view')
                pattern = patterns[name] = found[0].normalize()
                rules = set(pattern.coverage)
                if output:
                    if rules != {'matched'}:
                        raise TypeError(
                            'Output axis coverage must be matched')
                else:
                    if len(rules) != 1:
                        raise TypeError(
                            'A composite input axis needs uniform coverage')
                    rule = self.input_coverage[name] = rules.pop()
                    if (rule == 'optional' and
                            type(None) not in typing.get_args(view)):
                        raise TypeError(f'Optional input {name!r} needs None')
                    if name in self.parameters and (
                        view is not int or len(pattern.axes) != 1
                    ):
                        raise TypeError(
                            'A mapped parameter requires one integer axis')
                specs[name] = spec = spec.duplicate(view_type=view)
                if name in self.inputs and not output:
                    self.inputs[name] = spec
                if name in self.parameters and not output:
                    self.parameters[name] = spec

        self.is_mapped = bool(self.input_patterns or self.output_patterns)
        if not self.is_mapped:
            return
        for name, spec in self.parameters.items():
            if contains_generic_base(CaptureHolder, spec.view_type):
                raise TypeError(
                    f'Mapped methods do not support CaptureHolder '
                    f'parameters: {name!r}')
        bindings = set()
        groups = set()
        for pattern in self.input_patterns.values():
            group = frozenset(pattern.bindings)
            if group not in groups and bindings.intersection(group):
                raise TypeError(
                    'Mapped inputs require shared or disjoint axis patterns')
            groups.add(group)
            bindings.update(group)
        if not bindings or self.output_patterns.keys() != self.outputs.keys():
            raise TypeError('Mapped methods need mapped inputs and outputs')
        for name, pattern in self.output_patterns.items():
            output_bindings = set(pattern.bindings)
            if output_bindings != bindings:
                missing = sorted(bindings - output_bindings)
                unknown = sorted(output_bindings - bindings)
                raise TypeError(
                    f'Output {name!r} must bind all mapped axes. '
                    f'Missing bindings: {missing!r}. '
                    f'Unknown bindings: {unknown!r}.'
                )
            view = self.outputs[name].view_type
            if typing.get_origin(view) in (typing.Union, types.UnionType):
                args = typing.get_args(view)
                if len(args) != 2 or type(None) not in args:
                    raise TypeError('Mapped output unions must be optional')

    def _parse_signature(self, callable, inputs, parameters, outputs,
                         input_descriptions=None, parameter_descriptions=None,
                         output_descriptions=None):
        #  Initialize dictionaries if non-existant.
        if input_descriptions is None:
            input_descriptions = {}
        if parameter_descriptions is None:
            parameter_descriptions = {}
        if output_descriptions is None:
            output_descriptions = {}

        # Copy so we can "exhaust" the collections and check for missing params
        inputs = copy.copy(inputs)
        parameters = copy.copy(parameters)
        input_descriptions = copy.copy(input_descriptions)
        parameter_descriptions = copy.copy(parameter_descriptions)
        output_descriptions = copy.copy(output_descriptions)
        builtin_args = list(self.builtin_args)

        annotated_inputs = collections.OrderedDict()
        annotated_parameters = collections.OrderedDict()
        annotated_outputs = collections.OrderedDict()
        signature_order = collections.OrderedDict()
        annotations = callable.__annotations__
        if self.supports_mapping and any(
            isinstance(value, str) for value in annotations.values()
        ):
            annotations = typing.get_type_hints(callable, include_extras=True)

        for name, parameter in inspect.signature(callable).parameters.items():
            if (parameter.kind == parameter.VAR_POSITIONAL or
                    parameter.kind == parameter.VAR_KEYWORD):
                raise TypeError("Variadic definitions are unsupported: %r" %
                                name)

            if builtin_args:
                if builtin_args[0] != name:
                    raise TypeError("Missing builtin argument %r, got %r" %
                                    (builtin_args[0], name))
                if name == 'ctx':
                    # The ctx special argument to pipelines may be annotated
                    # just like all the real inputs/parameters to pipelines. We
                    # handle this here to avoid shuttling the ctx around as if
                    # it were an input/parameter
                    if parameter.annotation is not parameter.empty:
                        if parameter.annotation not in VALID_CTX_ANNOTATIONS:
                            raise TypeError(
                                'Context has the view type'
                                f'{parameter.annotation}. Valid view types for'
                                f' Context are {VALID_CTX_ANNOTATIONS}.'
                            )

                        # If the ctx is annotated everything else must be as
                        # well
                        self.annotated = True

                builtin_args = builtin_args[1:]
                continue

            view_type = ParameterSpec.NOVALUE
            if parameter.annotation is not parameter.empty:
                view_type = annotations.get(name, parameter.annotation)
            default = ParameterSpec.NOVALUE
            if parameter.default is not parameter.empty:
                default = parameter.default

            if name in inputs:
                description = input_descriptions.pop(name,
                                                     ParameterSpec.NOVALUE)
                param_spec = ParameterSpec(
                    qiime_type=inputs.pop(name), view_type=view_type,
                    default=default, description=description)
                annotated_inputs[name] = param_spec
                signature_order[name] = param_spec
            elif name in parameters:
                description = parameter_descriptions.pop(name,
                                                         ParameterSpec.NOVALUE)
                param_spec = ParameterSpec(
                    qiime_type=parameters.pop(name), view_type=view_type,
                    default=default, description=description)
                annotated_parameters[name] = param_spec
                signature_order[name] = param_spec
            elif name not in self.builtin_args:
                raise TypeError("Parameter in callable without QIIME type:"
                                " %r" % name)
        # we should have popped both of these empty by this point
        if inputs or parameters:
            raise TypeError("Callable does not have parameter(s): %r"
                            % (list(inputs) + list(parameters)))

        if 'return' in annotations:
            output_views = rachis.core.util.tuplize(annotations['return'])

            if len(output_views) != len(outputs):
                raise TypeError("Number of registered outputs (%r) does not"
                                " match annotation (%r)" %
                                (len(outputs), len(output_views)))

            for (name, qiime_type), view_type in zip(outputs.items(),
                                                     output_views):
                description = output_descriptions.pop(name,
                                                      ParameterSpec.NOVALUE)
                annotated_outputs[name] = ParameterSpec(
                    qiime_type=qiime_type, view_type=view_type,
                    description=description)
        else:
            for name, qiime_type in outputs.items():
                description = output_descriptions.pop(name,
                                                      ParameterSpec.NOVALUE)
                annotated_outputs[name] = ParameterSpec(
                    qiime_type=qiime_type, description=description)

        # we should have popped the descriptions empty by this point
        if input_descriptions or parameter_descriptions or output_descriptions:
            raise TypeError(
                "Callable does not have parameter(s)/output(s) found in "
                "descriptions: %r" % [*input_descriptions,
                                      *parameter_descriptions,
                                      *output_descriptions])

        return (annotated_inputs, annotated_parameters, annotated_outputs,
                signature_order)

    def collate_inputs(self, *args, **kwargs):
        # Collate positional inputs
        collated_inputs = {name: value for name, value in
                           zip(self.signature_order, args)}
        collated_inputs.update(kwargs)
        return collated_inputs

    def to_inspect_signature(self):
        parameters = []
        kind = inspect.Parameter.POSITIONAL_OR_KEYWORD
        for name, spec in self.signature_order.items():
            default = inspect.Parameter.empty
            if spec.has_default():
                default = spec.default
            parameters.append(inspect.Parameter(
                name, kind, default=default, annotation=spec.qiime_type))

        output_types = tuple(spec.qiime_type for spec in self.outputs.values())
        return_annotation = tuple[output_types]
        return inspect.Signature(
            parameters=parameters, return_annotation=return_annotation)

    def _assert_valid_inputs(self, inputs):
        for input_name, spec in inputs.items():
            if not is_semantic_type(spec.qiime_type):
                raise TypeError(
                    "Input %r must be a semantic QIIME type, not %r"
                    % (input_name, spec.qiime_type))

            if not isinstance(spec.qiime_type, (TypeExp, UnionExp)):
                raise TypeError(
                    "Input %r must be a complete semantic type expression, "
                    "not %r" % (input_name, spec.qiime_type))

            if spec.has_default() and spec.default is not None:
                raise ValueError(
                    "Input %r has a default value of %r. Only a default "
                    "value of `None` is supported for inputs."
                    % (input_name, spec.default))

            for var_selector in meta.select_variables(spec.qiime_type):
                var = var_selector(spec.qiime_type)
                if not var.input:
                    raise TypeError("An output variable has been associated"
                                    " with an input type: %r"
                                    % spec.qiime_type)

    def _assert_valid_parameters(self, parameters):
        for param_name, spec in parameters.items():
            if not is_primitive_type(spec.qiime_type):
                raise TypeError(
                    "Parameter %r must be a primitive QIIME type, not %r"
                    % (param_name, spec.qiime_type))

            if not isinstance(spec.qiime_type, (TypeExp, UnionExp)):
                raise TypeError(
                    "Parameter %r must be a complete primitive type "
                    "expression, not %r" % (param_name, spec.qiime_type))

            if (spec.has_default() and
                    spec.default is not None and
                    spec.default not in spec.qiime_type):
                raise TypeError("Default value for parameter %r is not of "
                                "semantic QIIME type %r or `None`."
                                % (param_name, spec.qiime_type))

            for var_selector in meta.select_variables(spec.qiime_type):
                var = var_selector(spec.qiime_type)
                if not var.input:
                    raise TypeError("An output variable has been associated"
                                    " with an input type: %r"
                                    % spec.qiime_type)

    def _assert_valid_outputs(self, outputs):
        if len(outputs) == 0:
            raise TypeError("%s requires at least one output"
                            % self.__class__.__name__)

        for output_name, spec in outputs.items():
            if not output_name.isidentifier():
                raise ValueError(
                    f'Output name "{output_name}" must be a valid python '
                    'identifier.'
                )
            if not (is_semantic_type(spec.qiime_type) or
                    spec.qiime_type == Visualization or
                    spec.qiime_type == Collection[Visualization]):
                raise TypeError(
                    "Output %r must be a semantic QIIME type or "
                    "Visualization, not %r"
                    % (output_name, spec.qiime_type))

            if spec.qiime_type.name == "List":
                raise TypeError(
                    f"Output '{output_name}' has been registered as a 'List', "
                    "please register it as a 'Collection'")

            if not isinstance(spec.qiime_type, (TypeVarExp, TypeExp)):
                raise TypeError(
                    "Output %r must be a complete type expression, not %r"
                    % (output_name, spec.qiime_type))

            for var_selector in meta.select_variables(spec.qiime_type):
                var = var_selector(spec.qiime_type)
                if not var.output:
                    raise TypeError("An input variable has been associated"
                                    " with an input type: %r")

    def _assert_valid_views(self, inputs, parameters, outputs):
        """
        Assert that the view type annotations on a Pipeline, if they are
        present, are valid. Inputs and outputs must be some form of Result.
        Parameters don't follow any special rules.

        Parameters
        ----------
        inputs : Dict[string : ParameterSpec]
            The inputs to the Pipeline
        parameters : Dict[string : ParameterSpec]
            The parameters to the Pipeline
        outputs : Dict[string : ParameterSpec]
            The outputs of the Pipeline

        Raises
        ------
        TypeError
            If a view type annotation on an input or an output is invalid.
        """
        self._assert_all_annotated_or_unannotated(
            inputs, parameters, outputs
        )

        VALID_INPUT_ANNOTATIONS = (
            rachis.sdk.Artifact,
            list[rachis.sdk.Artifact],
            dict[str, rachis.sdk.Artifact]
        )
        VALID_OUTPUT_ANNOTATIONS = (
            rachis.sdk.Artifact,
            list[rachis.sdk.Artifact],
            dict[str, rachis.sdk.Artifact],
            rachis.sdk.Visualization,
            list[rachis.sdk.Visualization],
            dict[str, rachis.sdk.Visualization]
        )

        if self.annotated:
            for name, spec in inputs.items():
                if spec.view_type not in VALID_INPUT_ANNOTATIONS:
                    raise TypeError(
                        f'The input {name} has the view type {spec.view_type}.'
                        f' Valid view types for Pipeline inputs are'
                        f' {VALID_INPUT_ANNOTATIONS}.'
                    )

            for name, spec in outputs.items():
                if spec.view_type not in VALID_OUTPUT_ANNOTATIONS:
                    raise TypeError(
                        f'The output {name} has the view type'
                        f' {spec.view_type}. Valid view types for Pipeline'
                        f' outputs are {VALID_OUTPUT_ANNOTATIONS}.'
                    )

    def _assert_all_annotated_or_unannotated(
            self, inputs, parameters, outputs
        ):
        """
        Asserts that we don't have a mix of annotated and unannotated inputs,
        parameters, and outputs. All must be annotated or none.

        Parameters
        ----------
        inputs : Dict[string : ParameterSpec]
            The inputs to the Pipeline
        parameters : Dict[string : ParameterSpec]
            The parameters to the Pipeline
        outputs : Dict[string : ParameterSpec]
            The outputs of the Pipeline

        Returns
        -------
        Boolean
            True if we have annotations False if we don't

        Raises
        ------
        TypeError
            If there is a mix of annotated and unannotated
        """
        all_specs = [
            spec.has_view_type() for spec in
            (*inputs.values(), *parameters.values(), *outputs.values())
        ]

        if (not all(all_specs) and self.annotated) or \
                (any(all_specs) and not self.annotated):
            raise TypeError(
                "Pipelines must have annotations for the context, all inputs,"
                " parameters, and outputs or no annotations."
            )

    def coerce_user_input(self, **user_input):
        """ Coerce user inputs to be appropriate for callable
        """
        callable_args = {}

        for name, spec in self.signature_order.items():
            # Some arguments may be optional and won't be present here. Whether
            # they passed all mandatory arguments or not is validated elsewhere
            if name in user_input:
                arg = user_input[name]
                if name in self.inputs:
                    callable_args[name] = self._coerce_given_input(arg, spec)
                else:
                    callable_args[name] = \
                        self._coerce_given_parameter(arg, spec)

        return callable_args

    def _coerce_given_input(self, _input, spec):
        """ Coerce input to be appropriate for callable
        """
        _, qiime_name = self._get_qiime_type_and_name(spec)

        # Transform collection from list to dict and vice versa if needed
        if qiime_name == 'Collection' and isinstance(_input, list):
            _input = self._list_to_dict(_input)
        elif qiime_name == 'List' and \
                (isinstance(_input, dict) or
                 isinstance(_input, rachis.sdk.ResultCollection)):
            _input = self._dict_to_list(_input)

        if isinstance(_input, dict):
            _input = rachis.sdk.ResultCollection(_input)

        return _input

    def _coerce_given_parameter(self, param, spec):
        """ Coerce parameter to be appropriate for callable
        """
        view_type = spec.view_type
        qiime_type = spec.qiime_type

        if view_type is dict and isinstance(param, list):
            param = self._list_to_dict(param)
        elif view_type is list and isinstance(param, dict):
            param = self._dict_to_list(param)

        if qiime_type is qtype.Threads and param == 'auto':
            param = 0

        return param


    def transform_and_add_callable_args_to_prov(self, provenance,
                                                record_inputs=True, /,
                                                **callable_args):
        """ Transform inputs to views and add all callable arguments to
            provenance. Needs to be done together so we can add transformation
            records to provenance and because we want transformers to run
            outside the DFK in parsl
        """
        # Keep track of all CaptureHolders so we can error if they do not have
        # a value set after execution
        captures = []

        for name, spec in self.signature_order.items():
            arg = callable_args[name]

            if name in self.inputs:
                if record_inputs:
                    callable_args[name] = \
                        self._transform_and_add_input_to_prov(
                            provenance, name, spec, arg)
            else:
                if contains_generic_base(CaptureHolder, spec.view_type):
                    capture = CaptureHolder(
                        name, arg, spec.qiime_type, provenance
                    )
                    callable_args[name] = capture
                    captures.append(capture)

                provenance.add_parameter(name, spec.qiime_type, arg)

        return callable_args, captures

    def _transform_and_add_input_to_prov(self, provenance, name, spec, _input):
        """ Transform the input and add both the input and the transformation
            record to provenance
        """
        transformed_input = None

        # Add input to provenance after creating the correct collection
        # type
        provenance.add_input(name, _input)
        qiime_type, _ = self._get_qiime_type_and_name(spec)

        if type(self) is PipelineSignature:
            # We do not transform Artifacts to view type for Pipeline inputs.
            # view type annotations are purely informational for
            # whoever/whatever is looking at the pipeline signature
            return _input

        # Transform artifacts to view types as necessary
        if _input is None:
            transformed_input = None
        elif spec.has_view_type():
            recorder = provenance.transformation_recorder(name)
            # Transform all members of collection into view type
            if qtype.is_collection_type(qiime_type):
                if isinstance(_input, rachis.sdk.result.ResultCollection):
                    transformed_input = rachis.sdk.result.ResultCollection(
                        {k: v._view(spec.view_type,
                                    recorder) for k, v in _input.items()})
                else:
                    transformed_input = [
                        i._view(spec.view_type, recorder) for i in _input]
            else:
                transformed_input = _input._view(spec.view_type, recorder)
        else:
            transformed_input = _input

        return transformed_input

    def _get_qiime_type_and_name(self, spec):
        """ Get concrete qiime type and name from nested spec
        """
        qiime_type = spec.qiime_type
        qiime_name = spec.qiime_type.name

        # I don't think this will necessarily work if we nest collection
        # types in the future
        if qiime_name == '':
            # If we have an outer union as our semantic type, the name will
            # be the empty string, and the type will be the entire union
            # expression. In order to get a meaningful name and a type
            # that tells us if we have a collection, we unpack the union
            # and grab that info from the first element. All subsequent
            # elements will share this same basic information because we
            # do not allow
            # List[TypeA] | Collection[TypeA]
            qiime_type = next(iter(spec.qiime_type))
            qiime_name = qiime_type.name

        return qiime_type, qiime_name

    def coerce_given_outputs(self, output_views, output_types, ctx,
                             provenance):
        """ Coerce the outputs produced by the method into the desired types if
            possible. Primarily useful to create collections of outputs
        """
        outputs = []

        for output_view, (name, spec) in zip(output_views,
                                             output_types.items()):
            if spec.qiime_type.name == 'Collection':
                output = rachis.sdk.ResultCollection()
                size = len(output_view)

                if isinstance(output_view, rachis.sdk.ResultCollection) or \
                        isinstance(output_view, dict):
                    keys = list(output_view.keys())
                    values = list(output_view.values())
                else:
                    keys = None
                    values = output_view

                for idx, view in enumerate(values):
                    if keys is not None:
                        key = str(keys[idx])
                    else:
                        key = str(idx)

                    collection_name = create_collection_name(
                        name=name, key=key, idx=idx, size=size)
                    output[key] = self._create_output_artifact(
                        provenance, collection_name, ctx, spec, view)
            elif type(output_view) is not spec.view_type:
                raise TypeError(
                    "Expected output view type %r, received %r" %
                    (spec.view_type.__name__, type(output_view).__name__))
            else:
                output = self._create_output_artifact(
                    provenance, name, ctx, spec, output_view)

            outputs.append(output)

        return outputs

    def _create_output_artifact(self, provenance, name, ctx, spec, view):
        """ Create an output artifact from a view and add it to provenance
        """
        prov = provenance.fork(name)
        qiime_type = spec.qiime_type

        # If we have a collection we need to get a concrete qiime_type to
        # instantiate each artifact as.
        #
        # For instance, we cannot instantiate a Collection[SingleInt] from an
        # integer. We want to instantiate a SingleInt that will be put into a
        # ResultCollection outside of this method.
        if is_collection_type(qiime_type):
            qiime_type = qiime_type.fields[0]

        artifact = rachis.sdk.Artifact._from_view(
            qiime_type, view, spec.view_type, prov)
        artifact = ctx.add_reference(artifact)

        return artifact

    def decode_parameters(self, **kwargs):
        params = {}
        for key, spec in self.parameters.items():
            if (spec.has_default() and
                    spec.default is None and
                    kwargs[key] is None):
                params[key] = None
            else:
                params[key] = parse_primitive(spec.qiime_type, kwargs[key])
        return params

    def _dict_to_list(self, _input):
        """ Turn dict to list
        """
        return list(_input.values())

    def _list_to_dict(self, _input):
        """ Turn list to dict
        """
        return {str(idx): v for idx, v in enumerate(_input)}

    def check_types(self, **kwargs):
        for name, spec in self.signature_order.items():
            parameter = kwargs[name]
            # A type mismatch is unacceptable unless the value is None
            # and this parameter's default value is None.
            if ((parameter not in spec.qiime_type) and
                    not (spec.has_default() and spec.default is None
                         and parameter is None)):

                if isinstance(parameter, rachis.sdk.Visualization):
                    raise TypeError(
                        "Parameter %r received a Visualization as an "
                        "argument. Visualizations may not be used as inputs."
                        % name)

                elif isinstance(parameter, rachis.sdk.Artifact):
                    raise TypeError(
                        "Parameter %r requires an argument of type %r. An "
                        "argument of type %r was passed." % (
                            name, spec.qiime_type, parameter.type))

                elif isinstance(parameter, rachis.Metadata):
                    raise TypeError(
                        "Parameter %r received Metadata as an "
                        "argument, which is incompatible with parameter "
                        "type: %r" % (name, spec.qiime_type))

                else:  # handle primitive types
                    raise TypeError(
                        "Parameter %r received %r as an argument, which is "
                        "incompatible with parameter type: %r"
                        % (name, parameter, spec.qiime_type))

    def solve_output(self, **kwargs):
        solved_outputs = None
        for _, spec in itertools.chain(self.inputs.items(),
                                       self.parameters.items(),
                                       self.outputs.items()):
            if list(meta.select_variables(spec.qiime_type)):
                break  # a variable exists, do the hard work
        else:
            # no variables
            solved_outputs = self.outputs

        if solved_outputs is None:
            inputs = {**{k: s.qiime_type for k, s in self.inputs.items()},
                      **{k: s.qiime_type for k, s in self.parameters.items()}}
            outputs = {k: s.qiime_type for k, s in self.outputs.items()}
            input_types = {
                k: self._infer_type(k, v) for k, v in kwargs.items()}

            solved = meta.match(input_types, inputs, outputs)
            solved_outputs = collections.OrderedDict(
                (k, s.duplicate(qiime_type=solved[k]))
                for k, s in self.outputs.items())

        for output_name, spec in solved_outputs.items():
            if not spec.qiime_type.is_concrete():
                raise TypeError(
                    "Solved output %r must be a concrete type, not %r" %
                    (output_name, spec.qiime_type))

        return solved_outputs

    def _infer_type(self, key, value):
        if value is None:
            if key in self.inputs:
                return self.inputs[key].qiime_type
            elif key in self.parameters:
                return self.parameters[key].qiime_type
            # Shouldn't happen:
            raise ValueError("Parameter passed not consistent with signature.")
        if type(value) is list:
            inner = UnionExp((self._infer_type(key, v) for v in value))
            return List[inner.normalize()]
        if type(value) is set:
            inner = UnionExp((self._infer_type(key, v) for v in value))
            return Set[inner.normalize()]
        if type(value) is dict or \
                isinstance(value, rachis.sdk.ResultCollection):
            inner = UnionExp(
                (self._infer_type(key, v) for v in value.values()))
            return Collection[inner.normalize()]
        if isinstance(
                value, (rachis.sdk.Artifact, rachis.sdk.proxy.ProxyArtifact)):
            return value.type
        else:
            return infer_primitive_type(value)

    def __repr__(self):
        lines = []
        for group in 'inputs', 'parameters', 'outputs':
            lookup = getattr(self, group)
            lines.append('%s:' % group)
            for name, spec in lookup.items():
                lines.append('    %s: %r' % (name, spec))
        return '\n'.join(lines)

    def __eq__(self, other):
        return (type(self) is type(other) and
                self.inputs == other.inputs and
                self.parameters == other.parameters and
                self.outputs == other.outputs and
                self.signature_order == other.signature_order)

    def __ne__(self, other):
        return not (self == other)


class MethodSignature(PipelineSignature):
    builtin_args = ()
    supports_mapping = True

    def _assert_valid_outputs(self, outputs):
        super()._assert_valid_outputs(outputs)
        # Assert all output types are semantic types. The parent class is less
        # strict in its output type requirements.
        for output_name, spec in outputs.items():
            if not is_semantic_type(spec.qiime_type):
                raise TypeError(
                    "Output %r must be a semantic QIIME type, not %r" %
                    (output_name, spec.qiime_type))

    def _assert_valid_views(self, inputs, parameters, outputs):
        for name, spec in itertools.chain(inputs.items(),
                                          parameters.items(),
                                          outputs.items()):
            if not spec.has_view_type():
                raise TypeError("Method is missing a function annotation for"
                                " parameter: %r" % name)


class VisualizerSignature(PipelineSignature):
    builtin_args = ('output_dir',)

    def __init__(self, callable, inputs, parameters, input_descriptions=None,
                 parameter_descriptions=None):
        outputs = {'visualization': Visualization}
        output_descriptions = None
        super().__init__(callable, inputs, parameters, outputs,
                         input_descriptions, parameter_descriptions,
                         output_descriptions)

    def _assert_valid_outputs(self, outputs):
        super()._assert_valid_outputs(outputs)
        output = outputs['visualization']
        if output.has_view_type() and output.view_type is not None:
            raise TypeError(
                "Visualizer callable cannot return anything. Its return "
                "annotation must be `None`, not %r. Write output to "
                "`output_dir`." % output.view_type)

    def _assert_valid_views(self, inputs, parameters, outputs):
        for name, spec in itertools.chain(inputs.items(), parameters.items()):
            if not spec.has_view_type():
                raise TypeError("Visualizer is missing a function annotation"
                                " for parameter: %r" % name)


IndexedCollectionElement = collections.namedtuple(
    'IndexedCollectionElement', ['item_name', 'idx', 'total'])


class HashableInvocation():
    def __init__(self, plugin_action, arguments):
        self.plugin_action = plugin_action

        unified_arguments = self._unify_dicts(arguments)
        self.arguments = self._make_hashable(unified_arguments)

    def __eq__(self, other):
        return (self.plugin_action == other.plugin_action) \
              and (self.arguments == other.arguments)

    def __hash__(self):
        return hash((self.plugin_action, self.arguments))

    def __repr__(self):
        return (f'\nPLUGIN_ACTION: {self.plugin_action}\nARGUMENTS:'
                f' {self.arguments}\n')

    def _unify_dicts(self, arguments):
        """Check if action.yaml gave us any lists of single element dicts to
        unify
        """
        for idx, argument in enumerate(arguments):
            name, value = list(argument.items())[0]
            if isinstance(value, list) and \
                    all(isinstance(x, dict) for x in value):
                arguments[idx] = {name: self._unify_dict(value)}

        return arguments

    def _unify_dict(self, collection):
        """If we do have a list of single element dicts, turn it into one dict
        """
        unified_dict = {}

        for elem in collection:
            for k, v in elem.items():
                unified_dict[k] = v

        return unified_dict

    def _make_hashable(self, collection):
        """Take an arbitrarily nested collection and turn it into a hashable
        arbitrarily nested tuple. Turns Artifacts into their uuid and Metadata
        into their md5sum
        """
        from rachis import Artifact
        from rachis.sdk import ResultCollection
        from rachis.metadata.metadata import _MetadataBase

        new_collection = []

        if isinstance(collection, dict) or \
                isinstance(collection, ResultCollection):
            for k, v in collection.items():
                new_collection.append((k, self._make_hashable(v)))
        elif isinstance(collection, list):
            for elem in collection:
                new_collection.append(self._make_hashable(elem))
        elif isinstance(collection, Artifact):
            return str(collection.uuid)
        elif isinstance(collection, _MetadataBase):
            with tempfile.NamedTemporaryFile('w') as fh:
                fp = fh.name
                collection.save(fp)
                collection = checksum(fp, checksum_type='md5')
                return collection
        elif isinstance(collection, MetadataInfo):
            return collection.md5sum_hash
        else:
            return collection

        return tuple(new_collection)


T = typing.TypeVar('T')


class CaptureHolder(typing.Generic[T]):
    # This value is treated as no value being set on the CaptureHolder
    CAPTURE_HOLDER_DEFAULT = None

    @classmethod
    def get_or_set(
            cls,
            instance: 'CaptureHolder' | T | None,
            setter: typing.Callable[[], T]
        ) -> T:
        """
        Gets the value set on a CaptureHolder, or sets a value if there isn't
        one.

        Parameters
        ----------
        instance: CaptureHolder[T] | T | None
            The CaptureHolder object we are getting/seting on
        setter: Callable[[], T]
            A callable that generates a value to set on instance if needed

        Returns
        -------
        T
            The value we have set on instance

        Note
        ----
        In order to work when calling the functions that underly an Action
        directly during testing, this method is tolerant of receiving anything
        as the "instance" parameter since during normal operation the
        CaptureHolder instance is created by the framework.

        Raises
        ------
        ValueError:
            If overwrite is False and a value is already set on the
            CaptureHolder instance
        TypeError:
            If the type of the value passed in is incompatible with the type
            of the CaptureHolder parameter
        """
        if not isinstance(instance, CaptureHolder):
            if instance == cls.CAPTURE_HOLDER_DEFAULT:
                return setter()

            return instance

        if instance.is_set:
            return instance._value

        value = setter()
        instance._set_value(value)
        return instance._value

    @classmethod
    def set_value(
        cls,
        instance: 'CaptureHolder' | T | None,
        value: T,
        overwrite: bool = False
    ) -> T:
        """
        Sets the passed value on the CaptureHolder instance provided and
        returns the set value.

        Parameters
        ----------
        instance: CaptureHolder[T] | T | None
            The CaptureHolder object we are setting the value on.
        value: T
            The value we are setting on the CaptureHolder.
        overwrite: bool
            Whether or not we want to overwrite the value currently set on the
            CaptureHolder or raise an error if a value is currently set.

        Returns
        -------
        T
            The value we have set on instance

        Note
        ----
        This method can overwrite the value set on the CaptureHolder.

        In order to work when calling the functions that underly an Action
        directly during testing, this method is tolerant of receiving anything
        as the "instance" parameter since during normal operation the
        CaptureHolder instance is created by the framework.

        Raises
        ------
        ValueError:
            If overwrite is False and a value is already set on the
            CaptureHolder instance.
        TypeError:
            If the type of the value passed in is incompatible with the type
            of the CaptureHolder parameter.
        """
        if not isinstance(instance, CaptureHolder):
            if instance == cls.CAPTURE_HOLDER_DEFAULT:
                return value

            return instance

        instance._set_value(value, overwrite=overwrite)
        return instance._value

    def __init__(self, name, value, type, provenance):
        """
        Initialize a CaptureHolder object.

        Parameters
        ----------
        name: str
            The name of the QIIME 2 parameter this CaptureHolder is for.
        value: any
            The value we are setting on this CaptureHolder at init time. Either
            the value the user passed in or the default value of this parameter
            if they did not pass one.
        type: TypeExpr
            The QIIME type of the parameter this CaptureHolder is wrapping
        provenance: ProvenanceCapture
            The provenance capture for this action.
        """
        self._set = False
        self._name = name
        self._value = value
        self._type = type
        self._provenance = provenance

    @property
    def is_set(self):
        """
        Returns True if a value has been set on this CaptureHolder False if
        not.

        Determines if the CaptureHolder has a set value based on whether a
        previous function call has marked the value as set OR the value does
        not match the CAPTURE_HOLDER_DEFAULT.

        Returns
        -------
        bool
            Whether a value has been set on this CaptureHolder or not.
        """
        return self._set or self._value != CaptureHolder.CAPTURE_HOLDER_DEFAULT

    def _set_value(self, value, overwrite=False):
        """
        Sets the value on this CaptureHolder instance after performing
        overwrite checking and typechecking.

        Parameters
        ----------
        value: any
            The value to set on the CaptureHolder instance.
        overwrite: bool
            Whether to allow overwriting the value set on the CaptureHolder or
            not.

        Raises
        ------
        ValueError:
            If overwrite is False and a value is already set on the
            CaptureHolder instance
        TypeError:
            If the type of the value passed in is incompatible with the type
            of the CaptureHolder parameter
        """
        if self.is_set and not overwrite:
            raise ValueError(f'Value already set to {self._value}')

        if value is None or value in self._type:
            self._provenance.parameters[self._name] = value
            self._value = value
            self._set = True
        else:
            raise TypeError(
                f'Value {value} not compatible with type {self._type}'
            )
