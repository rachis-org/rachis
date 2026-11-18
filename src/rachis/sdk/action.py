# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import abc
import inspect
import tempfile
import textwrap
from types import FunctionType

import dill

from typing import Mapping, TypedDict, Union
from types import MappingProxyType

import rachis.sdk
from rachis.sdk.result import Artifact, ResultCollection, ChecksumCache
import rachis.core.type as qtype
import rachis.core.archive as archive
from rachis.core.util import (LateBindingAttribute, tuplize,
                              create_collection_name)
from rachis.core.cite import CitationRecord


def _coerce_pipeline_outputs(ctx, outputs):
    """Ensure all futures are resolved and all collections are of type
       ResultCollection
    """
    coerced_outputs = []

    for output in outputs:
        # Ensure collection outputs are ResultCollection
        if isinstance(output, dict) or isinstance(output, list):
            output = rachis.sdk.ResultCollection(output)

        # Handle proxy outputs if root
        if ctx._parent is None and output is not None:
            output = output.result()

            # Handle proxies as elements of collections
            if isinstance(output, rachis.sdk.ResultCollection):
                for key, value in output.items():
                    output[key] = value.result()

        coerced_outputs.append(output)

    return tuple(coerced_outputs)


def _intersect_pipeline_output_type(expected, output_type, output_name):
    refined_type = output_type & expected

    if (not refined_type.is_bottom()) and refined_type.is_concrete():
        return refined_type

    raise TypeError(
        "Expected output %s to be of type %r, received %r"
        % (output_name, expected, output_type))


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


class Action(metaclass=abc.ABCMeta):
    """QIIME 2 Action"""
    type = 'action'
    _ProvCaptureCls = archive.ActionProvenanceCapture

    __call__ = LateBindingAttribute('_dynamic_call')
    asynchronous = LateBindingAttribute('_dynamic_async')
    parallel = LateBindingAttribute('_dynamic_parsl')

    # Executes a callable on the provided `view_args`, wrapping and returning
    # the callable's outputs. In other words, executes the "view API", wrapping
    # and returning the outputs as the "artifact API". `view_args` is a dict
    # mapping parameter name to unwrapped value (i.e. view). `view_args`
    # contains an entry for each parameter accepted by the wrapper. It is the
    # executor's responsibility to perform any additional transformations on
    # these parameters, or provide extra parameters, in order to execute the
    # callable. `output_types` is an OrderedDict mapping output name to QIIME
    # type (e.g. semantic type).
    @abc.abstractmethod
    def _callable_executor_(self, ctx, view_args, output_types):
        raise NotImplementedError

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
        self._dynamic_call = self._callable_action_wrapper()
        self._dynamic_async = self._get_async_wrapper()
        # This a temp thing to play with parsl before integrating more deeply
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

    def _bind(self, context_factory, execution_ctx={'type': 'synchronous'}):
        """Bind an action to a Context factory, returning a decorated function.

        This is a very primitive API and should be used primarily by the
        framework and very advanced interfaces which need deep control over
        the calling semantics of pipelines and garbage collection.

        The basic idea behind this is outlined as follows:

        Every action is defined as an *instance* that a plugin constructs.
        This means that `self` represents the internal details as to what
        the action is. If you need to associate additional state with the
        *application* of an action, you cannot mutate `self` without
        changing all future applications. So there needs to be an
        additional instance variable that can serve as the state of a given
        application. We call this a Context object. It is also important
        that each application of an action has *independent* state, so
        providing an instance of Context won't work. We need a factory.

        Parameterizing the context is necessary because it is possible for
        an action to call other actions. The details need to be coordinated
        behind the scenes to the user, so we can parameterize the behavior
        by providing different context factories to `bind` at different
        points in the "call stack".

        """
        def bound_callable(*args, **kwargs):
            ctx = context_factory()
            if ctx._record_provenance:
                provenance = self._ProvCaptureCls(
                    self.type, self.plugin_id, self.id, execution_ctx)
            else:
                provenance = archive.NoOpProvenanceCapture()

            if self.deprecated:
                with rachis.core.util.warning() as warn:
                    warn(self._build_deprecation_message(), FutureWarning)

            if self.migrated:
                with rachis.core.util.warning() as warn:
                    warn(self._build_migration_message(), FutureWarning)

            # Type management
            collated_inputs = self.signature.collate_inputs(*args, **kwargs)
            self.signature.check_types(**collated_inputs)
            output_types = self.signature.solve_output(**collated_inputs)
            callable_args = self.signature.coerce_user_input(**collated_inputs)

            # validate and cache checksums
            checksum_cache = ChecksumCache()
            for input in collated_inputs.values():
                if isinstance(input, Artifact):
                    checksum_cache.cache_artifact(input)
                elif isinstance(input, ResultCollection):
                    checksum_cache.cache_result_collection(input)

            callable_args, captures = \
                self.signature.transform_and_add_callable_args_to_prov(
                    provenance, **callable_args)

            outputs = self._callable_executor_(
                ctx, callable_args, output_types, provenance)

            self._ensure_captures_set(captures)

            if len(outputs) != len(self.signature.outputs):
                raise ValueError(
                    "Number of callable outputs must match number of "
                    "outputs defined in signature: %d != %d" %
                    (len(outputs), len(self.signature.outputs)))

            for output in outputs:
                output._record_provenance = ctx._record_provenance

            # Wrap in a Results object mapping output name to value so
            # users have access to outputs by name or position.
            results = rachis.sdk.Results(
                self.signature.outputs.keys(), outputs)

            return results

        bound_callable = self._rewrite_wrapper_signature(bound_callable)
        self._set_wrapper_properties(bound_callable)
        self._set_wrapper_name(bound_callable, self.id)
        return bound_callable

    def _ensure_captures_set(self, captures):
        '''
        Ensure all capture parameters have a value set. Either passed in by the
        caller or captured in the Action.

        captures : List[Str]
            List of all params that are CaptureHolders

        Raises : ValueError
            If the CaptureHolder never had a value set
        '''
        for capture in captures:
            if not capture.is_set:
                raise ValueError(
                    f"The capture parameter '{capture._name}' never had a "
                    "value set for it"
                )

    def _callable_action_wrapper(self):
        # This is a "root" level invocation (not a nested call within a
        # pipeline), so no special factory is needed.
        callable_wrapper = self._bind(lambda: rachis.sdk.Context(self))
        self._set_wrapper_name(callable_wrapper, '__call__')
        return callable_wrapper

    def _get_async_wrapper(self):
        def async_wrapper(*args, **kwargs):
            return rachis.sdk.AsynchronousContext(self)._dispatch_(
                *args, **kwargs)

        async_wrapper = self._rewrite_wrapper_signature(async_wrapper)
        self._set_wrapper_properties(async_wrapper)
        self._set_wrapper_name(async_wrapper, 'asynchronous')
        return async_wrapper

    def _get_parsl_wrapper(self):
        def parsl_wrapper(*args, **kwargs):
            # TODO: Maybe make this a warning instead?
            if not isinstance(self, Pipeline):
                raise ValueError('Only pipelines may be run in parallel')

            # TODO: could call callable_action here instead and do index check
            return rachis.sdk.ParallelContext(self)._dispatch_(*args, **kwargs)

        parsl_wrapper = self._rewrite_wrapper_signature(parsl_wrapper)
        self._set_wrapper_properties(parsl_wrapper)
        self._set_wrapper_name(parsl_wrapper, 'parsl')
        return parsl_wrapper

    def _rewrite_wrapper_signature(self, wrapper):
        def signature_wrapper(*args, **kwargs):
            bound = self._wrapper_signature.bind(*args, **kwargs)
            # ensures that provenance has all default arguments.
            bound.apply_defaults()
            return wrapper(*bound.args, **bound.kwargs)

        return signature_wrapper

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

    # Abstract method implementations:

    def _callable_executor_(self, ctx, view_args, output_types, provenance):
        output_views = self._callable(**view_args)
        output_views = tuplize(output_views)

        # TODO this won't work if the user has annotated their "view API" to
        # return a `typing.Tuple` with some number of components. Python will
        # return a tuple when there are multiple return values, and this length
        # check will fail because the tuple as a whole should be matched up to
        # a single output type instead of its components. This is an edgecase
        # due to how Python handles multiple returns, and can be worked around
        # by using something like `typing.List` instead.
        if len(output_views) != len(output_types):
            raise TypeError(
                "Number of output views must match number of output "
                "semantic types: %d != %d"
                % (len(output_views), len(output_types)))

        output_artifacts = \
            self.signature.coerce_given_outputs(output_views, output_types,
                                                ctx, provenance)

        return tuple(output_artifacts)

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

    # Abstract method implementations:

    def _callable_executor_(self, ctx, view_args, output_types, provenance):
        with tempfile.TemporaryDirectory(prefix='rachis-temp-') as temp_dir:
            ret_val = self._callable(output_dir=temp_dir, **view_args)
            if ret_val is not None:
                raise TypeError(
                    "Visualizer %r should not return anything. "
                    "Received %r as a return value." % (self, ret_val))
            provenance.output_name = 'visualization'
            viz = rachis.sdk.Visualization._from_data_dir(temp_dir,
                                                          provenance)
            viz = ctx.add_reference(viz)

            return (viz, )

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

    def _callable_executor_(self, ctx, view_args, output_types, provenance):
        outputs = self._callable(ctx, **view_args)
        # Just make sure we have an iterable even if there was only one output
        outputs = tuplize(outputs)

        # Make sure any collections returned are in the form of
        # ResultCollections and resolve proxies if this is the root pipeline
        outputs = _coerce_pipeline_outputs(ctx, outputs)

        # This condition *is* tested by the caller of _callable_executor_, but
        # the kinds of errors a plugin developer see will make more sense if
        # this check happens before the subtype check. Otherwise forgetting an
        # output would more likely error as a wrong type, which while correct,
        # isn't root of the problem.
        if len(outputs) != len(output_types):
            raise TypeError(
                "Number of outputs must match number of output "
                "semantic types: %d != %d"
                % (len(outputs), len(output_types)))

        message = "Pipelines must return `Result` objects, not %s"
        for output in outputs:
            if isinstance(output, rachis.sdk.ResultCollection):
                for elem in output.values():
                    if not isinstance(elem, rachis.sdk.IResult):
                        raise TypeError(message % type(elem))
            elif not isinstance(output, rachis.sdk.IResult):
                raise TypeError(message % type(output))

        results = []

        # If we don't have a Collection, we should have a Result, if we
        # have neither, or our types can't be reconciled, something bad
        # happened
        for output, (name, spec) in zip(outputs, output_types.items()):
            if spec.qiime_type.name == 'Collection':
                size = len(output)
                aliased_output = rachis.sdk.ResultCollection()
                element_type = spec.qiime_type.fields[0]

                for idx, (key, value) in enumerate(output.items()):
                    collection_name = create_collection_name(
                        name=name, key=key, idx=idx, size=size)
                    refined_type = _intersect_pipeline_output_type(
                        element_type, value.type, "%r member %r" % (name, key))
                    aliased_result = \
                        value._alias(collection_name, provenance, ctx,
                                     refined_type)

                    aliased_output[str(key)] = aliased_result
                results.append(aliased_output)
            else:
                refined_type = _intersect_pipeline_output_type(
                    spec.qiime_type, output.type, repr(name))
                aliased_result = output._alias(
                    name, provenance, ctx, refined_type)

                results.append(aliased_result)

        if len(results) != len(self.signature.outputs):
            raise ValueError(
                "Number of callable outputs must match number of "
                "outputs defined in signature: %d != %d" %
                (len(results), len(self.signature.outputs)))

        return tuple(results)

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
