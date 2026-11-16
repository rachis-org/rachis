# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import tempfile
import unittest
import shutil
import pathlib
import itertools

import rachis

from rachis.sdk import usage
from rachis.sdk.parallel_config import ParallelConfig
from rachis.plugin.util import transform
from rachis.plugin.model.base import FormatBase

# This is to be used by plugins with parallel testing that do not
# set RACHISTEST (or the historical QIIMETEST)
# it uses a ThreadPoolExecutor only to avoid the HighThroughputExecutor issues
# mentioned above. It also ensures all tests will in theory be using the same
# config not beholden to potential issues with vendored configs being
# different.
NON_QIIMETEST_TEST_CONFIG = {
    'parsl': {
        'strategy': 'None',
        'executors': [
            {'class': 'ThreadPoolExecutor', 'label': 'default',
                'max_threads': 1}
            ]
        }
    }


# TODO Split out into more specific subclasses if necessary.
class TestPluginBase(unittest.TestCase):
    """Test harness for simplifying testing QIIME 2 plugins.

    ``TestPluginBase`` extends ``unittest.TestCase``, with a few extra helpers
    and assertions.

    Attributes
    ----------
    package : str
        The name of the plugin package to be tested. Subclasses **must** define
        this as a class-attribute.
    test_dir_prefix : str
        The prefix for the temporary testing dir created by the harness.
        Optional.

    """

    package = None
    test_dir_prefix = 'rachis-plugin'
    test_config = ParallelConfig(NON_QIIMETEST_TEST_CONFIG)

    def setUp(self):
        """Test runner setup hook.

        If overriding this hook in a test, call ``__super__`` to invoke this
        method in the overridden hook, otherwise the harness might not work
        as expected.

        """

        try:
            package = self.package.split('.')[0]
        except AttributeError:
            self.fail('Test class must have a package property.')

        # plugins are keyed by their names, so a search inside the plugin
        # object is required to match to the correct plugin
        plugin = None
        for name, plugin_ in rachis.sdk.PluginManager().plugins.items():
            if plugin_.package == package:
                plugin = plugin_

        if plugin is not None:
            self.plugin = plugin
        else:
            self.fail('%s is not a registered Rachis plugin.' % package)

        # TODO use rachis temp dir when ported to framework, and when the
        # configurable temp dir exists
        # NOTE: it exists now, so switching would be good
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix='%s-test-temp-' % self.test_dir_prefix)

    def tearDown(self):
        """Test runner teardown hook.

        If overriding this hook in a test, call ``__super__`` to invoke this
        method in the overridden hook, otherwise the harness might not work
        as expected.

        """

        self.temp_dir.cleanup()

    def get_data_path(self, filename, result_as_str=True):
        """Convenience method for getting a data asset while testing.

        Test data stored in the ``data/`` dir local to the running test
        can be accessed via this method.

        Parameters
        ----------
        filename : str
            The name of the file to look up.
        result_as_str : bool, True
            To retain the original API, by default the result is returned as a
            string. Setting this to False will return the result as a
            pathlib.FilePath object.

        Returns
        -------
        filepath : str (or pathlib.Path if result_as_str != True)
            The materialized filepath to the requested test data.

        """
        fp = rachis.util.get_filepath_from_package(
            self.package, 'data/%s' % filename)
        if result_as_str:
            return str(fp)
        else:
            return fp

    def get_transformer(self, from_type, to_type):
        """Convenience method for getting a registered transformer.

        This helper deliberately side-steps the framework's validation
        machinery, so that it is possible for plugin developers to test
        failing conditions.

        Parameters
        ----------
        from_type : A View Type
            The :term:`View` type of the source data.
        to_type : A View Type
            The :term:`View` type to transform to.

        Returns
        -------
        transformer : A Transformer Function
            The registered tranformer from ``from_type`` to ``to_type``.

        """

        try:
            transformer_record = self.plugin.transformers[from_type, to_type]
        except KeyError:
            self.fail(
                "Could not find registered transformer from %r to %r." %
                (from_type, to_type))

        return transformer_record.transformer

    def assertRegisteredSemanticType(self, semantic_type):
        """Test assertion for ensuring a plugin's semantic type is registered.

        Fails if the semantic type requested is not found in the Plugin
        Manager.

        Parameters
        ----------
        semantic_type : A Semantic Type
            The :term:`Semantic Type` to test the presence of.

        """

        try:
            record = self.plugin.type_fragments[semantic_type.name]
        except KeyError:
            self.fail(
                "Semantic type %r is not registered on the plugin." %
                semantic_type)

        self.assertEqual(record.fragment, semantic_type)

    def assertSemanticTypeRegisteredToFormat(self, semantic_type, exp_format):
        """Test assertion for ensuring a semantic type is registered to a
        format.

        Fails if the semantic type requested is not registered to the format
        specified with ``exp_format``. Also fails if the semantic type isn't
        registered to **any** format.

        Parameters
        ----------
        semantic_type : A Semantic Type
            The :term:`Semantic Type` to check for.
        exp_format : A Format
            The :term:`Format` to check that the Semantic Type is registed on.

        """
        # For backward compatibility, support type expressions as input here
        for t in semantic_type:
            obs_format = None

            try:
                obs_format = self.plugin.artifact_classes[str(t)].format
            except KeyError:
                self.assertIsNotNone(
                    obs_format,
                    "Semantic type %r is not registered to a format." % t)

            self.assertEqual(
                obs_format, exp_format,
                "Expected semantic type %r to be registered to format %r, "
                "not %r." % (t, exp_format, obs_format))

    def transform_format(self, source_format, target, filename=None,
                         filenames=None):
        """Helper utility for loading data and transforming it.

        Combines several other utilities in this class, will load files from
        ``data/``, as ``source_format``, then transform to the ``target`` view.

        This helper deliberately side-steps the framework's validation
        machinery, so that it is possible for plugin developers to test
        failing conditions.

        Parameters
        ----------
        source_format : A Format
            The :term:`Format` to load the data as.
        target : A View Type
            The :term:`View Type <View>` to transform the data to.
        filename : str
            The name of the file to load from ``data``. Use this for formats
            that use a single file in their format definition. Mutually
            exclusive with the ``filenames`` parameter.
        filenames : list[str]
            The names of the files to load from ``data``. Use this for formats
            that use multiple files in their format definition. Mutually
            exclusive with the ``filename`` parameter.

        Returns
        -------
        input : A Format
            The data loaded from ``data`` as the specified ``source_format``.
        obs : A View Type
            The loaded data, transformed to the specified ``target`` view type.

        """

        # Guard any non-Rachis Format sources from being tested
        if not issubclass(source_format, FormatBase):
            raise ValueError("`source_format` must be a subclass of "
                             "FormatBase.")

        # Guard against invalid filename(s) usage
        if filename is not None and filenames is not None:
            raise ValueError("Cannot use both `filename` and `filenames` at "
                             "the same time.")

        # Handle format initialization
        source_path = None
        if filename:
            source_path = self.get_data_path(filename)
        elif filenames:
            source_path = self.temp_dir.name
            for filename in filenames:
                filepath = self.get_data_path(filename)
                shutil.copy(filepath, source_path)
        input = source_format(source_path, mode='r')

        obs = transform(input, from_type=source_format, to_type=target)

        if issubclass(target, FormatBase):
            self.assertIsInstance(obs, (type(pathlib.Path()), str, target))
        else:
            self.assertIsInstance(obs, target)

        return input, obs

    def execute_examples(self):
        """Runs all usage examples defined in the plugin."""
        if self.plugin is None:
            raise ValueError('Attempted to run `execute_examples` without '
                             'configuring test harness.')
        for _, action in itertools.chain(self.plugin.actions.items(),
                                         self.plugin.types.items()):
            for name, example_f in action.examples.items():
                with self.subTest(example=name):
                    use = usage.ExecutionUsage()
                    example_f(use)


def assert_no_nans_in_tables(fh):
    '''
    Checks for NaNs present in any of the tables in the indicated file then
    resets to the head of the file.
    '''
    from pandas import read_html

    tables = read_html(fh)
    for df in tables:
        assert not df.isnull().values.any()
    fh.seek(0)
