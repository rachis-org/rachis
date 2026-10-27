# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import os
import tempfile
import unittest

from rachis.core.annotate import Note, Annotation
from rachis.core.testing.type import FourInts
from rachis.sdk.result import Artifact


class TestAnnotationClass(unittest.TestCase):
    # Annotation base class instantiation failure
    def test_annotation_instantiation_type_error(self):
        with self.assertRaisesRegex(
            TypeError, 'Annotation is an abstract class'
                       ' and cannot be instantiated directly.'
        ):
            Annotation(name='foo')

    # Note subclass tests
    def test_note_instantiation_bad_name_error(self):
        with self.assertRaisesRegex(
            ValueError, 'Name "foo bar" is not a valid Python identifier.'
        ):
            Note(name='foo bar')

    def test_note_instantiation_no_text_or_fp_error(self):
        with self.assertRaisesRegex(
            ValueError, 'No inputs provided to either `text` or `filepath`.'
        ):
            Note(name='note')

    def test_note_instantiation_text_and_fp_error(self):
        with self.assertRaisesRegex(
            ValueError, 'Cannot set both `text` and `filepath` params.'
        ):
            Note(name='name', text='text', filepath='filepath.txt')

    def test_note_instantiation_incorrect_fp_type_error(self):
        fp = 42
        with self.assertRaisesRegex(
            TypeError, f'Unexpected input for `filepath`: {fp} '
        ):
            Note(name='name', filepath=fp)

    def test_note_instantiation_fp_not_found_error(self):
        fp = 'foo.txt'
        with self.assertRaisesRegex(
            ValueError, f'File not found from provided `filepath`: {fp} '
        ):
            Note(name='name', filepath=fp)


class TestAnnotationEndpoints(unittest.TestCase):
    # setup for endpoint testing
    def setUp(self):
        # Create Artifact
        self.test_dir = tempfile.TemporaryDirectory(prefix='rachis-test-temp-')
        self.artifact = Artifact.import_data(FourInts, [-1, 42, 0, 43])

        # Create Notes
        self.note1 = Note(name='mynote', text='my special text')
        self.note2 = Note(name='mynote', text='my other special text')
        self.note3 = Note(name='mynote3', text='my extra special text')

        # Note that exceeds size limit of 10 MiB
        bignote_path = os.path.join(self.test_dir.name, 'bignote.txt')
        with open(bignote_path, 'wb') as fh:
            fh.write(b'A' * (10 * 1024 * 1024 + 1))
        self.bignote = Note(name='mybignote', filepath=bignote_path)

        # Note that doesn't contain valid utf-8
        self.trashnote_path = os.path.join(self.test_dir.name, 'trashnote.txt')
        with open(self.trashnote_path, 'wb') as fh:
            fh.write(b'\xff\xfe\xfa\xfb')

    # `ADD_ANNOTATION` ENDPOINT TESTS
    def test_add_annotation_roundtrip(self):
        # confirm that annotations starts as an empty list
        self.assertEqual(self.artifact._archiver._annotations, {})

        # add note1 to ints1 artifact
        self.artifact.add_annotation(self.note1)

        # check that there's exactly one annotation entry
        self.assertEqual(len(self.artifact._archiver._annotations), 1)

        for annotation in self.artifact.iter_annotations():
            self.assertEqual(annotation.name, 'mynote')
            self.assertEqual(annotation.annotation_type, 'Note')
            self.assertEqual(annotation.contents, 'my special text')
            # testing for actual contents of the saved archive can be found
            # under core -> archive -> format -> test_util.py
            # where other version-specific tests live

    def test_add_annotation_with_same_name_error(self):
        self.artifact.add_annotation(self.note1)
        with self.assertRaisesRegex(
            ValueError, 'Duplicate name detected when attempting'
                        f' to add.*{self.note2.name}'
        ):
            self.artifact.add_annotation(self.note2)

    def test_add_annotation_note_too_big_error(self):
        with self.assertRaisesRegex(
            ValueError, 'Note contents exceed maximum size of 10 MiB'
        ):
            self.artifact.add_annotation(self.bignote)

    def test_add_annotation_note_invalid_error(self):
        with self.assertRaisesRegex(
            ValueError, 'Note contents are not valid UTF-8'
        ):
            self.artifact.add_annotation(
                Note(name='mytrashnote', filepath=self.trashnote_path)
            )

    # `GET_ANNOTATION` ENDPOINT TESTS
    def test_get_annotation_name_not_found_error(self):
        name = 'foo'
        with self.assertRaisesRegex(
            KeyError, f'No Annotation with name: "{name}" was found.'
        ):
            self.artifact.get_annotation(name=name)

    def test_get_annotation_round_trip(self):
        self.artifact.add_annotation(self.note1)
        note1 = self.artifact.get_annotation(name='mynote')

        self.assertEqual(note1.name, 'mynote')
        self.assertEqual(note1.annotation_type, 'Note')
        self.assertEqual(note1.contents, 'my special text')

    # `ITER_ANNOTATIONS` ENDPOINT TESTS
    def test_iter_annotations_on_empty_result(self):
        exp = []
        obs = []
        for annotation in self.artifact.iter_annotations():
            obs.append(annotation)

        self.assertEqual(exp, obs)

    def test_iter_annotations_with_multiple_notes(self):
        self.artifact.add_annotation(self.note1)
        self.artifact.add_annotation(self.note3)

        annotation_list = list(self.artifact.iter_annotations())
        # make sure the number of annotations is correct
        self.assertEqual(len(annotation_list), 2)

        exp_names = ['mynote', 'mynote3']
        exp_contents = ['my special text', 'my extra special text']
        names = []
        contents = []
        for annotation in self.artifact.iter_annotations():
            # we expect both to be notes
            self.assertEqual(annotation.annotation_type, 'Note')
            names.append(annotation.name)
            contents.append(annotation.contents)

        # Membership pairs names with contents; iteration order is unspecified.
        self.assertCountEqual(list(zip(names, contents)),
                              list(zip(exp_names, exp_contents)))

    # `REMOVE_ANNOTATION` ENDPOINT TESTS
    def test_remove_annotation_from_empty_result_error(self):
        with self.assertRaisesRegex(
            KeyError, 'No Annotation found with name: "mynote"'
        ):
            self.artifact.remove_annotation(name='mynote')

    def test_remove_annotation_from_result_with_wrong_name_error(self):
        self.artifact.add_annotation(self.note1)
        name = 'foo'

        with self.assertRaisesRegex(
            KeyError, f'No Annotation found with name: "{name}"'
        ):
            self.artifact.remove_annotation(name=name)

    def test_remove_annotation_round_trip(self):
        self.artifact.add_annotation(self.note1)
        # confirm there's currently one annotation
        self.assertEqual(len(self.artifact._archiver._annotations), 1)

        self.artifact.remove_annotation(name='mynote')
        # now confirm _annotations is empty
        self.assertEqual(len(self.artifact._archiver._annotations), 0)
