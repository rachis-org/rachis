# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import inspect
import tempfile
import textwrap
from contextlib import contextmanager, ExitStack
from enum import Enum
from functools import cached_property
from itertools import chain
from types import FunctionType

import dill

from typing import Mapping, TypedDict, Union
from types import MappingProxyType

import rachis.sdk
from rachis.sdk.iresult import IResult
from rachis.sdk.proxy import ProxyResultCollection
from rachis.sdk.result import Artifact, ResultCollection, ChecksumCache
import rachis.core.type as qtype
import rachis.core.archive as archive
from rachis.core.util import (LateBindingAttribute, tuplize,
                              create_collection_name)
from rachis.core.cite import CitationRecord


class FramePhase(Enum):
    OUTER_APPLY = 'outer_apply'


class Frame:
    """State for one application phase, shared by its wrapper stack."""

    def __init__(self, action, ctx, bound_arguments, *,
                 phase=FramePhase.OUTER_APPLY):
        self.action = action
        self.ctx = ctx
        self.bound_arguments = bound_arguments
        self.phase = phase
        self.call_kwargs = {}
        self.results = None
        self.provenance = None
        self.captures = ()

    def __call__(self):
        match self.phase:
            case FramePhase.OUTER_APPLY:
                return self.action._outer_apply(self)
        raise ValueError(f'Unknown frame phase: {self.phase!r}')

    @cached_property
    def output_types(self):
        """Solve output types from the bound inputs when first needed."""
        return self.action.signature.solve_output(**self.bound_arguments)

    def for_worker(self):
        """Copy a dispatchable phase's inputs into a fresh worker frame."""
        return Frame(self.action, self.ctx, self.bound_arguments,
                     phase=self.phase)


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
def signature_wrapper(frame):
    signature = frame.action.signature
    signature.check_types(**frame.bound_arguments)
    frame.output_types = signature.solve_output(**frame.bound_arguments)
    frame.call_kwargs = signature.coerce_user_input(**frame.bound_arguments)
    yield


@contextmanager
def provenance_wrapper(frame):
    action = frame.action
    frame.provenance = action._ProvCaptureCls(
        action.type, action.plugin_id, action.id, frame.ctx.execution_ctx)
    yield


@contextmanager
def checksum_wrapper(frame):
    cache = ChecksumCache()
    for value in frame.bound_arguments.values():
        if isinstance(value, Artifact):
            cache.cache_artifact(value)
        elif isinstance(value, ResultCollection):
            cache.cache_result_collection(value)
    yield


@contextmanager
def input_transform_wrapper(frame):
    frame.call_kwargs, frame.captures = (
        frame.action.signature.transform_and_add_callable_args_to_prov(
            frame.provenance, **frame.call_kwargs))
    yield


def checked_return_values(value, expected):
    values = tuplize(value)
    actual = len(values)
    if actual != expected:
        raise TypeError(
            "Number of returned values must match number of outputs: "
            f"{actual} != {expected}")
    return values


@contextmanager
def method_wrapper(frame):
    yield
    views = checked_return_values(frame.results, len(frame.output_types))
    frame.results = tuple(frame.action.signature.coerce_given_outputs(
        views, frame.output_types, frame.ctx, frame.provenance))


@contextmanager
def capture_wrapper(frame):
    yield
    for capture in frame.captures:
        if not capture.is_set:
            raise ValueError(
                f"The capture parameter '{capture._name}' never had a "
                "value set for it"
            )


@contextmanager
def results_wrapper(frame):
    yield
    frame.results = rachis.sdk.Results(
        frame.action.signature.outputs.keys(), frame.results)


@contextmanager
def visualization_wrapper(frame):
    with tempfile.TemporaryDirectory(prefix='rachis-temp-') as temp_dir:
        frame.call_kwargs['output_dir'] = temp_dir
        yield
        if frame.results is not None:
            raise TypeError(
                "Visualizer %r should not return anything. "
                "Received %r as a return value."
                % (frame.action, frame.results))
        frame.provenance.output_name = 'visualization'
        visualization = rachis.sdk.Visualization._from_data_dir(
            temp_dir, frame.provenance)
        frame.results = (frame.ctx.add_reference(visualization),)


def _coerce_pipeline_outputs(ctx, outputs):
    """Validate results, resolving them at the root pipeline boundary."""
    resolve = ctx._parent is None

    def coerce_result(value):
        if resolve and isinstance(value, IResult):
            value = value.result()
        if not isinstance(value, IResult):
            raise TypeError(f'Pipelines must return `Result` objects, '
                            f'not {type(value)}')
        return value

    coerced_outputs = []
    for output in outputs:
        if resolve and isinstance(output, ProxyResultCollection):
            output = output.result()
        if isinstance(output, (dict, list)):
            output = ResultCollection(output)
        if isinstance(output, ResultCollection):
            for key, value in output.items():
                value = coerce_result(value)
                if resolve:
                    output[key] = value
        else:
            output = coerce_result(output)
        coerced_outputs.append(output)

    return tuple(coerced_outputs)


def _intersect_pipeline_output_type(expected, output_type, output_name):
    refined_type = output_type & expected

    if (not refined_type.is_bottom()) and refined_type.is_concrete():
        return refined_type

    raise TypeError(
        "Expected output %s to be of type %r, received %r"
        % (output_name, expected, output_type))


@contextmanager
def pipeline_wrapper(frame):
    frame.call_kwargs['ctx'] = frame.ctx
    yield
    outputs = checked_return_values(frame.results, len(frame.output_types))
    outputs = _coerce_pipeline_outputs(frame.ctx, outputs)

    results = []
    for output, (name, spec) in zip(outputs, frame.output_types.items()):
        if spec.qiime_type.name == 'Collection':
            size = len(output)
            aliased_output = ResultCollection()
            element_type = spec.qiime_type.fields[0]
            for idx, (key, value) in enumerate(output.items()):
                collection_name = create_collection_name(name=name, key=key,
                                                         idx=idx, size=size)
                refined_type = _intersect_pipeline_output_type(
                    element_type, value.type, "%r member %r" % (name, key))
                aliased_output[str(key)] = value._alias(
                    collection_name, frame.provenance, frame.ctx, refined_type)
            results.append(aliased_output)
        else:
            refined_type = _intersect_pipeline_output_type(
                spec.qiime_type, output.type, repr(name))
            results.append(output._alias(
                name, frame.provenance, frame.ctx, refined_type))
    frame.results = tuple(results)


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

    # Outer wrappers run where an application is requested. Inner wrappers run
    # where its callable is evaluated. Context wrappers enclose both stacks.
    outer_wrappers = (deprecation_wrapper,)
    inner_wrappers = (
        signature_wrapper,
        provenance_wrapper,
        checksum_wrapper,
        results_wrapper,
        capture_wrapper,
    )

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
        """Build the callable that enters the outer stack and dispatches."""
        def bound_callable(*args, **kwargs):
            bound = self._wrapper_signature.bind(*args, **kwargs)
            bound.apply_defaults()
            ctx = context_factory()
            frame = Frame(self, ctx, bound.arguments)

            with ExitStack() as wrappers:
                for wrapper in chain(ctx.outer_wrappers, self.outer_wrappers):
                    wrappers.enter_context(wrapper(frame))
                if frame.results is None:
                    frame.results = ctx._dispatch_action_(frame)

            # Post-processing may replace results while the stack unwinds.
            return frame.results

        self._set_wrapper_properties(bound_callable)
        self._set_wrapper_name(bound_callable, name or self.id)
        return bound_callable

    def _outer_apply(self, frame):
        return self._inner_apply(frame)

    def _inner_apply(self, frame):
        """Execute a prepared application in its execution Context."""
        wrappers = chain(frame.ctx.inner_wrappers, self.inner_wrappers)
        with ExitStack() as stack:
            for wrapper in wrappers:
                stack.enter_context(wrapper(frame))
            frame.results = self._callable(**frame.call_kwargs)
        return frame.results

    def _get_parsl_wrapper(self):
        def context_factory():
            if not isinstance(self, Pipeline):
                raise ValueError('Only pipelines may be run in parallel')
            return rachis.sdk.ParallelContext(self)

        return self._outer_bind(context_factory, name='parsl')

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

    inner_wrappers = (
        *Action.inner_wrappers,
        input_transform_wrapper,
        method_wrapper,
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
        return super()._init(callable, signature, plugin_id, name, description,
                             citations, deprecated, migrated, examples)


class Visualizer(Action):
    """Rachis Visualizer"""

    type = 'visualizer'

    inner_wrappers = (
        *Action.inner_wrappers,
        input_transform_wrapper,
        visualization_wrapper,
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

    inner_wrappers = (
        *Action.inner_wrappers,
        input_transform_wrapper,
        pipeline_wrapper,
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
