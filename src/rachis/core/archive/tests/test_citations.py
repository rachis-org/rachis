# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import unittest

import rachis
from rachis.core.testing.type import IntSequence1
from rachis.core.testing.util import get_dummy_plugin


class TestCitationsTracked(unittest.TestCase):
    def setUp(self):
        self.plugin = get_dummy_plugin()

    def test_import(self):
        data = rachis.Artifact.import_data(IntSequence1, [1, 2, 3, 4])
        archiver = data._archiver

        expected = [
            ('framework|rachis:%s|0' % rachis.__version__,
             'Reproducible, interactive, scalable and extensible microbiome '
             'data science using QIIME 2'),
            ('plugin|dummy-plugin:0.0.0-dev|0',
             'Does knuckle cracking lead to arthritis of the fingers?'),
            ('plugin|dummy-plugin:0.0.0-dev|1',
             'Of flying frogs and levitrons'),
            ('transformer|dummy-plugin:0.0.0-dev|'
             'builtins:list->IntSequenceDirectoryFormat|0',
             'An in-depth analysis of a piece of shit: distribution of'
             ' Schistosoma mansoni and hookworm eggs in human stool'),
            ('view|dummy-plugin:0.0.0-dev|IntSequenceDirectoryFormat|0',
             'Walking with coffee: Why does it spill?')]

        obs = list(map(lambda item: (item[0], item[1].fields['title']),
                       archiver.citations.items()))

        self.assertEqual(sorted(obs), sorted(expected))

        with (archiver.provenance_dir / 'action' / 'action.yaml').open() as fh:
            action_yaml = fh.read()

        for key, _ in expected:
            self.assertIn('!cite %r' % key, action_yaml)

    def test_action(self):
        data = rachis.Artifact.import_data(IntSequence1, [1, 2, 3, 4])
        action = self.plugin.methods['split_ints']

        left, right = action(data)
        archiver = left._archiver

        expected = [
            ('framework|rachis:%s|0' % rachis.__version__,
             'Reproducible, interactive, scalable and extensible microbiome '
             'data science using QIIME 2'),
            ('action|dummy-plugin:0.0.0-dev|method:split_ints|0',
             'Sword swallowing and its side effects'),
            ('action|dummy-plugin:0.0.0-dev|method:split_ints|1',
             'Response behaviors of Svalbard reindeer towards humans and'
             ' humans disguised as polar bears on Edge\u00f8ya'),
            ('plugin|dummy-plugin:0.0.0-dev|0',
             'Does knuckle cracking lead to arthritis of the fingers?'),
            ('plugin|dummy-plugin:0.0.0-dev|1',
             'Of flying frogs and levitrons'),
            ('view|dummy-plugin:0.0.0-dev|IntSequenceDirectoryFormat|0',
             'Walking with coffee: Why does it spill?'),
            ('transformer|dummy-plugin:0.0.0-dev|'
             'builtins:list->IntSequenceDirectoryFormat|0',
             'An in-depth analysis of a piece of shit: distribution of'
             ' Schistosoma mansoni and hookworm eggs in human stool')]

        obs = list(map(lambda item: (item[0], item[1].fields['title']),
                       archiver.citations.items()))

        self.assertEqual(sorted(obs), sorted(expected))

        with (archiver.provenance_dir / 'action' / 'action.yaml').open() as fh:
            action_yaml = fh.read()

        for key, _ in expected:
            self.assertIn('!cite %r' % key, action_yaml)


if __name__ == '__main__':
    unittest.main()
