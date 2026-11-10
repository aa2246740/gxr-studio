"""已核对真实 DAT 与 ELF 的引擎身份，不包含引擎字节。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class EngineDescription:
    engine_sha256: str
    entry: int
    bss_bytes: int
    relocations: tuple[tuple[int, int], ...]
    linked_data_end: int


BOUND_ENGINES = {
    156: (
        EngineDescription("e406f24535118960ec63a835c918659c87a53be0b279e8687d0f836cae17d654", 61984, 9284, ((65840, 56136),), 88684),
        EngineDescription("688427df58d334a375b125f1b5574ac6f0854962f5123859e47e5030c0052bf0", 64892, 9296, ((68748, 59044),), 90532),
    ),
    158: (
        EngineDescription("86c9eb3a42d5d43f8c8fefa07b574f2ba723f750256d31472d762fbd0885528c", 63832, 4360, ((67484, 57984),), 92484),
        EngineDescription("441531dc1900ae5c3cf2e62092c5c6dc88a1f326b7cb74f8506159245fd2b34e", 66056, 4360, ((69716, 60208),), 94952),
    ),
    176: (EngineDescription("099b71fd8a1f4e9418c8fdde0b3ebc38c035e0d28f0877b4ee4cc52981c1cc76", 70732, 4248, ((74492, 70700),), 74664),),
    178: (EngineDescription("1fd9fb0de74e535cef5dd91c939c2bcac09772cf038b97955e25baa68d2b4c85", 71756, 23772, ((75552, 71724),), 75724),),
    181: (EngineDescription("28013529e2291f3854622dbc46ebefce10098689aa000cb0f512351838f2102f", 72308, 4252, ((76076, 65220),), 76248),),
    182: (EngineDescription("2391f3a55c036888750dfde9ca2d28748fe3fb4a85edd3ee0385be17f7803cc9", 72480, 4252, ((76264, 62800),), 76436),),
}
