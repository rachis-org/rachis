# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import copy
import inspect
import tempfile
import textwrap
from contextlib import contextmanager, ExitStack
from enum import Enum
from itertools import chain
from types import FunctionType

import dill

from typing import Mapping, TypedDict, Union
from types import MappingProxyType

import rachis.sdk
from rachis.sdk.futures import as_future, join, result_as_future
from rachis.sdk.iresult import IResult
from rachis.sdk.proxy import (
    ProxyResult, ProxyResultCollection, contains_proxy, output_proxy,
)
from rachis.sdk.result import Artifact, ResultCollection, ChecksumCache
from rachis.sdk.results import Results
import rachis.core.type as qtype
from rachis.core.type.util import contains_generic_base
import rachis.core.archive as archive
from rachis.core.util import (LateBindingAttribute, tuplize,
                              create_collection_name)
from rachis.core.cite import CitationRecord


class FramePhase(Enum):
    INNER_APPLY = 'inner_apply'
    FINISHED = 'finished'


class Frame:
    """Serializable continuation and runtime state for one application.

    Binding establishes the inputs and next phase before dispatch. The
    receiving Context calls the frame to enter that phase's wrapper stack.
    Live stacks and resources belong to the process applying the frame.
    """

    def __init__(self, action, ctx, bound_arguments, *, phase):
        self.action = action
        self.ctx = ctx
        self.bound_arguments = bound_arguments
        self.phase = phase
        self.transformed_arguments = None
        self.output_types = None
        self.results = None
        self.provenance = None
        self.ready_provenance = None

    def __call__(self):
        phase = self.phase
        self.phase = FramePhase.FINISHED
        match phase:
            case FramePhase.INNER_APPLY:
                return self.action._inner_apply(self)
            case FramePhase.FINISHED:
                raise RuntimeError('Cannot call a finished frame')
            case _:
                raise ValueError(f'Unknown frame phase: {phase!r}')


@contextmanager
def track_results_wrapper(frame):
    yield
    # Observe the exterior result after all result adapters have unwound.
    frame.ctx._track_results(frame.results)


@contextmanager
def deprecation_wrapper(frame):
    action = frame.action
    if action.deprecated:
        with rachis.core.util.warning() as warn:
            warn(action._build_deprecation_message(), FutureWarning)
    if action.migrated:
        with rachis.core.util.warning() as warn:
            warn(action._build_migration_message(), FutureWarning)
    yield


@contextmanager
def bind_signature_wrapper(frame):
    signature = frame.action.signature
    arguments = frame.bound_arguments
    signature.check_types(**arguments)
    frame.output_types = signature.solve_output(**arguments)
    frame.bound_arguments = signature.normalize_arguments(**arguments)
    yield


@contextmanager
def prepare_callable_arguments_wrapper(frame):
    signature = frame.action.signature
    arguments = frame.transformed_arguments = frame.bound_arguments.copy()
    for name, spec in signature.inputs.items():
        argument = arguments[name]
        if isinstance(argument, ProxyResultCollection):
            argument = argument.result()
        if isinstance(argument, ResultCollection):
            if not argument:
                raise TypeError(
                    f'Parameter {name!r} received an empty collection.')
            arguments[name] = signature._shape_collection_input(argument, spec)
    yield


@contextmanager
def provenance_wrapper(frame):
    action = frame.action
    provenance = frame.provenance = action._ProvCaptureCls(
        action.type, action.plugin_id, action.id, frame.ctx.execution_metadata)
    inputs = {}
    for name in action.signature.inputs:
        value = frame.transformed_arguments[name]
        if isinstance(value, ResultCollection):
            value = ResultCollection(dict(value.items()))
        elif isinstance(value, (list, set)):
            value = value.copy()
        inputs[name] = value
    for name, spec in action.signature.parameters.items():
        provenance.add_parameter(name, spec.qiime_type,
                                 frame.bound_arguments[name])

    yield

    def record_inputs(*values):
        cache = ChecksumCache()
        for name, value in zip(inputs, values):
            if isinstance(value, Artifact):
                cache.cache_artifact(value)
            elif isinstance(value, ResultCollection):
                cache.cache_result_collection(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, Artifact):
                        cache.cache_artifact(item)
            provenance.add_input(name, value)
        return provenance

    if contains_proxy(inputs):
        frame.ready_provenance = join(
            map(result_as_future, inputs.values()), record_inputs,
            ctx=frame.ctx)
    else:
        frame.ready_provenance = as_future(
            lambda: record_inputs(*inputs.values()))


@contextmanager
def input_transform_wrapper(frame):
    signature = frame.action.signature
    arguments = frame.transformed_arguments
    recorder_for = frame.provenance.transformation_recorder

    for name in signature.inputs:
        arguments[name] = signature._transform_input(
            name, arguments[name], recorder_for)
    yield


@contextmanager
def capture_holders_wrapper(frame):
    arguments = frame.transformed_arguments
    captures = []
    for name, spec in frame.action.signature.parameters.items():
        if contains_generic_base(qtype.CaptureHolder, spec.view_type):
            capture = qtype.CaptureHolder(
                name, arguments[name], spec.qiime_type, frame.provenance)
            arguments[name] = capture
            captures.append(capture)

    yield

    for capture in captures:
        if not capture.is_set:
            raise ValueError(
                f"The capture parameter '{capture._name}' never had a "
                "value set for it"
            )


def checked_return_values(value, expected):
    values = tuplize(value)
    actual = len(values)
    if actual != expected:
        raise TypeError(
            "Number of returned values must match number of outputs: "
            f"{actual} != {expected}")
    return values


@contextmanager
def method_output_wrapper(frame):
    yield
    views = checked_return_values(frame.results, len(frame.output_types))
    provenance = frame.ready_provenance.result()
    outputs = frame.action.signature.coerce_given_outputs(
        views, frame.output_types, frame.ctx, provenance.fork)
    frame.results = Results(frame.output_types, outputs)


@contextmanager
def visualization_wrapper(frame):
    with tempfile.TemporaryDirectory(prefix='rachis-temp-') as temp_dir:
        frame.transformed_arguments['output_dir'] = temp_dir
        yield
        if frame.results is not None:
            raise TypeError(
                "Visualizer %r should not return anything. "
                "Received %r as a return value."
                % (frame.action, frame.results))
        provenance = frame.ready_provenance.result()
        visualization = rachis.sdk.Visualization._from_data_dir(
            temp_dir, provenance.fork('visualization'))
        frame.results = Results(
            frame.output_types, (frame.ctx.add_reference(visualization),))


def _validate_pipeline_output(output):
    if isinstance(output, (dict, list)):
        output = ResultCollection(output)
    if isinstance(output, ProxyResultCollection):
        return output
    values = output.values() if isinstance(output, ResultCollection) else (
        output,)
    for value in values:
        if not isinstance(value, IResult):
            raise TypeError(f'Pipelines must return `Result` objects, '
                            f'not {type(value)}')
    return output


def _intersect_pipeline_output_type(expected, output_type, output_name):
    refined_type = output_type & expected

    if (not refined_type.is_bottom()) and refined_type.is_concrete():
        return refined_type

    raise TypeError(
        "Expected output %s to be of type %r, received %r"
        % (output_name, expected, output_type))


def _alias_pipeline_output(name, qiime_type, output, provenance, ctx):
    if qiime_type.name == 'Collection':
        aliased = ResultCollection()
        size = len(output)
        for idx, (key, value) in enumerate(output.items()):
            member = create_collection_name(name=name, key=key,
                                            idx=idx, size=size)
            refined_type = _intersect_pipeline_output_type(
                qiime_type.fields[0], value.type,
                "%r member %r" % (name, key))
            aliased[str(key)] = value._alias(
                member, provenance, ctx, refined_type)
        return aliased
    refined_type = _intersect_pipeline_output_type(
        qiime_type, output.type, repr(name))
    return output._alias(name, provenance, ctx, refined_type)


@contextmanager
def pipeline_call_wrapper(frame):
    frame.transformed_arguments['ctx'] = frame.ctx
    yield

    outputs = checked_return_values(frame.results, len(frame.output_types))
    frame.results = tuple(map(_validate_pipeline_output, outputs))


@contextmanager
def pipeline_alias_wrapper(frame):
    yield

    ctx = frame.ctx
    proxies = {}
    for output, (name, spec) in zip(frame.results, frame.output_types.items()):
        qiime_type = spec.qiime_type

        def alias(resolved, provenance, name=name, qiime_type=qiime_type):
            return _alias_pipeline_output(
                name, qiime_type, resolved, provenance, ctx)

        future = join((result_as_future(output), frame.ready_provenance),
                      alias, ctx=ctx)
        if isinstance(output, ProxyResult):
            refined_type = _intersect_pipeline_output_type(
                qiime_type, output.type, repr(name))
            proxies[name] = output._with_future(future, refined_type)
        else:
            proxies[name] = output_proxy(future, None, qiime_type)

    frame.results = Results(proxies, proxies.values())


# TODO: validation on these params via data.yaml in distributions
class MigrationInfo(TypedDict):
    to_plugin: str
    from_distro: str
    to_distro: str
    epoch: str


def _validate_and_freeze_migrated(migrated: Union[bool, Mapping[str, str]]):
    if migrated is False:
        return False

    if migrated is True or not isinstance(migrated, Mapping):
        raise TypeError(
            '`migrated` must be False or a Mapping with at least `to_plugin`.')

    allowed = {'to_plugin', 'from_distro', 'to_distro', 'epoch'}

    if 'to_plugin' not in migrated:
        raise ValueError('`migrated` mapping missing required key: '
                         '`to_plugin`.')
    for key in migrated:
        if key not in allowed:
            raise ValueError(f'Got unexpected key: {key}. '
                             f'Allowed keys are: {allowed}')

    normalized = {}
    for key, val in migrated.items():
        if not isinstance(val, str) or not val.strip():
            raise TypeError(f'`migrated["{key}"]` must be a non-empty string.')
        normalized[key] = val.strip()

    return MappingProxyType(normalized)


class Action:
    """QIIME 2 Action"""
    type = 'action'
    _ProvCaptureCls = archive.ActionProvenanceCapture
    _initial_frame_phase = FramePhase.INNER_APPLY

    # Outer wrappers run where an application is requested. Inner wrappers run
    # where its callable is evaluated. Context wrappers enter after the
    # action's wrappers in each stack.
    outer_bind_wrappers = (
        track_results_wrapper,
        deprecation_wrapper,
        bind_signature_wrapper,
    )
    inner_apply_wrappers = (prepare_callable_arguments_wrapper,)

    __call__ = LateBindingAttribute('_dynamic_call')
    asynchronous = LateBindingAttribute('_dynamic_async')
    parallel = LateBindingAttribute('_dynamic_parsl')

    # Private constructor
    @classmethod
    def _init(
        cls,
        callable: FunctionType,
        signature: qtype.MethodSignature,
        plugin_id: str,
        name: str,
        description: str,
        citations: CitationRecord | list[CitationRecord],
        deprecated: bool,
        migrated: bool | dict[str, str],
        examples: dict[str, FunctionType]
    ):
        """

        Parameters
        ----------
        callable : callable
        signature : rachis.core.type.Signature
        plugin_id : str
        name : str
            Human-readable name for this action.
        description : str
            Human-readable description for this action.

        """
        self = cls.__new__(cls)
        self.__init(callable, signature, plugin_id, name, description,
                    citations, deprecated, migrated, examples)
        return self

    # This "extra private" constructor is necessary because `Action` objects
    # can be initialized from a static (classmethod) context or on an
    # existing instance (see `_init` and `__setstate__`, respectively).
    def __init(self, callable, signature, plugin_id, name, description,
               citations, deprecated, migrated, examples):
        self._validate_capture_holders(signature)

        self._callable = callable
        self.signature = signature
        self.plugin_id = plugin_id
        self.name = name
        self.description = description
        self.citations = citations
        self.deprecated = deprecated
        self.migrated = _validate_and_freeze_migrated(migrated)
        self.examples = examples

        self.id = callable.__name__
        self._wrapper_signature = self.signature.to_inspect_signature()
        self._dynamic_call = self._outer_bind(
            lambda: rachis.sdk.Context(self), name='__call__')
        self._dynamic_async = self._outer_bind(
            lambda: rachis.sdk.AsynchronousContext(self), name='asynchronous')
        self._dynamic_parsl = self._get_parsl_wrapper()

    def _validate_capture_holders(self, signature):
        """
        Validates that any CaptureHolder params have their default set
        correctly and errors if not.

        signature : Dict[string: ParamSpec]
            The signature of this action

        Raises : ValueError
            If the default value of a CaptureHolder param is found to not be
            the required default
        """
        for name, spec in signature.parameters.items():
            if spec.view_type is qtype.CaptureHolder and spec.default != \
                    qtype.CaptureHolder.CAPTURE_HOLDER_DEFAULT:
                raise ValueError(
                        f"Default value of CaptureHolder parameter '{name}' "
                        f"is '{spec.default}' should be "
                        f"'{qtype.CaptureHolder.CAPTURE_HOLDER_DEFAULT}'."
                    )

    def __init__(self):
        raise NotImplementedError(
            "%s constructor is private." % self.__class__.__name__)

    @property
    def source(self):
        """
        The source code for the action's callable.

        Returns
        -------
        str
            The source code of this action's callable formatted as Markdown
            text.

        """
        try:
            source = inspect.getsource(self._callable)
        except OSError:
            raise TypeError(
                "Cannot retrieve source code for callable %r" %
                self._callable.__name__)
        return markdown_source_template % {'source': source}

    def get_import_path(self, include_self=True):
        path = f'rachis.plugins.{self.plugin_id}.{self.type}s'
        if include_self:
            path += f'.{self.id}'
        return path

    def __repr__(self):
        return "<%s %s>" % (self.type, self.get_import_path())

    def __getstate__(self):
        return dill.dumps({
            'callable': self._callable,
            'signature': self.signature,
            'plugin_id': self.plugin_id,
            'name': self.name,
            'description': self.description,
            'citations': self.citations,
            'deprecated': self.deprecated,
            'migrated': self.migrated,
            'examples': self.examples,
        })

    def __setstate__(self, state):
        self.__init(**dill.loads(state))

    def _outer_bind(self, context_factory, *, name=None):
        """Bind an action to a context factory and build its callable.

        A registered action is shared across calls. The factory gives each
        application its own context and lets pipelines coordinate nested calls.
        """
        def bound_callable(*args, **kwargs):
            bound = self._wrapper_signature.bind(*args, **kwargs)
            # Include default arguments in the invocation's provenance.
            bound.apply_defaults()
            ctx = context_factory()
            frame = Frame(self, ctx, bound.arguments,
                          phase=self._initial_frame_phase)

            with ExitStack() as wrappers:
                for wrapper in chain(self.outer_bind_wrappers,
                                     ctx.outer_bind_wrappers):
                    wrappers.enter_context(wrapper(frame))
                if frame.results is None:
                    # Dispatch owns the continuation while outer wrappers
                    # observe the caller-facing result on this frame.
                    frame.results = ctx._dispatch_action_(copy.copy(frame))

            return frame.results

        self._set_wrapper_properties(bound_callable)
        self._set_wrapper_name(bound_callable, name or self.id)
        return bound_callable

    def _inner_apply(self, frame):
        """Execute a prepared application in its execution Context."""
        wrappers = chain(self.inner_apply_wrappers,
                         frame.ctx.inner_apply_wrappers)
        with ExitStack() as stack:
            for wrapper in wrappers:
                stack.enter_context(wrapper(frame))
            frame.results = self._callable(**frame.transformed_arguments)
        return frame.results

    def _get_parsl_wrapper(self):
        def parsl_wrapper(*args, **kwargs):
            if not isinstance(self, Pipeline):
                raise ValueError('Only pipelines may be run in parallel')
            with rachis.sdk.ParallelContext(self) as ctx:
                call = self._outer_bind(lambda: ctx)
                results = call(*args, **kwargs)
            return results._result()

        self._set_wrapper_properties(parsl_wrapper)
        self._set_wrapper_name(parsl_wrapper, 'parallel')
        return parsl_wrapper

    def _set_wrapper_name(self, wrapper, name):
        wrapper.__name__ = wrapper.__qualname__ = name

    def _set_wrapper_properties(self, wrapper):
        wrapper.__module__ = self.get_import_path(include_self=False)
        wrapper.__doc__ = self._build_numpydoc()
        wrapper.__annotations__ = self._build_annotations()
        wrapper.__signature__ = self._wrapper_signature

    def _build_annotations(self):
        annotations = {
            name: param.annotation
            for name, param in self._wrapper_signature.parameters.items()}
        annotations['return'] = self._wrapper_signature.return_annotation
        return annotations

    def _build_numpydoc(self):
        numpydoc = []
        numpydoc.append(textwrap.fill(self.name, width=75))
        if self.deprecated:
            base_msg = textwrap.indent(
                textwrap.fill(self._build_deprecation_message(), width=72),
                '   ')
            numpydoc.append('.. deprecated::\n' + base_msg)
        elif self.migrated:
            base_msg = textwrap.indent(
                textwrap.fill(self._build_migration_message(), width=72),
                '   ')
            numpydoc.append('.. migrated::\n' + base_msg)
        numpydoc.append(textwrap.fill(self.description, width=75))

        sig = self.signature
        parameters = self._build_section("Parameters", sig.signature_order)
        returns = self._build_section("Returns", sig.outputs)

        # TODO: include Usage-rendered examples here

        for section in (parameters, returns):
            if section:
                numpydoc.append(section)

        return '\n\n'.join(numpydoc) + '\n'

    def _build_section(self, header, iterable):
        section = []

        if iterable:
            section.append(header)
            section.append('-'*len(header))
            for key, value in iterable.items():
                variable_line = (
                    "{item} : {type}".format(item=key, type=value.qiime_type))
                if value.has_default():
                    variable_line += ", optional"
                section.append(variable_line)
                if value.has_description():
                    section.append(textwrap.indent(textwrap.fill(
                        str(value.description), width=71), '    '))

        return '\n'.join(section).strip()

    def _build_deprecation_message(self):
        return (f'This {self.type.title()} is deprecated and will be removed '
                'in a future version of this plugin.')

    def _build_migration_message(self):
        info = self.migrated

        # required
        to_plugin = info['to_plugin']
        # optional
        from_distro = info.get('from_distro')
        to_distro = info.get('to_distro')
        epoch = info.get('epoch')

        base_msg = f'is slated for migration from the {self.plugin_id} plugin'
        destination = f'to the {to_plugin} plugin'

        if from_distro:
            base_msg += f' of the {from_distro} distribution'

        if to_distro:
            destination += f' of the {to_distro} distribution'

        when = f'in {epoch}' if epoch else 'in a future release'

        return (f'This {self.type.title()} {base_msg} {destination} {when}.')


class Method(Action):
    """Rachis Method"""

    type = 'method'

    inner_apply_wrappers = (
        *Action.inner_apply_wrappers,
        method_output_wrapper,
        provenance_wrapper,
        input_transform_wrapper,
        capture_holders_wrapper,
    )

    @classmethod
    def _init(
        cls,
        callable: FunctionType,
        inputs: dict,
        parameters: dict,
        outputs: dict,
        plugin_id: str,
        name: str,
        description: str,
        input_descriptions: dict[str, str],
        parameter_descriptions: dict[str, str],
        output_descriptions: dict[str, str],
        citations: CitationRecord | list[CitationRecord],
        deprecated: bool,
        migrated: bool | dict[str, str],
        examples: dict[str, FunctionType]
    ):
        signature = qtype.MethodSignature(callable, inputs, parameters,
                                          outputs, input_descriptions,
                                          parameter_descriptions,
                                          output_descriptions)
        if cls is Method and signature.is_mapped:
            from rachis.sdk.mapped import MappedMethod
            cls = MappedMethod
        return super(Method, cls)._init(
            callable, signature, plugin_id, name, description, citations,
            deprecated, migrated, examples)


class Visualizer(Action):
    """Rachis Visualizer"""

    type = 'visualizer'

    inner_apply_wrappers = (
        *Action.inner_apply_wrappers,
        visualization_wrapper,
        provenance_wrapper,
        input_transform_wrapper,
        capture_holders_wrapper,
    )

    @classmethod
    def _init(cls, callable, inputs, parameters, plugin_id, name, description,
              input_descriptions, parameter_descriptions, citations,
              deprecated, migrated, examples):
        signature = qtype.VisualizerSignature(callable, inputs, parameters,
                                              input_descriptions,
                                              parameter_descriptions)
        return super()._init(callable, signature, plugin_id, name, description,
                             citations, deprecated, migrated, examples)


class Pipeline(Action):
    """Rachis Pipeline"""
    type = 'pipeline'
    _ProvCaptureCls = archive.PipelineProvenanceCapture

    # Pipeline inputs are artifacts. Their annotations describe the interface.
    inner_apply_wrappers = (
        *Action.inner_apply_wrappers,
        pipeline_alias_wrapper,
        provenance_wrapper,
        pipeline_call_wrapper,
        capture_holders_wrapper,
    )

    @classmethod
    def _init(cls, callable, inputs, parameters, outputs, plugin_id, name,
              description, input_descriptions, parameter_descriptions,
              output_descriptions, citations, deprecated, migrated, examples):
        signature = qtype.PipelineSignature(callable, inputs, parameters,
                                            outputs, input_descriptions,
                                            parameter_descriptions,
                                            output_descriptions)
        return super()._init(callable, signature, plugin_id, name, description,
                             citations, deprecated, migrated, examples)


markdown_source_template = """
```python
%(source)s
```
"""
