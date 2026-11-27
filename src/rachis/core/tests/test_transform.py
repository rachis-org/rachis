# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import unittest
from unittest.mock import Mock, call, patch
from typing import Union
from tempfile import TemporaryDirectory

from rachis import Artifact
from rachis import sdk
from rachis.core import util
from rachis.core.transform import (
    ModelType, NodeQueue, SearchNode, TransformType, compose_transformation,
    find_transformation_path
)
from rachis.plugin import SingleFileDirectoryFormat, TextFileFormat
from rachis.core.testing.format import (
    FirstStepFormat, SecondStepFormat, ThirdStepFormat, FourthStepFormat,
    FifthStepFormat, Cephalapod, IntSequenceFormat, IntSequenceFormatV2,
    IntSequenceDirectoryFormat, IntSequenceV2DirectoryFormat
)


class TestTransitiveTransformers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with TemporaryDirectory() as tempdir:
            cls.first_format = Artifact.import_data(
                type='FirstStep', view=tempdir
            )

        cls.int_sequence = Artifact.import_data(
            type='IntSequence1', view=[1, 2, 3]
        )

    def test_upgrade_true_only_path(self):
        """
        Path exists and each hop is `upgrade=True`.
        FirstStepFormat -> SecondStepFormat -> ThirdStepFormat
        """
        transformed = self.first_format.view(ThirdStepFormat)
        self.assertEqual(type(transformed), ThirdStepFormat)

    def test_upgrade_none_allowed_at_end_of_transformation_path(self):
        """
        The transformation from `FirstStepFormat` to `FourthStepFormat` uses
        an `upgrade=None` transformer from `ThirdStepFormat` to
        `FourthStepFormat`. This is allowed because there is only one
        `upgrade=None` hop, and it occurs at one of the ends of the path.
        """
        transformed = self.first_format.view(FourthStepFormat)
        self.assertEqual(type(transformed), FourthStepFormat)

    def test_union_transitivity(self):
        """
        Path exists between FirstStepFormat and ThirdStepFormat but not between
        FirsStepFormat and Cephalapod.
        """
        view = self.first_format.view(Union[Cephalapod, ThirdStepFormat])
        self.assertEqual(type(view), ThirdStepFormat)

    def test_int_sequence_dir_to_list(self):
        """
        Tests that unwrapping a format and then transforming does not require
        upgrading the path.
        """
        view = self.int_sequence.view(list)
        self.assertEqual(view, [1, 2, 3])

    def test_implicit_steps_preserve_payload_and_return_user_owned_view(self):
        """
        An implicit unwrap and wrap surrounding a registered transformation
        preserve payload data. The final format returned by `Artifact.view`
        is user-owned.
        """
        view = self.int_sequence.view(IntSequenceV2DirectoryFormat)

        self.assertEqual(view.file.view(list), [1, 2, 3])
        self.assertTrue(view.path._user_owned)

    def test_lossy_transformer(self):
        """
        Tests that path with lossy steps still completes.
        First -> Second -> Third -lossy-> Fifth
        """
        view = self.first_format.view(FifthStepFormat)
        self.assertEqual(type(view), FifthStepFormat)


class TestTransitiveUpgradeSpec(unittest.TestCase):
    _implicit = object()

    def _make_path(self, *upgrades):
        '''
        Creates a dummy transformation path.

        Parameters
        ----------
        *upgrades : list[None | True | False | self._implicit]
            The upgrade types in the path. `None`, `True`, and `False`
            represent registered transformers with `upgrade=None`,
            `upgrade=True`, and `upgrade=False`, respectively. A
            `self._implicit` represents an implicit (unregistered) transformer.

        Returns
        -------
        SearchNode
            A node storing the dummy transformation path in its ancestors.
        '''
        node = SearchNode(type('Start', (), {}))

        for index, upgrade in enumerate(upgrades):
            if upgrade is self._implicit:
                record = None
                transform_type = TransformType.wrap
            else:
                record = Mock(upgrade=upgrade)
                transform_type = TransformType.registered

            node = SearchNode(
                type_=type(f'Step{index}', (), {}),
                parent=node,
                record=record,
                transform_type=transform_type,
            )

        return node

    def test_upgrade_none_only_is_valid(self):
        '''
        A path composed of a single `upgrade=None` step is valid because there
        is only one and it occurs at an end (here, both ends).
        '''
        path = self._make_path(None)
        self.assertTrue(path.validate_path())

    def test_upgrade_none_first_is_valid(self):
        '''
        A path containing a single `upgrade=None` step as the first registered
        step is valid.
        '''
        path = self._make_path(None, True)
        self.assertTrue(path.validate_path())

    def test_upgrade_none_last_is_valid(self):
        '''
        A path containging a single `upgrade=None` step as the last registered
        step is valid.
        '''
        path = self._make_path(True, None)
        self.assertTrue(path.validate_path())

    def test_implicit_steps_do_not_affect_upgrade_none_position(self):
        '''
        Implicit (unregistered) steps do not determine the "ends" of the path.
        Thus, the `upgrade=Nones` here are valid because they are the first or
        last registered transformers.
        '''
        paths = [
            self._make_path(
                self._implicit, None, True, self._implicit
            ),
            self._make_path(
                self._implicit, True, None, self._implicit
            ),
        ]
        for path in paths:
            self.assertTrue(path.validate_path())

    def test_upgrade_none_in_middle_is_invalid(self):
        '''
        A path containing an `upgrade=None` step that occurs between registered
        steps is invalid.
        '''
        path = self._make_path(True, None, True)
        self.assertFalse(path.validate_path())

    def test_multiple_upgrade_none_steps_are_invalid(self):
        '''
        A path may not contain more than one `upgrade=None` step, even if they
        occur at the ends.
        '''
        path = self._make_path(None, True, None)
        self.assertFalse(path.validate_path())

    def test_search_continues_after_invalid_target_path(self):
        '''
        Shows that an invalid target path which is explored first does not
        prevent finding a valid one later on.
        '''
        class Target:
            pass

        invalid_target = self._make_path(True, None, True)
        invalid_target.type_ = Target
        valid_target = self._make_path(True, None)
        valid_target.type_ = Target

        with patch.object(
            NodeQueue, 'pop', side_effect=[invalid_target, valid_target]
        ) as pop:
            target = find_transformation_path(object, Target)

        self.assertIs(target, valid_target)
        self.assertEqual(pop.call_count, 2)


class _TransformationGraphTestCase(unittest.TestCase):
    def _find_path(self, start, target, edges, ff_to_sfdf=None):
        '''
        Find a transformation path through the search graph represented in
        `edges`. Mocks the plugin manager to create artificial transformers
        for each of the graph edges in `edges`.

        Parameters
        ----------
        start : type
            The starting type for the transformation path.
        target : type
            The target type for the transformation path.
        edges : list[tuple[type, type, bool | None]]
            Transformers represented by source type, target type, and upgrade
            classification. Their order determines registration order.
        ff_to_sfdf : dict[type, set[type]] | None
            Maps file formats to the single-file directory formats that wrap
            them, as in `PluginManager._ff_to_sfdf`. Only needed when the
            graph contains formats with implicit wrap/unwrap steps.

        Returns
        -------
        SearchNode | None
            A `SearchNode` representing the discovered path, or None if no
            path was found.
        '''
        transformers = {}
        for source, destination, upgrade in edges:
            transformers.setdefault(source, {})[destination] = Mock(
                upgrade=upgrade
            )

        plugin_manager = Mock(
            transformers=transformers,
            _ff_to_sfdf={} if ff_to_sfdf is None else ff_to_sfdf,
        )
        with patch(
            'rachis.core.transform.sdk.PluginManager',
            return_value=plugin_manager,
        ):
            return find_transformation_path(start, target)


class TestCompetingPathPreferences(_TransformationGraphTestCase):
    def test_longer_upgrade_only_path_preferred_to_shorter_false_path(self):
        '''
        A path containing only `upgrade=True` transformations of length 4 is
        preferred over a path containing an `upgrade=False` transformation of
        length 2. Shows that true-only paths are prioritized regardless of
        length.
        '''
        class Start:
            pass

        class First:
            pass

        class Second:
            pass

        class Target:
            pass

        path = self._find_path(
            Start,
            Target,
            [
                (Start, Target, False),
                (Start, First, True),
                (First, Second, True),
                (Second, Target, True),
            ],
        )

        self.assertEqual(
            [node.type_ for node in path.steps()],
            [Start, First, Second, Target],
        )

    def test_longer_false_path_preferred_to_shorter_none_path(self):
        '''
        A path containing an `upgrade=False` transformations of length 4 is
        preferred over a path containing an `upgrade=None` transformation of
        length 2. Shows that non-None paths are prioritized over
        None-containing paths regardless of length.
        '''
        class Start:
            pass

        class First:
            pass

        class Second:
            pass

        class Target:
            pass

        path = self._find_path(
            Start,
            Target,
            [
                (Start, Target, None),
                (Start, First, False),
                (First, Second, True),
                (Second, Target, True),
            ],
        )

        self.assertEqual(
            [node.type_ for node in path.steps()],
            [Start, First, Second, Target],
        )

    def test_shorter_path_preferred_within_same_classification(self):
        '''
        Asserts that within a path classification (upgrade-only,
        includes-false, includes-none), the shortest path is preferred. Here
        the includes-false class is used.
        '''
        class Start:
            pass

        class Short:
            pass

        class LongFirst:
            pass

        class LongSecond:
            pass

        class Target:
            pass

        path = self._find_path(
            Start,
            Target,
            [
                (Start, Short, False),
                (Start, LongFirst, False),
                (Short, Target, True),
                (LongFirst, LongSecond, True),
                (LongSecond, Target, True),
            ],
        )

        self.assertEqual(
            [node.type_ for node in path.steps()],
            [Start, Short, Target],
        )

    def test_true_preference_independent_of_registration_order(self):
        '''
        A path containing only `upgrade=True` steps is preferred over a path
        with an `upgrade=False` step regardless of the order in which the
        differing transformers are registered.
        '''
        class Start:
            pass

        class UpgradeOnly:
            pass

        class IncludesFalse:
            pass

        class Target:
            pass

        false_registered_first = self._find_path(
            Start,
            Target,
            [
                (Start, IncludesFalse, False),
                (Start, UpgradeOnly, True),
                (UpgradeOnly, Target, True),
                (IncludesFalse, Target, True),
            ],
        )
        true_registered_first = self._find_path(
            Start,
            Target,
            [
                (Start, UpgradeOnly, True),
                (Start, IncludesFalse, False),
                (UpgradeOnly, Target, True),
                (IncludesFalse, Target, True),
            ],
        )

        expected = [Start, UpgradeOnly, Target]
        self.assertEqual(
            [node.type_ for node in false_registered_first.steps()], expected
        )
        self.assertEqual(
            [node.type_ for node in true_registered_first.steps()], expected
        )

    def test_false_preference_independent_of_registration_order(self):
        '''
        A path containing `upgrade=False` but no `upgrade=None` steps is
        preferred over a path with an `upgrade=None` step regardless of the
        order in which the differing transformers registered.
        '''
        class Start:
            pass

        class IncludesFalse:
            pass

        class IncludesNone:
            pass

        class Target:
            pass

        none_registered_first = self._find_path(
            Start,
            Target,
            [
                (Start, IncludesNone, None),
                (Start, IncludesFalse, False),
                (IncludesFalse, Target, True),
                (IncludesNone, Target, True),
            ],
        )
        false_registered_first = self._find_path(
            Start,
            Target,
            [
                (Start, IncludesFalse, False),
                (Start, IncludesNone, None),
                (IncludesFalse, Target, True),
                (IncludesNone, Target, True),
            ],
        )

        expected = [Start, IncludesFalse, Target]
        self.assertEqual(
            [node.type_ for node in none_registered_first.steps()], expected
        )
        self.assertEqual(
            [node.type_ for node in false_registered_first.steps()], expected
        )

    def test_direct_transformer_preferred_over_wrap_detour(self):
        '''
        A transformer registered directly from a file format to the target is
        preferred over implicitly wrapping the file format into its single-file
        directory format and using a transformer registered on the directory
        format. Both paths contain one registered transformer, so the path
        without the extra implicit step should win.
        '''
        class Target:
            pass

        path = self._find_path(
            IntSequenceFormat,
            Target,
            [
                (IntSequenceFormat, Target, None),
                (IntSequenceDirectoryFormat, Target, None),
            ],
            ff_to_sfdf={IntSequenceFormat: {IntSequenceDirectoryFormat}},
        )

        self.assertEqual(
            [node.type_ for node in path.steps()],
            [IntSequenceFormat, Target],
        )

    def test_direct_transformer_preferred_over_unwrap_detour(self):
        '''
        A transformer registered directly from a single-file directory format
        to the target is preferred over implicitly unwrapping the directory
        format and using a transformer registered on the contained file format.
        Both paths contain one registered transformer, so the path without the
        extra implicit step should win.
        '''
        class Target:
            pass

        path = self._find_path(
            IntSequenceDirectoryFormat,
            Target,
            [
                (IntSequenceDirectoryFormat, Target, None),
                (IntSequenceFormat, Target, None),
            ],
            ff_to_sfdf={IntSequenceFormat: {IntSequenceDirectoryFormat}},
        )

        self.assertEqual(
            [node.type_ for node in path.steps()],
            [IntSequenceDirectoryFormat, Target],
        )

    def test_direct_transformer_preferred_over_registered_unwrap_detour(self):
        '''
        When transformers are registered from `tuple` to both a file format and
        its single-file directory format, a direct transformation to the file
        format is preferred over transforming to the directory format and then
        unwrapping it, regardless of registration order.
        '''
        ff_to_sfdf = {IntSequenceFormat: {IntSequenceDirectoryFormat}}
        file_registered_first = self._find_path(
            tuple,
            IntSequenceFormat,
            [
                (tuple, IntSequenceFormat, None),
                (tuple, IntSequenceDirectoryFormat, None),
            ],
            ff_to_sfdf=ff_to_sfdf,
        )
        directory_registered_first = self._find_path(
            tuple,
            IntSequenceFormat,
            [
                (tuple, IntSequenceDirectoryFormat, None),
                (tuple, IntSequenceFormat, None),
            ],
            ff_to_sfdf=ff_to_sfdf,
        )

        expected = [tuple, IntSequenceFormat]
        self.assertEqual(
            [node.type_ for node in file_registered_first.steps()], expected
        )
        self.assertEqual(
            [node.type_ for node in directory_registered_first.steps()],
            expected,
        )


class TestTransformationPathCycles(_TransformationGraphTestCase):
    def test_reachable_target_with_cycle(self):
        '''
        Shows that the presence of a cycle in the transformation graph does
        not preclude the target from being found.
        '''
        class Start:
            pass

        class First:
            pass

        class Second:
            pass

        class Target:
            pass

        path = self._find_path(
            Start,
            Target,
            [
                (Start, First, True),
                (First, Second, True),
                (Second, First, True),
                (Second, Target, True),
            ],
        )

        self.assertEqual(
            [node.type_ for node in path.steps()],
            [Start, First, Second, Target],
        )

    def test_unreachable_target_with_cycle(self):
        '''
        Shows that a transformation graph with a cycle but no valid path does
        not hang forever.
        '''
        class Start:
            pass

        class First:
            pass

        class Second:
            pass

        class Target:
            pass

        path = self._find_path(
            Start,
            Target,
            [
                (Start, First, True),
                (First, Second, True),
                (Second, First, True),
            ],
        )

        self.assertIsNone(path)

    def test_reconverging_paths_preserve_path_history(self):
        '''
        A type reached through one path does not prevent it from being reached
        and extended through another path with different history.

        The shorter path to `Merge` produces the invalid sequence
        [True, None, True]. The longer path produces the valid sequence
        [None, True, True, True]. We know that the shorter path to `Merge`
        is discovered first due to queue ordering.
        '''
        class Start:
            pass

        class InvalidBranch:
            pass

        class ValidBranch:
            pass

        class Detour:
            pass

        class Merge:
            pass

        class Target:
            pass

        path = self._find_path(
            Start,
            Target,
            [
                (Start, InvalidBranch, True),
                (InvalidBranch, Merge, None),
                (Start, ValidBranch, None),
                (ValidBranch, Detour, True),
                (Detour, Merge, True),
                (Merge, Target, True),
            ],
        )

        self.assertEqual(
            [node.type_ for node in path.steps()],
            [Start, ValidBranch, Detour, Merge, Target],
        )


class TestTransformationComposition(unittest.TestCase):
    def _compose_registered_path(self, types, transformers):
        '''
        Compose a transformation closure from a series of types and associated
        transformers.
        '''
        node = SearchNode(types[0])
        for type_, transformer in zip(types[1:], transformers):
            node = SearchNode(
                type_=type_,
                parent=node,
                record=Mock(transformer=transformer),
                transform_type=TransformType.registered,
            )

        return compose_transformation(node)

    def test_transformers_execute_in_order_and_pass_intermediate_value(self):
        '''
        Assert that each transformer receives the expected value produced by
        the expected prior step.
        '''
        class First:
            def __init__(self, value):
                self.value = value

        class Second:
            def __init__(self, value):
                self.value = value

        class Third:
            def __init__(self, value):
                self.value = value

        calls = []

        def first_to_second(view):
            calls.append(('first-to-second', view.value))
            return Second(view.value + 1)

        def second_to_third(view):
            calls.append(('second-to-third', view.value))
            return Third(view.value * 2)

        transformation = self._compose_registered_path(
            [First, Second, Third],
            [first_to_second, second_to_third],
        )

        result = transformation(First(3))

        self.assertEqual(
            calls,
            [('first-to-second', 3), ('second-to-third', 4)],
        )
        self.assertIsInstance(result, Third)
        self.assertEqual(result.value, 8)

    def test_validator_can_short_circuit_transformation_chain(self):
        '''
        Shows that validation will terminate a composed transformation if an
        intermediate type is invalid.
        '''
        class First:
            pass

        class Second:
            pass

        class Third:
            pass

        first_to_second = Mock(return_value='uh-oh')
        second_to_third = Mock(return_value=Third())
        transformation = self._compose_registered_path(
            [First, Second, Third],
            [first_to_second, second_to_third],
        )

        with self.assertRaisesRegex(TypeError, 'cannot transform further'):
            transformation(First())

        first_to_second.assert_called_once()
        second_to_third.assert_not_called()

    def test_format_validation_ownership_and_lifetime(self):
        '''
        Shows that each format in the transformation chain is validated at
        the same requested level. Also shows that intermediate formats remain
        readable during the next hop and that transformation outputs are
        marked as internally owned (`user_owned=False`).
        '''
        validations = []
        transformer_calls = []

        class TracedFormat(TextFileFormat):
            label = None

            def _validate_(self, level):
                validations.append((self.label, level))

        class FirstFormat(TracedFormat):
            label = 'first'

        class SecondFormat(TracedFormat):
            label = 'second'

        class ThirdFormat(TracedFormat):
            label = 'third'

        def write_format(format_, value):
            result = format_()
            with result.open() as fh:
                fh.write(value)
            return result

        def first_to_second(view):
            transformer_calls.append(
                ('first-to-second', view._mode, view.path._user_owned,
                 view.path.exists())
            )
            return write_format(SecondFormat, 'second')

        def second_to_third(view):
            with view.open() as fh:
                intermediate_value = fh.read()
            transformer_calls.append(
                ('second-to-third', view._mode, view.path._user_owned,
                 view.path.exists())
            )
            self.assertEqual(intermediate_value, 'second')
            return write_format(ThirdFormat, 'third')

        source = write_format(FirstFormat, 'first')
        transformation = self._compose_registered_path(
            [FirstFormat, SecondFormat, ThirdFormat],
            [first_to_second, second_to_third],
        )
        result = transformation(source, validate_level='max')

        self.assertEqual(
            transformer_calls,
            [
                # source format is user_owned=True
                ('first-to-second', 'r', True, True),
                # transformed-to format is user_owned=False
                ('second-to-third', 'r', False, True),
            ],
        )
        self.assertEqual(
            validations,
            [
                ('first', 'max'),
                # SecondFormat is validated once as output from first-to-second
                # transformer and again as input to second-to-third transformer
                ('second', 'max'),
                ('second', 'max'),
                ('third', 'max'),
            ],
        )

        self.assertTrue(source.path._user_owned)
        self.assertTrue(source.path.exists())

        self.assertEqual(result._mode, 'r')
        self.assertFalse(result.path._user_owned)
        self.assertTrue(result.path.exists())

    def test_implicit_unwrap_rewrap_preserves_source_file(self):
        '''
        Tests the special case where an unwrap transformation is followed by a
        wrap transformation and ensures that the source file is copied, not
        moved. Because unwrap transformers create no new storage we must ensure
        that the resulting output is marked `user_owned=True`.
        '''
        SourceFormat = SingleFileDirectoryFormat(
            'SourceFormat', 'source.txt', IntSequenceFormat
        )
        TargetFormat = SingleFileDirectoryFormat(
            'TargetFormat', 'target.txt', IntSequenceFormat
        )

        source = SourceFormat()
        source_member = source.file.path_maker()
        source_member.write_text('1\n2\n')
        self.assertTrue(source.path._user_owned)

        node = SearchNode(SourceFormat)
        node = SearchNode(
            IntSequenceFormat,
            parent=node,
            transform_type=TransformType.unwrap,
        )
        node = SearchNode(
            TargetFormat,
            parent=node,
            transform_type=TransformType.wrap,
        )

        result = compose_transformation(node)(source)
        target_member = result.file.path_maker()

        # path was not moved into TargetFormat
        self.assertTrue(source_member.exists())
        self.assertEqual(target_member.read_text(), source_member.read_text())

    def _registered_then_unwrap_path(self):
        '''
        Builds the path:

            tuple -registered-> IntSequenceDirectoryFormat -unwrap->
            IntSequenceFormat

        The registered transformer creates a new, framework-owned directory
        which the unwrap step then aliases.

        Used in tests catching a regression on the combination of a registered
        transformer followed by an unwrapping step where the directory
        containing the unwrapped file was being destroyed after the registered
        transformer returned, leaving the unwrapped file with a dangling path.
        '''
        def tuple_to_directory(view):
            directory = IntSequenceDirectoryFormat()
            directory.file.path_maker().write_text(
                ''.join(f'{i}\n' for i in view)
            )
            return directory

        node = SearchNode(tuple)
        node = SearchNode(
            IntSequenceDirectoryFormat,
            parent=node,
            record=Mock(transformer=tuple_to_directory),
            transform_type=TransformType.registered,
        )
        node = SearchNode(
            IntSequenceFormat,
            parent=node,
            transform_type=TransformType.unwrap,
        )

        return node

    def test_unwrap_of_intermediate_directory_format_mid_chain(self):
        '''
        A directory format created by a registered transformer remains on disk
        while a subsequent step reads the file unwrapped from it.

        Catches a regression where the directory format was being destroyed
        after the registered transformer returned (all references to the
        backing OutPath were gone, so the OutPath was destroyed), leaving the
        unwrapped file format with a dangling path.
        '''
        def file_to_list(view):
            with view.open() as fh:
                return [int(line) for line in fh]

        node = SearchNode(
            list,
            parent=self._registered_then_unwrap_path(),
            record=Mock(transformer=file_to_list),
            transform_type=TransformType.registered,
        )

        result = compose_transformation(node)((1, 2, 3))

        self.assertEqual(result, [1, 2, 3])

    def test_unwrap_of_intermediate_directory_format_as_final_step(self):
        '''
        When the final step unwraps a directory format created by a registered
        transformer, the returned file format remains readable after the
        transformation returns.

        Catches a regression where the directory format was being destroyed
        after the registered transformer returned (all references to the
        backing OutPath were gone, so the OutPath was destroyed), leaving the
        unwrapped file format with a dangling path.
        '''
        node = self._registered_then_unwrap_path()

        result = compose_transformation(node)((1, 2, 3))

        self.assertTrue(result.path.exists())
        with result.open() as fh:
            self.assertEqual(fh.read(), '1\n2\n3\n')


class TestTransformationRecorder(unittest.TestCase):
    def setUp(self):
        self.pm = sdk.PluginManager()

    def _record(self, from_type, to_type):
        '''
        Mocks the recorder callable in `make_transformation`.
        '''
        recorder = Mock()
        from_mt = ModelType.from_view_type(from_type)
        to_mt = ModelType.from_view_type(to_type)
        from_mt.make_transformation(to_mt, recorder=recorder)
        return recorder

    def _call(self, record, from_type, to_type):
        '''
        Builds an expected `unittest.mock.call` object.
        '''
        input_name = util.get_view_name(from_type)
        output_name = util.get_view_name(to_type)
        return call(
            record,
            input_name=input_name,
            input_record=self.pm.views.get(input_name),
            output_name=output_name,
            output_record=self.pm.views.get(output_name),
        )

    def test_records_implicit_only_path(self):
        '''
        Asserts that a single-step implicit transformation results in one
        call to the recorder, with no `TransformerRecord`.
        '''
        recorder = self._record(
            IntSequenceFormat, IntSequenceDirectoryFormat
        )

        self.assertEqual(
            recorder.call_args_list,
            [self._call(
                None, IntSequenceFormat, IntSequenceDirectoryFormat
            )],
        )

    def test_records_identity_path(self):
        '''
        Asserts that an identity transformation results in one call to the
        recorder, with no `TransformerRecord` and equivalent start/end types.
        '''
        recorder = self._record(IntSequenceFormat, IntSequenceFormat)

        self.assertEqual(
            recorder.call_args_list,
            [self._call(None, IntSequenceFormat, IntSequenceFormat)],
        )

    def test_folds_leading_implicit_step_into_registered_step(self):
        '''
        Here we transform from `IntSequenceDirectoryFormat` to `list`. An
        implicit transformation to `IntSequenceFormat` must be made. If we
        represent the starting node in the search path as S, nodes resulting
        from an implicit transformation as I, and nodes resulting from a
        registered transformation as R, then we have:

            transformation type: S-I-R
                          index: 0-1-2

        This test shows we record the transformation as starting at S (index 0)
        and ending at R (index 2), but associating the registered transformer
        from I (index 1) to R (index 2).
        '''
        recorder = self._record(IntSequenceDirectoryFormat, list)
        record = self.pm.transformers[IntSequenceFormat][list]

        self.assertEqual(
            recorder.call_args_list,
            [self._call(record, IntSequenceDirectoryFormat, list)],
        )

    def test_folds_trailing_implicit_step_into_registered_step(self):
        '''
        See `test_folds_leading_implicit_step_into_registered_step`. Here
        we have:

            transformation type: S-R-I
                          index: 0-1-2

        This test shows that we record the transformation as starting at S
        (index 0) and ending at I (index 2), but associating the transformer
        registered from S (index 0) to R (index 1).
        '''
        recorder = self._record(list, IntSequenceDirectoryFormat)
        record = self.pm.transformers[list][IntSequenceFormat]

        self.assertEqual(
            recorder.call_args_list,
            [self._call(record, list, IntSequenceDirectoryFormat)],
        )

    def test_folds_leading_and_trailing_implicit_steps(self):
        '''
        Asserts that a path with both leading and trailing implicit steps is
        recorded as one transformation between the path endpoints, but is
        associated with the only registered transformer in the chain.

            transformation type: S-I-R-I
                          index: 0-1-2-3

        This test shows that we start at S (index 0), end at I (index 3), and
        associate the transformer from I (index 1) to R (index 2).
        '''
        recorder = self._record(
            IntSequenceDirectoryFormat, IntSequenceV2DirectoryFormat
        )
        record = self.pm.transformers[
            IntSequenceFormat][IntSequenceFormatV2]

        self.assertEqual(
            recorder.call_args_list,
            [self._call(
                record,
                IntSequenceDirectoryFormat,
                IntSequenceV2DirectoryFormat,
            )],
        )

    def test_records_each_registered_step(self):
        '''
        Asserts that when transforming from `FirstStepFormat` to
        `ThirdStepFormat` a transitive, registered hop over `SecondStepFormat`
        is made, and that the resulting two expected calls to the recorder
        appear on the mock.
        '''
        recorder = self._record(FirstStepFormat, ThirdStepFormat)

        self.assertEqual(
            recorder.call_args_list,
            [
                self._call(
                    self.pm.transformers[FirstStepFormat][SecondStepFormat],
                    FirstStepFormat,
                    SecondStepFormat,
                ),
                self._call(
                    self.pm.transformers[SecondStepFormat][ThirdStepFormat],
                    SecondStepFormat,
                    ThirdStepFormat,
                ),
            ],
        )
