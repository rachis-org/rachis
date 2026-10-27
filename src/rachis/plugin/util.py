# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from pathlib import Path
import psutil
import secrets
import subprocess

from rachis.core.transform import ModelType


def transform(data, *, from_type=None, to_type):
    from_type = type(data) if from_type is None else from_type

    from_model_type = ModelType.from_view_type(from_type)
    to_model_type = ModelType.from_view_type(to_type)
    transformation = from_model_type.make_transformation(to_model_type)

    return transformation(data)


def get_available_cores(n_less: int = 0):
    '''
    Finds the number of currently available (logical) cores. Useful for plugins
    that need to convert a 0 to a concrete number of cores when 0 is not
    supported by the underlying/called software.

    Parameters
    ----------
    n_less : int
        The number of cores less than the total number available to request.
        For example `get_available_cores(n_less=2)` with 10 available cores
        will return 8.

    Returns
    -------
    int
        The number of cores to be requested.
    '''
    cpus = psutil.cpu_count()
    if cpus is not None:
        return cpus - n_less

    return 1


def run_commands(cmds, verbose=True):
    '''
    Helper method for subprocess calls. Typically used when wrapping
    external tools/methods within a QIIME 2 plugin.
    '''
    if verbose:
        print("Running external command line application(s). This may print "
              "messages to stdout and/or stderr.")
        print("The command(s) being run are below. These commands cannot "
              "be manually re-run as they will depend on temporary files that "
              "no longer exist.")
    for cmd in cmds:
        if verbose:
            print("\nCommand:", end=' ')
            print(" ".join(cmd), end='\n\n')
        subprocess.run(cmd, check=True)


# Numpy recommends using at least 128 bits of entropy as a seed.
NP_RNG_BITS = 128


def get_np_random_seed():
    """
    Generates a random int with 128 bits of entropy. The min recommended for
    numpy seeding. secrects.randbits is used here as it is the most reliable
    way to produce this behavior with the Python standard libraries.

    Returns
    -------
    int
        A random int with 128 bits of entropy
    """
    return secrets.randbits(NP_RNG_BITS)


def get_nonhidden_files(dir: Path) -> list[Path]:
    '''
    Return all files under `dir` that are not dotfiles and are not ancestors
    of dotdirectories.
    '''
    candidates = dir.glob('**/*')

    non_hidden = []
    for path in candidates:
        if not path.is_file():
            continue
        if any([part.startswith('.') for part in path.relative_to(dir).parts]):
            continue

        non_hidden.append(path)

    return non_hidden
