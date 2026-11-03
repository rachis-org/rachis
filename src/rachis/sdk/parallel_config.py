# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import os
import copy
import psutil
import platformdirs
import threading
import importlib
import inspect

import parsl
import tomlkit

import logging


# Stores info about the currently loaded parallel config
PARALLEL_CONFIG = threading.local()
PARALLEL_CONFIG.dfk = None
PARALLEL_CONFIG.debug = False
PARALLEL_CONFIG.parallel_config = None
PARALLEL_CONFIG.action_executor_mapping = {}

# We write a default config to a location in the conda env if there is an
# active conda env. If there is not an active conda env (most likely because we
# are using Docker) then the path we want to write the default to will not
# exist, so we will not write a default, we will just load it from memory
CONDA_PREFIX = os.environ.get('CONDA_PREFIX', '')
VENDORED_FP = os.path.join(CONDA_PREFIX, 'etc', 'qiime2_config.toml')

VENDORED_CONFIG = {
    'parsl': {
        'strategy': 'None',
        'executors': [
            {'class': 'ThreadPoolExecutor', 'label': 'tpool',
                'max_threads': max(psutil.cpu_count() - 1, 1)},
            {'class': 'HighThroughputExecutor', 'label': 'default',
                'max_workers_per_node': max(psutil.cpu_count() - 1, 1),
                'provider': {'class': 'LocalProvider'}}
            ]
        }
    }

# Directs keys in the config whose values need to be objects to the module that
# contains the class they need to instantiate
PARSL_DATA_PROVIDER = 'parsl.data_provider'
PARSL_EXECUTOR = 'parsl.executors'
PARSL_LAUNCHER = 'parsl.launchers'
PARSL_MANAGER_SELECTOR = 'parsl.executors.high_throughput.manager_selector'
PARSL_MEMOIZER = 'parsl.dataflow.memoization'
PARSL_MONITORING = 'parsl.monitoring'
PARSL_PROVIDER = 'parsl.providers'
PARSL_RADIO = ('parsl.monitoring.radios.filesystem',
               'parsl.monitoring.radios.htex',
               'parsl.monitoring.radios.multiprocessing',
               'parsl.monitoring.radios.udp')
PARSL_STAGING = ('parsl.data_provider.file_noop', 'parsl.data_provider.ftp',
                 'parsl.data_provider.globus', 'parsl.data_provider.http',
                 'parsl.data_provider.rsync', 'parsl.data_provider.zip')
module_paths = {
    'executor': PARSL_EXECUTOR,
    'executors': PARSL_EXECUTOR,
    'launcher': PARSL_LAUNCHER,
    'launchers': PARSL_LAUNCHER,
    'manager_selector': PARSL_MANAGER_SELECTOR,
    'memoizer': PARSL_MEMOIZER,
    'monitoring': PARSL_MONITORING,
    'provider': PARSL_PROVIDER,
    'providers': PARSL_PROVIDER,
    'remote_monitoring_radio': PARSL_RADIO,
    'storage_access': PARSL_STAGING,
}

# parsl classes that need optional dependencies which are not in the QIIME 2
# conda environments at this time. Maps each class to the modules it imports
optional_dependencies = {
    'AWSProvider': ['boto3'],
    'AzureProvider': ['azure.common', 'azure.mgmt.compute',
                      'azure.mgmt.network', 'azure.mgmt.resource',
                      'msrestazure'],
    'GlobusComputeExecutor': ['globus_compute_sdk'],
    'GlobusStaging': ['globus_sdk'],
    'GoogleCloudProvider': ['googleapiclient'],
    'KubernetesProvider': ['kubernetes'],
    'MonitoringHub': ['sqlalchemy'],
    'RadicalPilotExecutor': ['radical.pilot', 'radical.utils'],
    'TaskVineExecutor': ['ndcctools.taskvine'],
    'WorkQueueExecutor': ['ndcctools.work_queue'],
}

# parsl classes we cannot parse from a TOML config, which can only be used by
# passing an instantiated parsl.Config to ParallelConfig
python_api_only_classes = {
    # Its executor option takes a globus_compute_sdk.Executor, but we look up
    # anything under an executor key in parsl.executors
    'GlobusComputeExecutor',
    # These are not exported from parsl.executors
    'RadicalPilotExecutor',
    'TaskVineExecutor',
}

# Options of parsl classes we cannot parse from a TOML config because they take
# a callable or an existing instance, which can only be used by passing an
# instantiated parsl.Config to ParallelConfig. Maps each class to these options
python_api_only_options = {
    'Config': {'dependency_resolver', 'retry_handler', 'std_autopath'},
}

# TOML has no tuples, so options parsl requires to be tuples are loaded as
# lists. Maps each of these options to a function that converts its value
tuple_options = {
    'interchange_port_range': tuple,
    'worker_port_range': tuple,
    # A list of (claim name, mount path) pairs
    'persistent_volumes': lambda volumes: [tuple(v) for v in volumes],
}


class ChangeDebugLevel:
    def __enter__(self):
        self.DEBUG = logging.DEBUG
        if not PARALLEL_CONFIG.debug:
            logging.DEBUG = logging.INFO

    def __exit__(self, *args):
        logging.DEBUG = self.DEBUG


def get_vendored_config():
    """Gets the vendored parallel config as a dict

    Returns
    -------
    dict
        The vendored config as a dict
    dict
        The vendored mapping as a dict
    string
        The source the dicts were retrieved from. This is primarily for use by
        `qiime info`

    NOTE: Does NOT load the config
    """
    from rachis.core.util import in_test_mode

    # If we are running tests, always use the vendored config dict, don't try
    # to load from a file and potentially get a different config.
    config_fp = None
    if in_test_mode():
        source = 'vendored config dict'
        config_dict = copy.copy(VENDORED_CONFIG)
    else:
        config_fp = _get_vendored_config_path()

    # If we are not in a conda environment, we may not get an fp back
    # (because the vendored fp uses the conda prefix), so we load from
    # the vendored dict. Otherwise we load from the vendored file
    if config_fp:
        source = config_fp
        config_dict = _get_config_dict_from_file(config_fp)
    else:
        source = 'vendored config dict'
        config_dict = copy.copy(VENDORED_CONFIG)

    vendored_mapping = config_dict.pop('executor_mapping', {})
    vendored_config = config_dict
    return vendored_config, vendored_mapping, source


def _get_vendored_config_path():
    """Gets the path to the vendored config file if there is one. Checks the
    following locations in the following order:

    1. Path pointed to by the `QIIME2_CONFIG` envvar if set
    2. A file named qiime2_config.toml in the user writable
       platformdirs.user_config_dir for QIIME 2
    3. A file named qiime2_config.toml in the admin writable
       platformdirs.site_config_dir for QIIME 2
    4. The default location `CONDA_PREFIX/etc/qiime2_config.toml`
    5. Write the VENDORED_CONFIG to the location specified in 4 then return
       that path

    Default: It will return None, and we will end up loading the
             VENDORED_CONFIG dict directly

    Returns
    -------
    string
        Path to the vendored config.
    """
    # 1. Check envvar
    config_fp = os.environ.get('QIIME2_CONFIG')

    if config_fp is None:
        # 2. Check in user writable location
        # platformdirs.user_config_dir(appname='qiime2', author='...')
        if os.path.exists(fp_ := os.path.join(
                platformdirs.user_config_dir('qiime2'), 'qiime2_config.toml')):
            config_fp = fp_
        # 3. Check in admin writable location
        # /etc/
        # site_config_dir
        # platformdirs.site_config_dir(appname='qiime2, author='...')
        elif os.path.exists(fp_ := os.path.join(
                platformdirs.site_config_dir('qiime2'), 'qiime2_config.toml')):
            config_fp = fp_
        # NOTE: These next two are dependent on us being in a conda environment
        # 4. Check in conda env
        # ~/miniconda3/env/{env_name}/conf
        elif CONDA_PREFIX != '' and os.path.exists(fp_ := VENDORED_FP):
            config_fp = fp_
        # 5. Write the vendored config to the vendored location and use
        # that
        elif CONDA_PREFIX != '':
            with open(VENDORED_FP, 'w') as fh:
                tomlkit.dump(VENDORED_CONFIG, fh)
            config_fp = VENDORED_FP

    return config_fp


def load_config_from_file(config_fp):
    """Takes a config filepath and loads the config and mapping from it.

    Parameters
    ----------
    config_fp: string | pathlike
        The path to the config to load

    Returns
    -------
    parsl.Config | None
        The config loaded from the file at the path or None if one was not
        found
    dict
        The mapping loaded from the file at the path
    """
    config_dict = _get_config_dict_from_file(config_fp)
    return load_config_from_dict(config_dict)


def load_config_from_dict(config_dict):
    """Takes a config dict and loads the config and mapping from it/

    Parameters
    ----------
    config_dict: dict
        A dict containing a config to load and/or a mapping

    Returns
    -------
    parsl.Config | None
        The config loaded from the dict or None if one was not found
    dict
        The mapping loaded from the dict
    """
    parallel_config_dict = config_dict.get('parsl')
    mapping = parallel_config_dict.pop('executor_mapping', {})

    # NOTE: There was a prior version of the mappings that did not contain the
    # name of the plugin the action belonged to. This is ensuring the config is
    # not of that format. What it checks is designed to specifically catch that
    # case where each key is an action and each value is an executor as opposed
    # to each key being a string and each value being a dict now.
    for value in mapping.values():
        if not isinstance(value, dict):
            raise TypeError(f'The provided mapping:\n\n{mapping}\n\ndoes not'
                            ' follow the correct format of:\n\n'
                            "{'plugin': {'action': 'executor'}}")

    # debug is for us, not parsl, so it must be removed before processing
    PARALLEL_CONFIG.debug = parallel_config_dict.pop('debug', False)
    processed_parallel_config_dict = _process_config(parallel_config_dict)

    if processed_parallel_config_dict != {}:
        parallel_config = parsl.Config(**processed_parallel_config_dict)
    else:
        parallel_config = None

    return parallel_config, mapping


def _get_config_dict_from_file(config_fp):
    """Takes a filepath and returns the dict loaded out of it by tomlkit.load

    Parameters
    ----------
    config_fp: string | pathlike
        The path to the file to load the dict from

    Returns
    -------
    dict
        The dict loaded from the file
    """
    with open(config_fp, 'r') as fh:
        # After parsing the file tomlkit has the data wrapped in its own
        # proprietary classes. Unwrap recursively turns these classes into
        # Python built-ins
        #
        # ex: tomlkit.items.Int -> int
        #
        # issue caused by this wrapping
        # https://github.com/Parsl/parsl/issues/3027
        return tomlkit.load(fh).unwrap()


def _process_config(config_dict):
    """Takes a dict loaded from a toml file or passed in by the user and parses
    it into kwargs that can be used to instantiate a parsl.Config.

    Parameters
    ----------
    config_dict: dict
        The raw values given by the user

    Returns
    -------
    dict
        Kwargs that can be used to instantiate a parsl.Config
    """
    _check_python_api_only_options('Config', config_dict)
    config_kwargs = {}

    for key, value in config_dict.items():
        # Parsl likes the string 'none' as opposed to None or 'None'
        if isinstance(value, str) and value.lower() == 'none':
            config_kwargs[key] = value.lower()
        # We have a list of values
        elif isinstance(value, list):
            config_kwargs[key] = []
            for item in value:
                config_kwargs[key].append(_process_key(key, item))
        # We have a single value
        else:
            config_kwargs[key] = _process_key(key, config_dict[key])

    return config_kwargs


def _process_key(key, value):
    """Takes a key and value given in the raw config and turns the value into
    the correct data type or class instance to be used in instantiating a
    parsl.Config object.

    Parameters
    ----------
    key: string
        The key of this piece of data in the config_dict
    value: list[list | string | <primitive>] | string | primitive
        The value of this piece of data in the config_dict.

    Returns
    -------
    Object
        This could be a primitive, it could be a list of primitives, it could
        be an instantiated parsl class, it could be a mixture of the above.
    """
    # Our key points to a value parsl needs as a tuple
    if key in tuple_options:
        return tuple_options[key](value)
    # Our key points to a list
    elif isinstance(value, list):
        processed_value = []
        for item in value:
            processed_value.append(_process_key(key, item))
        return processed_value
    # Our key needs to point to some object.
    elif key in module_paths:
        _type = value['class']
        _check_optional_dependencies(key, value)
        if _type in python_api_only_classes:
            raise ValueError(_python_api_only_message(_type))

        # Get the class we need to instantiate
        cls = _get_class(key, _type)

        _check_python_api_only_options(_type, value)

        # Get the kwargs we need to pass to the class constructor
        kwargs = {}
        for k, v in value.items():
            # We already handled this key
            if k != 'class':
                kwargs[k] = _process_key(k, v)

        # Instantiate the class
        return cls(**kwargs)
    # Our key points to primitive data
    else:
        return value


def _get_class(key, class_name):
    """Finds the parsl class with the given name in the modules for the given
    key in the config.

    Parameters
    ----------
    key: string
        The key in the config whose value is an instance of the class
    class_name: string
        The name of the class

    Returns
    -------
    type
        The class
    """
    modules = module_paths[key]
    if isinstance(modules, str):
        modules = (modules,)

    for module_path in modules:
        module = importlib.import_module(module_path)
        if hasattr(module, class_name):
            return getattr(module, class_name)

    raise AttributeError(
        f'Could not find a parsl class named {class_name!r} for {key!r}. '
        f'Looked in: {", ".join(modules)}')


def _get_missing_dependencies(class_name):
    """Gets the optional dependencies the given parsl class needs that are not
    installed.

    Parameters
    ----------
    class_name: string
        The name of the parsl class

    Returns
    -------
    list[string]
        The modules the class needs that could not be imported
    """
    missing = []
    for module in optional_dependencies.get(class_name, []):
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(module)

    return missing


def _check_optional_dependencies(key, options):
    """Raises an ImportError explaining what is missing if the parsl class the
    options are for needs optional dependencies that are not installed.

    Parameters
    ----------
    key: string
        The key in the config whose value is an instance of the class
    options: dict
        The class's table from the config, which names the class under 'class'
    """
    class_name = options['class']
    missing = _get_missing_dependencies(class_name)
    if not missing:
        return

    # We cannot build the class, but we can still make sure it accepts the
    # options it was given. Some of these classes are not in the modules we
    # look them up in, so we cannot check those
    try:
        cls = _get_class(key, class_name)
    except AttributeError:
        cls = None

    if cls is not None:
        kwargs = {k: v for k, v in options.items() if k != 'class'}
        try:
            inspect.signature(cls).bind(**kwargs)
        except TypeError as e:
            raise TypeError(f'{class_name}: {e}') from None

    message = (
        f'{class_name} requires {", ".join(missing)}, which could not be '
        'imported. These are optional dependencies of parsl that are not '
        'included in the QIIME 2 conda environments at this time, so they '
        f'must be installed separately to use {class_name}.')
    if class_name in python_api_only_classes:
        message += ' Even then, ' + _python_api_only_message(class_name)

    raise ImportError(message)


def _check_python_api_only_options(class_name, options):
    """Raises a ValueError if the config gives the parsl class an option we
    cannot parse from a TOML config.

    Parameters
    ----------
    class_name: string
        The name of the parsl class
    options: dict
        The class's table from the config
    """
    for option in options:
        if option in python_api_only_options.get(class_name, set()):
            raise ValueError(_python_api_only_message(
                f'The {option!r} option of {class_name}'))


def _python_api_only_message(subject):
    return (f'{subject} cannot be parsed from a TOML config, so it is only '
            'supported through the Python API by passing an instantiated '
            'parsl.Config to ParallelConfig.')


class ParallelConfig():
    def __init__(self, parallel_config=None, action_executor_mapping={}):
        """Tell QIIME 2 how to parsl from the Python API

        Parameters
        ----------
        parallel_config: parsl.Config | None
            If a config is given, this is the config that will be used when
            this ParallelConfig is set. Otherwise we will load the vendored
            config.
        action_executor_mapping: dict
            Maps actions to executors. All unmapped actions will be run on the
            default executor. We check which executor a given action is
            supposed to use when we get ready to run the action, so errors will
            only occur if an action that is being run in a given QIIME 2
            invocation has been mapped to an executor that does not exist
        """
        if isinstance(parallel_config, str) or \
                isinstance(parallel_config, os.PathLike):
            parallel_config, _ = load_config_from_file(parallel_config)
        elif isinstance(parallel_config, dict):
            parallel_config, _ = load_config_from_dict(parallel_config)

        self.parallel_config = parallel_config
        self.action_executor_mapping = action_executor_mapping

    def __enter__(self):
        """Set this to be our parallel config on the current thread local.
        """
        if PARALLEL_CONFIG.parallel_config is not None:
            raise ValueError('ParallelConfig already loaded, cannot nest '
                             'ParallelConfigs')

        # Make sure the vendored mapping is initialized. It is very possible
        # that no mapping will be set at all
        vendored_mapping = {}

        # If they did not already supply a config, we get the vendored one
        if not self.parallel_config:
            vendored_config, vendored_mapping, _ = \
                get_vendored_config()
            vendored_config, _ = load_config_from_dict(vendored_config)

            PARALLEL_CONFIG.parallel_config = vendored_config
        else:
            PARALLEL_CONFIG.parallel_config = self.parallel_config

        # If they did not supply a mapping, set the vendored one
        if not self.action_executor_mapping:
            PARALLEL_CONFIG.action_executor_mapping = vendored_mapping
        else:
            PARALLEL_CONFIG.action_executor_mapping = \
                self.action_executor_mapping

        with ChangeDebugLevel():
            PARALLEL_CONFIG.dfk = parsl.load(PARALLEL_CONFIG.parallel_config)

    def __exit__(self, *args):
        """Unset our parallel config.
        """
        PARALLEL_CONFIG.dfk.cleanup()
        parsl.clear()

        PARALLEL_CONFIG.dfk = None
        PARALLEL_CONFIG.parallel_config = None
        PARALLEL_CONFIG.action_executor_mapping = {}


# TESTING STUFF #
def _check_env(cls):
    from rachis.core.util import in_test_mode

    if not in_test_mode():
        raise ValueError(
            f"Do not instantiate the class '{cls}' when not testing")


class _MASK_CONDA_ENV_():
    """Used to test config loading behavior when outside of a conda environment
    """
    def __init__(self):
        _check_env(self.__class__)

    def __enter__(self):
        global CONDA_PREFIX, VENDORED_FP

        self.old_prefix = CONDA_PREFIX
        self.old_fp = VENDORED_FP

        CONDA_PREFIX = ''
        VENDORED_FP = None

    def __exit__(self, *args):
        global CONDA_PREFIX, VENDORED_FP

        CONDA_PREFIX = self.old_prefix
        VENDORED_FP = self.old_fp
