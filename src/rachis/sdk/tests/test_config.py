# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import collections
import importlib
import inspect
import os
import pathlib
import pkgutil
import tempfile
import unittest
import unittest.mock

import parsl
import parsl.data_provider
import parsl.executors
import parsl.launchers
import parsl.monitoring
import parsl.monitoring.radios
import parsl.providers
import tomlkit
from parsl.data_provider.staging import Staging
from parsl.executors.threads import ThreadPoolExecutor
from parsl.monitoring.radios.base import RadioConfig

from rachis import Artifact, Cache

import rachis.util
from rachis.core.util import load_action_yaml
from rachis.core.testing.type import SingleInt
from rachis.core.testing.util import get_dummy_plugin
from rachis.sdk.parallel_config import (PARALLEL_CONFIG, _MASK_CONDA_ENV_,
                                        ParallelConfig,
                                        _get_missing_dependencies,
                                        load_config_from_dict,
                                        load_config_from_file,
                                        optional_dependencies,
                                        python_api_only_options,
                                        tuple_options)


class TestConfig(unittest.TestCase):
    # Get actions
    plugin = get_dummy_plugin()
    pipeline = plugin.pipelines['resumable_pipeline']
    method = plugin.methods['list_of_ints']

    mapping = {
        'dummy_plugin': {
            'list_of_ints': 'tpool',
            'concatenate_ints': 'tpool'
        }
    }

    # Expected provenance based on type of executor used
    default_expected = [{
        'type': 'parsl', 'parsl_type': 'HighThroughputExecutor'}, {
        'type': 'parsl', 'parsl_type': 'HighThroughputExecutor'}]
    tpool_expected = [{
        'type': 'parsl', 'parsl_type': 'ThreadPoolExecutor'}, {
        'type': 'parsl', 'parsl_type': 'ThreadPoolExecutor'}]

    def setUp(self):
        # Create a config whose default executor is a different type than the
        # vendored config's, with an executor the vendored config lacks
        self.test_default = parsl.Config(
            executors=[
                ThreadPoolExecutor(
                    max_threads=1,
                    label='other'
                ),
                parsl.HighThroughputExecutor(
                    max_workers_per_node=1,
                    label='default'
                )
            ],
            # AdHoc Clusters should not be setup with scaling strategy.
            strategy='none',
        )

        # Create temp test dir and cache in dir
        self.test_dir = tempfile.TemporaryDirectory(prefix='rachis-test-temp-')
        self.cache = Cache(os.path.join(self.test_dir.name, 'new_cache'))

        # Create artifacts here so we have unique inputs in each test
        self.art = [Artifact.import_data(SingleInt, 0),
                    Artifact.import_data(SingleInt, 1)]

        # Get paths to config files
        self.mapping_config_fp = self.get_data_path('mapping_config.toml')
        self.mapping_only_config_fp = \
            self.get_data_path('mapping_only_config.toml')
        self.mapping_only_config_old_fp = \
            self.get_data_path('mapping_only_config_old.toml')

    def tearDown(self):
        self.test_dir.cleanup()

        # Ensure default state post test
        PARALLEL_CONFIG.parallel_config = None
        PARALLEL_CONFIG.action_executor_mapping = {}

    def get_data_path(self, filename):
        fp = rachis.util.get_filepath_from_package(
            'rachis.sdk.tests', 'data/parsl_configs/%s' % filename)
        return str(fp)

    def test_default_config(self):
        with ParallelConfig():
            self.assertIsInstance(
                PARALLEL_CONFIG.parallel_config, parsl.Config)
            self.assertEqual(PARALLEL_CONFIG.action_executor_mapping, {})

    def test_mapping_from_config(self):
        config, mapping = load_config_from_file(self.mapping_config_fp)

        self.assertEqual(mapping, self.mapping)

        with self.cache:
            with ParallelConfig(config, mapping):
                future = self.pipeline.parallel(self.art, self.art)
                list_return, dict_return = future._result()

        list_execution_contexts = self._load_alias_execution_contexts(
            list_return)
        dict_execution_contexts = self._load_alias_execution_contexts(
            dict_return)

        self.assertEqual(dict_execution_contexts, self.default_expected)
        self.assertEqual(list_execution_contexts, self.tpool_expected)

    def test_mapping_only_config(self):
        _, mapping = load_config_from_file(self.mapping_only_config_fp)

        self.assertEqual(mapping, self.mapping)

        with self.cache:
            with ParallelConfig(action_executor_mapping=mapping):
                future = self.pipeline.parallel(self.art, self.art)
                list_return, dict_return = future._result()

        list_execution_contexts = self._load_alias_execution_contexts(
            list_return)
        dict_execution_contexts = self._load_alias_execution_contexts(
            dict_return)

        self.assertEqual(dict_execution_contexts, self.default_expected)
        self.assertEqual(list_execution_contexts, self.tpool_expected)

    def test_mapping_from_dict(self):
        with self.cache:
            with ParallelConfig(action_executor_mapping=self.mapping):
                future = self.pipeline.parallel(self.art, self.art)
                list_return, dict_return = future._result()

        list_execution_contexts = self._load_alias_execution_contexts(
            list_return)
        dict_execution_contexts = self._load_alias_execution_contexts(
            dict_return)

        self.assertEqual(dict_execution_contexts, self.default_expected)
        self.assertEqual(list_execution_contexts, self.tpool_expected)

    def test_parallel_configs(self):
        with self.cache:
            with ParallelConfig():
                future = self.pipeline.parallel(self.art, self.art)
                list_return, dict_return = future._result()

            list_execution_contexts = self._load_alias_execution_contexts(
                list_return)
            dict_execution_contexts = self._load_alias_execution_contexts(
                dict_return)

            self.assertEqual(list_execution_contexts, self.default_expected)
            self.assertEqual(dict_execution_contexts, self.default_expected)

            with ParallelConfig(self.test_default):
                future = self.pipeline.parallel(self.art, self.art)
                list_return, dict_return = future._result()

            list_execution_contexts = self._load_alias_execution_contexts(
                list_return)
            dict_execution_contexts = self._load_alias_execution_contexts(
                dict_return)

            self.assertEqual(dict_execution_contexts, self.default_expected)
            self.assertEqual(list_execution_contexts, self.default_expected)

            # At this point we should be using the default config again which
            # does not have an executor called test
            with ParallelConfig(
                    action_executor_mapping={
                        'dummy_plugin': {'list_of_ints': 'test'}}):
                with self.assertRaisesRegex(KeyError, 'test'):
                    future = self.pipeline.parallel(self.art, self.art)
                    list_return, dict_return = future._result()

    def test_nested_configs(self):
        with self.cache:
            with self.assertRaisesRegex(
                    ValueError, 'cannot nest ParallelConfigs'):
                with ParallelConfig():
                    with ParallelConfig(self.test_default):
                        pass

    def test_parallel_non_pipeline(self):
        with self.assertRaisesRegex(
                ValueError, 'Only pipelines may be run in parallel'):
            self.method.parallel(self.art)

    def test_no_vendored_fp(self):
        with _MASK_CONDA_ENV_():
            with ParallelConfig():
                with self.cache:
                    future = self.pipeline.parallel(self.art, self.art)
                    list_return, dict_return = future._result()

            list_execution_contexts = self._load_alias_execution_contexts(
                list_return)
            dict_execution_contexts = self._load_alias_execution_contexts(
                dict_return)

            self.assertEqual(list_execution_contexts, self.default_expected)
            self.assertEqual(dict_execution_contexts, self.default_expected)

    def test_no_config(self):
        with self.assertRaisesRegex(ValueError,
                                    'You must load a parallel config'):
            self.pipeline.parallel(self.art, self.art)

    def test_config_unset(self):
        with ParallelConfig():
            self.pipeline.parallel(self.art, self.art)

        with self.assertRaisesRegex(ValueError,
                                    'You must load a parallel config'):
            self.pipeline.parallel(self.art, self.art)

    def test_concatenate_ints_mapping(self):
        pm = rachis.sdk.PluginManager()
        other_plugin = pm.plugins['other-plugin']

        parameter_only_dummy = self.plugin.pipelines['parameter_only_pipeline']
        parameter_only_other = \
            other_plugin.pipelines['parameter_only_pipeline']

        config, mapping = load_config_from_file(self.mapping_config_fp)

        with self.cache:
            with ParallelConfig(config, mapping):
                _, concate_output_dummy = \
                    parameter_only_dummy.parallel(0)._result()
                _, concate_output_other = \
                    parameter_only_other.parallel(0, other=True)._result()

        dummy_execution_context = \
            self._load_alias_execution_context(concate_output_dummy)
        other_execution_context = \
            self._load_alias_execution_context(concate_output_other)

        self.assertEqual(dummy_execution_context,
                         {'type': 'parsl',
                          'parsl_type': 'ThreadPoolExecutor'})
        self.assertEqual(other_execution_context,
                         {'type': 'parsl',
                          'parsl_type': 'HighThroughputExecutor'})

    def test_old_mapping(self):
        with self.assertRaisesRegex(TypeError,
                                    'The provided mapping:\n\n'
                                    "{'list_of_ints': 'tpool'}"):
            load_config_from_file(self.mapping_only_config_old_fp)

    def _load_alias_execution_contexts(self, collection):
        execution_contexts = []

        for result in collection.values():
            execution_context = self._load_alias_execution_context(result)
            execution_contexts.append(execution_context)

        return execution_contexts

    def _load_alias_execution_context(self, result):
        alias_uuid = load_action_yaml(
            result._archiver.path)['action']['alias-of']
        return load_action_yaml(
            self.cache.data / alias_uuid)['execution']['execution_context']


# The installed parsl's version, for options added after the oldest parsl we
# support
PARSL_VERSION = tuple(int(part) for part in parsl.__version__.split('.')[:3])


class TestParslConfigParsing(unittest.TestCase):
    """Parses the configs in data/parsl_configs, which between them use every
    option of every parsl class that can be written in TOML, so we notice when
    a parsl release changes its config in a way our parser cannot handle.

    The configs are only parsed into parsl objects, never loaded by parsl, so
    nothing is ever submitted to a scheduler or cloud provider.
    """
    # Keys in the parsl table that are used by rachis and not passed to parsl
    RACHIS_OPTIONS = {'debug', 'executor_mapping'}

    # Options parsl rewrites when it is given them, so the attribute on the
    # parsl object will not equal the value in the config. For example,
    # SlurmProvider appends the partition to scheduler_options
    REWRITTEN_OPTIONS = {'block_error_handler', 'environment',
                         'scheduler_options', 'worker_init'}

    def setUp(self):
        self.config_dir = pathlib.Path(str(
            rachis.util.get_filepath_from_package(
                'rachis.sdk.tests', 'data/parsl_configs')))

    def tearDown(self):
        PARALLEL_CONFIG.debug = False
        PARALLEL_CONFIG.parallel_config = None
        PARALLEL_CONFIG.action_executor_mapping = {}

    def test_every_parsl_option_is_in_a_config(self):
        classes = [parsl.Config, parsl.monitoring.MonitoringHub]
        for module in (parsl.executors, parsl.providers, parsl.launchers):
            classes.extend(getattr(module, name)
                           for name in dict.fromkeys(module.__all__))
        # These are spread across their packages' modules with no list of them
        classes.extend(self._find_subclasses(parsl.data_provider, Staging))
        classes.extend(
            self._find_subclasses(parsl.monitoring.radios, RadioConfig))

        used_options = collections.defaultdict(set)
        for fp in self.config_dir.glob('*.toml'):
            self._collect_options(
                self._read_config(fp)['parsl'], 'Config', used_options)

        missing = {}
        for cls in classes:
            name = cls.__name__
            if name not in used_options:
                missing[name] = 'class is not used'
                continue

            options = {
                param.name
                for param in inspect.signature(cls).parameters.values()
                if param.kind not in (param.VAR_POSITIONAL,
                                      param.VAR_KEYWORD)}
            # These are covered by test_python_api_only_options
            unused = (options - used_options[name]
                      - python_api_only_options.get(name, set()))
            if unused:
                missing[name] = sorted(unused)

        self.assertEqual(
            missing, {}, 'These parsl classes or options are not used by any '
            'config in data/parsl_configs. Add them to one to test that they '
            'parse.')

    def test_config_options(self):
        self._load_config('config_options.toml')

    def test_strategy_none(self):
        config, _ = load_config_from_file(
            self.config_dir / 'test_config.toml')

        # The config says 'None', but parsl only accepts 'none'
        self.assertEqual(config.strategy, 'none')

    def test_thread_pool_executor(self):
        self._load_config('thread_pool_executor.toml')

    def test_high_throughput_executor(self):
        self._load_config('high_throughput_executor.toml')

    def test_mpi_executor(self):
        self._load_config('mpi_executor.toml')

    def test_flux_executor(self):
        # FluxExecutor registers a finalizer that joins a thread parsl only
        # starts when the executor is loaded, so it raises at exit otherwise
        with unittest.mock.patch('weakref.finalize'):
            self._load_config('flux_executor.toml')

    def test_providers(self):
        self._load_config('providers.toml')

    def test_launchers(self):
        self._load_config('launchers.toml')

    # Nox was running my tests with parsl 2026.01.19 when my conda env had
    # 2026.02.23, so we need to skip this test when parsl is too old.
    @unittest.skipIf(PARSL_VERSION < (2026, 2, 23),
                     'SlurmProvider gained gpus_per_node and gres in parsl '
                     '2026.02.23')
    def test_slurm_provider_gpus(self):
        self._load_config('slurm_provider_gpus.toml')

    def test_storage_access(self):
        self._load_config('storage_access.toml')

    def test_remote_monitoring_radio(self):
        self._load_config('remote_monitoring_radio.toml')

    def test_unknown_class(self):
        with self.assertRaisesRegex(
                AttributeError, "parsl class named 'NotAStagingProvider' for "
                "'storage_access'. Looked in: parsl.data_provider"):
            load_config_from_dict({'parsl': {'executors': [{
                'class': 'ThreadPoolExecutor',
                'storage_access': [{'class': 'NotAStagingProvider'}]}]}})

    def test_debug(self):
        self._load_config('debug.toml')
        self.assertTrue(PARALLEL_CONFIG.debug)

    def test_missing_optional_dependency(self):
        with unittest.mock.patch.dict(
                optional_dependencies,
                {'ThreadPoolExecutor': ['os', 'rachis_missing_module']}):
            with self.assertRaisesRegex(
                    ImportError, 'ThreadPoolExecutor requires '
                    'rachis_missing_module, which could not be imported.*'
                    'not included in the QIIME 2 conda environments'):
                self._load_config('thread_pool_executor.toml')

    def test_missing_optional_dependency_bad_option(self):
        # We check the options are valid even when we cannot build the class
        with unittest.mock.patch.dict(
                optional_dependencies,
                {'ThreadPoolExecutor': ['rachis_missing_module']}):
            with self.assertRaisesRegex(
                    TypeError, 'ThreadPoolExecutor: .*not_an_option'):
                load_config_from_dict({'parsl': {'executors': [
                    {'class': 'ThreadPoolExecutor', 'not_an_option': 1}]}})

    def test_installed_optional_dependency(self):
        with unittest.mock.patch.dict(
                optional_dependencies, {'ThreadPoolExecutor': ['os']}):
            self._load_config('thread_pool_executor.toml')

    def test_python_api_only_options(self):
        for option in python_api_only_options['Config']:
            with self.subTest(option=option):
                with self.assertRaisesRegex(
                        ValueError, f"^The '{option}' option of Config cannot "
                        'be parsed from a TOML config, so it is only '
                        'supported through the Python API'):
                    load_config_from_dict(
                        {'parsl': {option: 'my_module.my_function'}})

    def test_python_api_only_installed_dependency(self):
        with unittest.mock.patch.dict(
                optional_dependencies, {'TaskVineExecutor': ['os']}):
            self._assert_python_api_only(
                'task_vine_executor.toml', 'TaskVineExecutor')

    # The following use classes that need optional dependencies of parsl, so
    # loading them raises an ImportError when those are not installed
    def test_monitoring(self):
        # Requires the optional dependency sqlalchemy, which we do not have in
        # our conda environments at this time
        self._load_optional_config('monitoring.toml', 'MonitoringHub')

    def test_globus_staging(self):
        # Requires the optional dependency globus-sdk, which we do not have in
        # our conda environments at this time
        self._load_optional_config('globus_staging.toml', 'GlobusStaging')

    def test_work_queue_executor(self):
        # Requires the optional dependency ndcctools, which we do not have in
        # our conda environments at this time
        self._load_optional_config(
            'work_queue_executor.toml', 'WorkQueueExecutor')

    def test_task_vine_executor(self):
        # Requires the optional dependency ndcctools, which we do not have in
        # our conda environments at this time. We also cannot parse it from
        # TOML, so it is only supported through the Python API
        self._assert_python_api_only(
            'task_vine_executor.toml', 'TaskVineExecutor')

    def test_radical_pilot_executor(self):
        # Requires the optional dependency radical.pilot, which we do not have
        # in our conda environments at this time. We also cannot parse it from
        # TOML, so it is only supported through the Python API
        self._assert_python_api_only(
            'radical_pilot_executor.toml', 'RadicalPilotExecutor')

    def test_globus_compute_executor(self):
        # Requires the optional dependency globus-compute-sdk, which we do not
        # have in our conda environments at this time. We also cannot parse it
        # from TOML, so it is only supported through the Python API
        self._assert_python_api_only(
            'globus_compute_executor.toml', 'GlobusComputeExecutor')

    @unittest.skipUnless(_get_missing_dependencies('AWSProvider'),
                         'AWSProvider contacts AWS when boto3 is installed')
    def test_aws_provider(self):
        # Requires the optional dependency boto3, which we do not have in our
        # conda environments at this time
        self._load_optional_config('aws_provider.toml', 'AWSProvider')

    @unittest.skipUnless(_get_missing_dependencies('AzureProvider'),
                         'AzureProvider contacts Azure when its dependencies '
                         'are installed')
    def test_azure_provider(self):
        # Requires the optional Azure SDK dependencies, which we do not have in
        # our conda environments at this time
        self._load_optional_config('azure_provider.toml', 'AzureProvider')

    @unittest.skipUnless(_get_missing_dependencies('GoogleCloudProvider'),
                         'GoogleCloudProvider contacts Google Cloud when '
                         'google-api-python-client is installed')
    def test_google_cloud_provider(self):
        # Requires the optional dependency google-api-python-client, which we
        # do not have in our conda environments at this time
        self._load_optional_config(
            'google_cloud_provider.toml', 'GoogleCloudProvider')

    @unittest.skipUnless(_get_missing_dependencies('KubernetesProvider'),
                         'KubernetesProvider reads the cluster config when '
                         'kubernetes is installed')
    def test_kubernetes_provider(self):
        # Requires the optional dependency kubernetes, which we do not have in
        # our conda environments at this time
        self._load_optional_config(
            'kubernetes_provider.toml', 'KubernetesProvider')

    def assertParslObjectMatches(self, obj, table):
        """Asserts that obj is of the class named in the config table and has
        each option's value from the table as an attribute
        """
        if 'class' in table:
            self.assertEqual(type(obj).__name__, table['class'])

        for option, value in table.items():
            if option == 'class' or option in self.REWRITTEN_OPTIONS:
                continue

            attr = getattr(obj, option)
            if self._is_parsl_object(value):
                self.assertParslObjectMatches(attr, value)
            elif isinstance(value, list) and value and \
                    all(self._is_parsl_object(item) for item in value):
                self.assertEqual(len(attr), len(value))
                for attr_item, item in zip(attr, value):
                    self.assertParslObjectMatches(attr_item, item)
            else:
                if option in tuple_options:
                    value = tuple_options[option](value)
                self.assertEqual(attr, value,
                                 f'{type(obj).__name__}.{option}')

    def _load_config(self, filename):
        fp = self.config_dir / filename
        table = {key: value
                 for key, value in self._read_config(fp)['parsl'].items()
                 if key not in self.RACHIS_OPTIONS}

        config, _ = load_config_from_file(fp)
        self.assertParslObjectMatches(config, table)

    def _load_optional_config(self, filename, class_name):
        """Loads a config that uses a class with optional dependencies. If they
        are not installed, asserts that we raise our ImportError, which we only
        do after checking that the class accepts the options in the config
        """
        if not _get_missing_dependencies(class_name):
            self._load_config(filename)
            return

        with self.assertRaisesRegex(
                ImportError, f'^{class_name} requires .* not included in the '
                'QIIME 2 conda environments'):
            self._load_config(filename)

    def _assert_python_api_only(self, filename, class_name):
        """Asserts that loading the config raises an error saying the class is
        only supported through the Python API. If the class's dependencies are
        not installed, this is part of our ImportError about them
        """
        if _get_missing_dependencies(class_name):
            error = ImportError
        else:
            error = ValueError

        with self.assertRaisesRegex(
                error, f'^{class_name} .*only supported through the Python '
                'API by passing an instantiated parsl.Config'):
            self._load_config(filename)

    def _read_config(self, fp):
        with open(fp) as fh:
            return tomlkit.load(fh).unwrap()

    def _collect_options(self, table, class_name, used_options):
        used_options[class_name].update(table.keys() - {'class'})
        for value in table.values():
            for item in value if isinstance(value, list) else [value]:
                if self._is_parsl_object(item):
                    self._collect_options(item, item['class'], used_options)

    def _find_subclasses(self, package, base):
        """Finds the classes in any of package's modules that subclass base
        """
        for module in pkgutil.iter_modules(package.__path__):
            try:
                importlib.import_module(f'{package.__name__}.{module.name}')
            except ImportError:
                pass

        subclasses = []
        to_search = [base]
        while to_search:
            for cls in to_search.pop().__subclasses__():
                if cls.__module__.startswith(package.__name__):
                    subclasses.append(cls)
                to_search.append(cls)

        return subclasses

    def _is_parsl_object(self, value):
        return isinstance(value, dict) and 'class' in value
