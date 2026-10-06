# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import os
import pytest
import pathlib
import shutil
import tempfile
import unittest

from rachis.core.path import OwnedPath, OutPath


class TestOwnedPath(unittest.TestCase):
    def setUp(self):
        self.from_dir = tempfile.mkdtemp()
        (pathlib.Path(self.from_dir) / 'foo.txt').touch()

        self.to_dir = tempfile.mkdtemp()
        # assume to_dir is empty for all tests

    def test_move_or_copy_owned(self):
        d = OwnedPath(self.from_dir)
        # ensure that we are owned
        d._user_owned = True

        d._move_or_copy(self.to_dir)

        # since from_dir is owned, _move_or_copy should copy, not move
        self.assertTrue(os.path.exists(os.path.join(self.from_dir, 'foo.txt')))
        self.assertTrue(os.path.exists(os.path.join(self.to_dir, 'foo.txt')))

        shutil.rmtree(self.from_dir)
        shutil.rmtree(self.to_dir)

    def test_move_or_copy_not_owned_rename(self):
        d = OwnedPath(self.from_dir)
        # ensure that we are not owned
        d._user_owned = False

        d._move_or_copy(self.to_dir)

        # since from_dir is not owned, _move_or_copy should move, not copy
        self.assertFalse(os.path.exists(os.path.join(self.from_dir,
                                                     'foo.txt')))
        self.assertTrue(os.path.exists(os.path.join(self.to_dir, 'foo.txt')))

        with self.assertRaises(FileNotFoundError):
            shutil.rmtree(self.from_dir)
        shutil.rmtree(self.to_dir)

    @unittest.mock.patch('pathlib.Path.rename', side_effect=FileExistsError)
    def test_move_or_copy_not_owned_copy(self, _):
        d = OwnedPath(self.from_dir)
        # ensure that we are not owned
        d._user_owned = False

        d._move_or_copy(self.to_dir)

        # since from_dir is not owned, but the network fs race condition crops
        # up, _move_or_copy should copy, not move, but then we still ensure
        # that the original path has been cleaned up
        self.assertFalse(os.path.exists(os.path.join(self.from_dir,
                                                     'foo.txt')))
        self.assertTrue(os.path.exists(os.path.join(self.to_dir, 'foo.txt')))

        with self.assertRaises(FileNotFoundError):
            shutil.rmtree(self.from_dir)
        shutil.rmtree(self.to_dir)


class TestOutPath(unittest.TestCase):
    def test_new_outpath(self):
        f = OutPath()
        self.assertIsInstance(f, OutPath)
        self.assertTrue(f.is_file())

        g = OutPath(dir=True)
        self.assertIsInstance(g, OutPath)
        self.assertTrue(g.is_dir())

    @pytest.mark.usefixtures("legacy_tree_cache")
    def test_new_outpath_context_mgr(self):
        with OutPath() as f:
            path = str(f)
            self.assertIsInstance(f, OutPath)
            self.assertTrue(os.path.isfile(path))
        self.assertFalse(os.path.isfile(path))

    @pytest.mark.usefixtures("legacy_tree_cache")
    def test_destructor(self):
        f = OutPath()
        path = str(f)

        self.assertTrue(os.path.isfile(path))
        f._destructor()
        self.assertFalse(os.path.isfile(path))


if __name__ == '__main__':
    unittest.main()
