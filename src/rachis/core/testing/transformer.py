# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------
import collections
from rachis import Metadata
import pandas as pd

from .format import (
    FourIntsDirectoryFormat,
    MappingDirectoryFormat,
    IntSequenceFormat,
    IntSequenceFormatV2,
    IntSequenceDirectoryFormat,
    IntSequenceV2DirectoryFormat,
    IntSequenceMultiFileDirectoryFormat,
    SingleIntFormat,
    MappingFormat,
    UnimportableFormat,
    RedundantSingleIntDirectoryFormat,
    EchoFormat,
    ImportableOnlyFormat,
    ExportableOnlyFormat,
    FirstStepFormat,
    SecondStepFormat,
    ThirdStepFormat,
    FourthStepFormat,
    FifthStepFormat,
)
from .plugin import dummy_plugin, citations


@dummy_plugin.register_transformer
def _2(data: int) -> SingleIntFormat:
    ff = SingleIntFormat()
    with ff.open() as fh:
        fh.write('%d\n' % data)
    return ff


@dummy_plugin.register_transformer
def _5(ff: SingleIntFormat) -> int:
    with ff.open() as fh:
        return int(fh.read())


@dummy_plugin.register_transformer(citations=[citations['krauth2012depth']])
def _7(data: list) -> IntSequenceFormat:
    ff = IntSequenceFormat()
    with ff.open() as fh:
        for int_ in data:
            fh.write(f'{int_}\n')
    return ff


@dummy_plugin.register_transformer(citations=citations)
def _77(data: list) -> IntSequenceFormatV2:
    ff = IntSequenceFormatV2()
    with ff.open() as fh:
        fh.write('VERSION 2\n')
        for int_ in data:
            fh.write('%d\n' % int_)
    return ff


@dummy_plugin.register_transformer
def _9(ff: IntSequenceFormat) -> list:
    with ff.open() as fh:
        return list(map(int, fh.readlines()))


@dummy_plugin.register_transformer
def _99(ff: IntSequenceFormatV2) -> list:
    with ff.open() as fh:
        fh.readline()  # skip header
        return list(map(int, fh.readlines()))


@dummy_plugin.register_transformer
def _10(ff: IntSequenceFormat) -> collections.Counter:
    with ff.open() as fh:
        return collections.Counter(map(int, fh.readlines()))


@dummy_plugin.register_transformer
def _1010(ff: IntSequenceFormatV2) -> collections.Counter:
    with ff.open() as fh:
        fh.readline()  # skip header
        return collections.Counter(map(int, fh.readlines()))


@dummy_plugin.register_transformer
def _1000(ff: IntSequenceFormat) -> IntSequenceFormatV2:
    new_ff = IntSequenceFormatV2()

    with new_ff.open() as new_fh, ff.open() as fh:
        new_fh.write("VERSION 2\n")
        for line in fh:
            new_fh.write(line)

    return new_ff


# This only exists to test `get_formats` and is functionally useless otherwise
@dummy_plugin.register_transformer
def _1100(df: IntSequenceMultiFileDirectoryFormat) -> \
        IntSequenceDirectoryFormat:
    return IntSequenceDirectoryFormat()


@dummy_plugin.register_transformer
def _1001(df: IntSequenceV2DirectoryFormat) -> \
        IntSequenceMultiFileDirectoryFormat:
    return IntSequenceMultiFileDirectoryFormat()


@dummy_plugin.register_transformer
def _0202(data: int) -> RedundantSingleIntDirectoryFormat:
    df = RedundantSingleIntDirectoryFormat()
    df.int1.write_data(data, int)
    df.int2.write_data(data, int)
    return df


@dummy_plugin.register_transformer
def _2020(ff: RedundantSingleIntDirectoryFormat) -> int:
    return ff.int1.view(int)  # int2 must be the same for this format


@dummy_plugin.register_transformer
def _11(data: dict) -> MappingDirectoryFormat:
    df = MappingDirectoryFormat()
    df.mapping.write_data(data, dict)
    return df


@dummy_plugin.register_transformer
def _12(data: dict) -> MappingFormat:
    ff = MappingFormat()
    with ff.open() as fh:
        for key, value in data.items():
            fh.write('%s\t%s\n' % (key, value))
    return ff


@dummy_plugin.register_transformer(citations=[citations['silvers1997effects']])
def _13(df: MappingDirectoryFormat) -> dict:
    # If this had been a `SingleFileDirectoryFormat` then this entire
    # transformer would have been redundant (the framework could infer it).
    return df.mapping.view(dict)


@dummy_plugin.register_transformer
def _14(ff: MappingFormat) -> dict:
    data = {}
    with ff.open() as fh:
        for line in fh:
            key, value = line.rstrip('\n').split('\t')
            if key in data:
                raise ValueError(
                    "mapping.txt file must have unique keys. Key %r was "
                    "observed more than once." % key)
            data[key] = value
    return data


@dummy_plugin.register_transformer
def _15(df: MappingDirectoryFormat) -> Metadata:
    d = df.mapping.view(dict)
    return Metadata(pd.DataFrame(d, index=pd.Index(["0"], name='id')))


@dummy_plugin.register_transformer
def _3(df: FourIntsDirectoryFormat) -> list:
    # Note: most uses of `iter_views` will need to look at the first element
    # of the series of tuples provided by iter_views
    return [x for _, x in df.single_ints.iter_views(int)]


@dummy_plugin.register_transformer
def _1(data: list) -> FourIntsDirectoryFormat:
    df = FourIntsDirectoryFormat()
    for i, int_ in enumerate(data, 1):
        df.single_ints.write_data(int_, int, num=i)
    return df


@dummy_plugin.register_transformer
def _4(ff: UnimportableFormat) -> int:
    return 1


@dummy_plugin.register_transformer
def _a1(data: str) -> EchoFormat:
    ff = EchoFormat()
    with ff.open() as fh:
        fh.write(data)
    return ff


# only for testing get_formats - otherwise useless
@dummy_plugin.register_transformer()
def _4242(data: ImportableOnlyFormat) -> IntSequenceDirectoryFormat:
    return IntSequenceDirectoryFormat()


# only for testing get_formats - otherwise useless
@dummy_plugin.register_transformer()
def _4243(data: IntSequenceDirectoryFormat) -> ExportableOnlyFormat:
    return ExportableOnlyFormat()


# only for testing transitive transformers
@dummy_plugin.register_transformer(upgrade=True)
def _6000(data: FirstStepFormat) -> SecondStepFormat:
    return SecondStepFormat()


# only for testing transitive transformers
@dummy_plugin.register_transformer(upgrade=True)
def _6001(data: SecondStepFormat) -> ThirdStepFormat:
    return ThirdStepFormat()


# only for testing transitive transformers
@dummy_plugin.register_transformer
def _6002(data: ThirdStepFormat) -> FourthStepFormat:
    return FourthStepFormat()


# only for testing transitive transformers
@dummy_plugin.register_transformer(upgrade=False)
def _6003(data: ThirdStepFormat) -> FifthStepFormat:
    return FifthStepFormat()
