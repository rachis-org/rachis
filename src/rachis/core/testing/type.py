# ----------------------------------------------------------------------------
# Copyright (c) 2016-2026, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import rachis.plugin as plugin


IntSequence1 = plugin.SemanticType('IntSequence1')
IntSequence2 = plugin.SemanticType('IntSequence2')
IntSequence3 = plugin.SemanticType('IntSequence3')
AscIntSequence = plugin.SemanticType('AscIntSequence')
Mapping = plugin.SemanticType('Mapping')
FourInts = plugin.SemanticType('FourInts')
SingleInt = plugin.SemanticType('SingleInt')

Kennel = plugin.SemanticType('Kennel', field_names='pet')
Dog = plugin.SemanticType('Dog', variant_of=Kennel.field['pet'])
Cat = plugin.SemanticType('Cat', variant_of=Kennel.field['pet'])
# Kennel[Dog | Cat]

C1 = plugin.SemanticType('C1', field_names='first')
C2 = plugin.SemanticType('C2', field_names=['first', 'second'],
                         variant_of=C1.field['first'],
                         field_members={'first': [C1], 'second': [C1]})
C3 = plugin.SemanticType('C3', field_names=['first', 'second', 'third'],
                         variant_of=[C1.field['first'], C2.field['first'],
                                     C2.field['second']],
                         field_members={'first': [C1, C2],
                                        'second': [C1, C2],
                                        'third': [C1, C2]})
_variants = [
    C1.field['first'], C2.field['first'], C3.field['first'],
    C2.field['second'], C3.field['second'],
    C3.field['third']
]
# C1[C2[C3[Foo, Bar, Baz], C1[Foo]]] ... etc
Foo = plugin.SemanticType('Foo', variant_of=_variants)
Bar = plugin.SemanticType('Bar', variant_of=_variants)
Baz = plugin.SemanticType('Baz', variant_of=_variants)

Squid = plugin.SemanticType('Squid')
Octopus = plugin.SemanticType('Octopus')
Cuttlefish = plugin.SemanticType('Cuttlefish')

FirstStep = plugin.SemanticType('FirstStep')
SecondStep = plugin.SemanticType('SecondStep')
ThirdStep = plugin.SemanticType('ThirdStep')
FourthStep = plugin.SemanticType('FourthStep')
FifthStep = plugin.SemanticType('FifthStep')
