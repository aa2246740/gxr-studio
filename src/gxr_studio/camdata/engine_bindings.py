"""已核对真实 DAT 与 ELF 的引擎身份，不包含引擎字节。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class EngineDescription:
    engine_sha256: str
    entry: int
    bss_bytes: int
    relocations: tuple[tuple[int, int], ...]
    linked_data_end: int
    xdata_sha256: str | None = None


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
    185: (EngineDescription("7c1ccba52638de97711602ad25c404504a2f618c0cddea01d7fa28e26d4b0ef7", 74760, 8468, ((78756, 64892),), 78928),),
    250: (
        EngineDescription(
            "0f35d588356501e528639f2d2b90ec948ff40f6e97debc3f7192be9b3a9e77e8",
            79052,
            8608,
            ((86352, 65340),),
            90440,
            "22f939465361925ea31bdb19e7f016b6d374bdb2f70b6e35449375e8df00d1a0",
        ),
    ),
    253: (
        EngineDescription(
            "25f66879e735ccb886c50cf772a7f762c17f720eec534db79f941e17ee831bdb",
            79528,
            8616,
            ((86828, 65472),),
            90932,
            "ec5a9de229d25c7b8fc580768d9154e4803d29ad343857f9e253dd94bae4e697",
        ),
    ),
    256: (
        EngineDescription(
            "55a289e0f8c5ae56761a9de8d12c1ecc293191c93284a2c6b8089f02a3b69964",
            79756,
            8648,
            ((87224, 65740),),
            91292,
            "1324689f9a6e22cbe27f5d1bf3d78c0092fcb64305e7dcf1537cc9da8488906c",
        ),
    ),
    257: (
        EngineDescription(
            "d3065103ca9165c1534eae872318f62dedf385ec08080040622b7c192121edf6",
            79656,
            8644,
            ((87124, 65640),),
            91192,
            "1324689f9a6e22cbe27f5d1bf3d78c0092fcb64305e7dcf1537cc9da8488906c",
        ),
    ),
    258: (
        EngineDescription(
            "1767ab7769f23c0f72b3d4c647ff00e35805e10b3d4d881ec1b6e5b1b4bd4845",
            79708,
            8644,
            ((87236, 65692),),
            91304,
            "1324689f9a6e22cbe27f5d1bf3d78c0092fcb64305e7dcf1537cc9da8488906c",
        ),
    ),
    259: (
        EngineDescription(
            "7282b5c9397fa0620fd1a46b422bf8a6c583e9a3a68c4f429133bb352dedf82e",
            80608,
            8680,
            ((88136, 66564),),
            92328,
            "ba83e5b8e2a18795146a0ddfed632d8a295b438039a99393fdb05bf9c057827a",
        ),
    ),
    260: (
        EngineDescription(
            "9cd51001bd44675994a1c663205cb916c1413f158de2df31c64744cee7fab682",
            81304,
            8680,
            ((88832, 67260),),
            93024,
            "ba83e5b8e2a18795146a0ddfed632d8a295b438039a99393fdb05bf9c057827a",
        ),
    ),
    262: (
        EngineDescription(
            "df8559ecd05777ad24906e1fb0671fbf7013283664e6b4c6b04c13ac47e0d201",
            81620,
            9160,
            ((89188, 67576),),
            93380,
            "ba83e5b8e2a18795146a0ddfed632d8a295b438039a99393fdb05bf9c057827a",
        ),
    ),
    266: (
        EngineDescription(
            "8fae9c79c895852d9090831c584255879dd5b6eb48473c0ebc4e1a03391693e9",
            85764,
            9164,
            ((93696, 102789), (93716, 71708)),
            97980,
            "105c98b490b86cf2cbb20e461925f3a2531f7e395c6e0f6f9254e16df45b1342",
        ),
    ),
}
