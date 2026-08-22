"""
Regression tests for episode filename parsing.

Every case here is a real filename observed in Torrentio results (~1000
harvested across anime and western series), or a real failure Zapflix hit in
production. Run with:  python -m pytest test_naming.py -q
                or:   python test_naming.py
"""

from naming import (parse_media_filename, canonical_name, season_folder,
                    absolute_to_season_episode)

SLIME = {1: 24, 2: 24, 3: 24, 4: 24}
BLACK_TORCH = {1: 12}
TEEN_WOLF = {1: 12, 2: 12, 3: 24, 4: 12, 5: 20, 6: 20}
JJK = {1: 24, 2: 23, 3: 12}
FRIEREN = {1: 28, 2: 10}
BREAKING_BAD = {1: 7, 2: 13, 3: 13, 4: 13, 5: 16}
# Real Cinemeta season split (verified live) — One Piece's seasons are uneven,
# so absolute-number mapping must follow the metadata, not an assumption.
ONE_PIECE = {1: 8, 2: 22, 3: 17, 4: 13, 5: 9, 6: 22, 7: 39, 8: 13, 9: 52,
             10: 31, 11: 99, 12: 56, 13: 100, 14: 35, 15: 62, 16: 49, 17: 118,
             18: 33, 19: 98, 20: 14, 21: 194, 22: 70, 23: 20}

# (filename, season_counts, pack_seasons, expected)
#   expected: "SxxEyy" | "extra" | "unknown"
CASES = [
    # ── The production bug: an 81-file anime pack whose episodes carried no
    # SxxEyy at all. Every one of these used to be discarded as "bonus".
    ("/[Anime Time] Tensei Shitara Slime Datta Ken/[Anime Time] Tensei Shitara Slime Datta Ken 01.mkv",
     SLIME, {1, 2}, "S01E01"),
    ("[Anime Time] Tensei Shitara Slime Datta Ken 24.mkv", SLIME, {1, 2}, "S01E24"),
    ("[Anime Time] Tensei Shitara Slime Datta Ken 25.mkv", SLIME, {1, 2}, "S02E01"),
    ("[Anime Time] Tensei Shitara Slime Datta Ken 48.mkv", SLIME, {1, 2}, "S02E24"),

    # ── Explicit markers still win
    ("That Time I Got Reincarnated as a Slime - S03E01.mkv", SLIME, {3}, "S03E01"),
    ("That Time I Got Reincarnated as a Slime - S00E13 - [Digression].mkv", SLIME, {3}, "S00E13"),
    ("Teen Wolf (2011) - S04E02 - 117 (1080p BluRay x265 Celdra).mkv", TEEN_WOLF, {4}, "S04E02"),
    ("Show.S02E03.1080p.WEB-DL.x265.AAC.mkv", {2: 10}, {2}, "S02E03"),

    # ── "BS11" is a TV station, not season 11 (used to file under Season 11)
    ("[YE] BLACK TORCH - 05 (BS11 1280x720 x265 10bit AAC).mkv", BLACK_TORCH, {1}, "S01E05"),
    ("BLACK TORCH - S01E06 - Turn Up - 2160p HDR Ai Upscale -Mesc.mkv", BLACK_TORCH, {1}, "S01E06"),

    # ── Separator styles
    ("[JAM_CLUB]_Tensei_Shitara_Slime_Datta_Ken_01_[1080p][RUS][DUB].mp4", SLIME, {1}, "S01E01"),
    ("[Cleo]Tensei_shitara_Slime_Datta_Ken_-_01_(Dual Audio_10bit_BD1080p_x265).mkv",
     SLIME, {1}, "S01E01"),
    ("Teen.Wolf.4x02.117.ITA-ENG.720p.DLMux.DD5.1.h264-NovaRip.mkv", TEEN_WOLF, {4}, "S04E02"),
    ("Demon_Slayer_01_FR_HD.mp4", {1: 26}, {1}, "S01E01"),

    # ── Number before the title / leading number
    ("01. Tensei shitara Slime Datta Ken (BDRip 1080p HEVC).mkv", SLIME, {1}, "S01E01"),
    ("01 - The Storm Dragon, Verudora.mkv", SLIME, {1}, "S01E01"),
    ("One Piece 001 - Yo soy Luffy, el futuro rey de los piratas.avi", ONE_PIECE, {1}, "S01E01"),
    ("514 Ozymandias.mkv", BREAKING_BAD, None, "S05E14"),          # concatenated SxxEyy

    # ── Trailing metadata groups must not hide the number
    ("One Piece 001 [Shichibukai] [5D713620].avi", ONE_PIECE, {1}, "S01E01"),
    ("One Piece [HDTV 1080p][Cap.101](wolfmax4k.com).mkv", ONE_PIECE, None, "S07E10"),
    ("[No0bSubs] Frieren - Beyond Journey's End S1 20 (1080p AV1 MULTI Audio)[DD77F995].mkv",
     FRIEREN, {1}, "S01E20"),
    ("[Erai-raws] Tensei shitara Slime Datta Ken 2nd Season - 12 END [1080p][Multiple Subtitle].mkv",
     SLIME, {2}, "S02E12"),

    # ── "2nd Season 25": the season is 2 and 25 is a continuous episode number.
    # Reading "Season 25" as the season produced S25E25.
    ("[Asakura] Tensei Shitara Slime Datta Ken 2nd Season 25 [BDRip].mkv", SLIME, {2}, "S02E01"),
    ("[Beatrice-Raws] Tensei Shitara Slime Datta Ken 2nd Season 01 [BDRip 1920x1080].mkv",
     SLIME, {2}, "S02E01"),

    # ── Releases that label continuous numbering as SxxEyy anyway
    ("Jujutsu Kaisen S02E29 1080p WEBDL x265.mkv", JJK, None, "S02E05"),
    ("[AF]Frieren.Beyond.Journeys.End.S02E020[BDRemux].mkv", FRIEREN, None, "S01E20"),
    ("One.Piece.S01E0382.KOREAN.720p.JBOX.WEB-DL.H264-R1.mkv", ONE_PIECE, None, "S13E01"),

    # ── Titles containing numbers must not be read as the episode
    ("[Erai-raws] 86 - Eighty Six - 07 [1080p][AAC].mkv", {1: 11, 2: 12}, {1}, "S01E07"),
    ("[Group] Gundam 00 - 05 (1080p).mkv", {1: 25}, {1}, "S01E05"),
    ("[Group] Show - 05v2 (1080p) [A1B2C3D4].mkv", {1: 12}, {1}, "S01E05"),

    # ── Bonus material: dropped, and never mistaken for an episode
    ("Gag Reel.mkv", TEEN_WOLF, {1}, "extra"),
    ("Shirtless Montage 1.0.mkv", TEEN_WOLF, {1}, "extra"),
    ("VFX Breakdown.mkv", TEEN_WOLF, {1}, "extra"),
    ("Show.NCOP.01.1080p.mkv", {1: 12}, {1}, "extra"),
    ("[Group] Show - NCED 02 (1080p).mkv", {1: 12}, {1}, "extra"),
    ("Deleted, Alternate and Extended Scenes.mkv", TEEN_WOLF, {1}, "extra"),
    ("cover.jpg", TEEN_WOLF, {1}, "extra"),

    # ── Spin-offs inside a pack go to specials, never over main episodes
    ("[Anime Time] Tensura Nikki - 03 (BD 1080p).mkv", SLIME, {1, 2}, "S00E03"),
    ("[SubsPlease] Show - 05.5 (1080p) [ABCD1234].mkv", {1: 12}, {1}, "S00E05"),

    # ── Genuinely ambiguous: better left alone than misfiled
    ("[Erai-raws] Tensei shitara Slime Datta Ken 2nd Season - S1 [1080p].mkv", SLIME, {2}, "unknown"),
]


def _result(parsed) -> str:
    if parsed.is_episode:
        return f"S{parsed.season:02d}E{parsed.episode:02d}"
    return parsed.kind


def test_absolute_mapping():
    assert absolute_to_season_episode(1, SLIME) == (1, 1)
    assert absolute_to_season_episode(25, SLIME) == (2, 1)
    assert absolute_to_season_episode(73, SLIME) == (4, 1)
    assert absolute_to_season_episode(96, SLIME) == (4, 24)
    assert absolute_to_season_episode(97, SLIME) is None   # out of range
    assert absolute_to_season_episode(0, SLIME) is None


def test_canonical_name_and_folder():
    p = parse_media_filename("[Anime Time] Show 25.mkv", SLIME, {1, 2})
    assert canonical_name("My Show", p, "[Anime Time] Show 25.mkv") == "My Show - S02E01.mkv"
    assert season_folder(p) == "Season 02"
    # Unparseable files keep their original name
    q = parse_media_filename("Love Bites!.mkv", SLIME, {1})
    assert canonical_name("My Show", q, "Love Bites!.mkv") == "Love Bites!.mkv"
    # Illegal path characters are stripped from the series name
    assert "/" not in canonical_name("A/B: C?", p, "x.mkv")


def test_real_world_filenames():
    failures = []
    for path, counts, packs, expected in CASES:
        got = _result(parse_media_filename(path, counts, packs))
        if got != expected:
            failures.append(f"{path}\n    expected {expected}, got {got}")
    assert not failures, "\n  " + "\n  ".join(failures)


if __name__ == "__main__":
    test_absolute_mapping()
    test_canonical_name_and_folder()
    passed = 0
    for path, counts, packs, expected in CASES:
        p = parse_media_filename(path, counts, packs)
        got = _result(p)
        ok = got == expected
        passed += ok
        flag = "ok  " if ok else "FAIL"
        print(f"{flag} {got:<8} exp {expected:<8} {(p.how or p.note)[:36]:<36} {path[-46:]}")
    print(f"\n{passed}/{len(CASES)} filename cases pass")
    raise SystemExit(0 if passed == len(CASES) else 1)
