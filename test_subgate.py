import json
import unittest

import subgate as sg


def stream(kind, lang=None, title=None, forced=0, codec="subrip"):
    s = {"codec_type": kind, "codec_name": codec, "disposition": {"forced": forced}, "tags": {}}
    if lang is not None:
        s["tags"]["language"] = lang
    if title is not None:
        s["tags"]["title"] = title
    return s


VIDEO = stream("video", codec="h264")


class ClassifySubPath(unittest.TestCase):
    def check(self, path, expected):
        self.assertEqual(sg.classify_sub_path(path), expected, path)

    def test_unlabelled(self):
        self.check("Movie.2017.1080p.srt", "unknown")

    def test_english_folder_style(self):
        self.check("Movie.2017/Subs/2_English.srt", "english")
        self.check("Subs/2_English.srt", "english")

    def test_three_letter(self):
        self.check("Movie.spa.srt", "other")
        self.check("Movie.2019.eng.srt", "english")

    def test_forced(self):
        self.check("English.forced.srt", "forced")

    def test_season_pack_folder(self):
        self.check("Show.S01/Subs/Show.S01E01/3_Spanish.srt", "other")
        self.check("Show.S01/Subs/English/Show.S01E01.srt", "english")

    def test_two_letter_trailing(self):
        self.check("Movie.de.srt", "other")
        self.check("Movie.en.srt", "english")

    def test_title_words_are_not_languages(self):
        self.check("The.Den.2023.srt", "unknown")
        self.check("Ben-Hur.1959.srt", "unknown")
        self.check("May.December.2023/May.December.2023.srt", "unknown")

    def test_root_folder_ignored(self):
        self.check("The.English.Patient.1996/Spanish.srt", "other")
        self.check("Le.Film.French.2020/Movie.srt", "unknown")

    def test_trailing_tag_beats_title_word(self):
        self.check("The.English.Patient.1996.spa.srt", "other")


class PickTarget(unittest.TestCase):
    def test_largest_non_sample(self):
        files = [
            {"index": 0, "name": "M/Sample/m-sample.mkv", "size": 900},
            {"index": 1, "name": "M/m.mkv", "size": 800},
            {"index": 2, "name": "M/m.nfo", "size": 10_000},
            {"index": 3, "name": "M/sample.mkv", "size": 5000},
        ]
        self.assertEqual(sg.pick_target_file(files)["index"], 1)

    def test_none(self):
        self.assertIsNone(sg.pick_target_file([{"index": 0, "name": "x.iso", "size": 1}]))


class Pieces(unittest.TestCase):
    def test_contiguous_single_file(self):
        ps = 4 * sg.MIB
        self.assertEqual(sg.contiguous_head_bytes(0, 100 * sg.MIB, ps, [2, 2, 1, 2, 0]), 8 * sg.MIB)
        self.assertEqual(sg.contiguous_head_bytes(0, 100 * sg.MIB, ps, [0, 2, 2]), 0)

    def test_contiguous_offset_inside_piece(self):
        ps = 4 * sg.MIB
        # file starts 1 MiB into piece 2; pieces 2,3 done -> 7 MiB of the file
        off = 2 * ps + sg.MIB
        self.assertEqual(sg.contiguous_head_bytes(off, 50 * sg.MIB, ps, [0, 0, 2, 2, 0]), 7 * sg.MIB)

    def test_contiguous_capped_at_size(self):
        self.assertEqual(sg.contiguous_head_bytes(0, 3 * sg.MIB, 4 * sg.MIB, [2]), 3 * sg.MIB)

    def test_file_offset(self):
        files = [{"index": 0, "size": 10}, {"index": 1, "size": 20}, {"index": 2, "size": 5}]
        self.assertEqual(sg.file_offset(files, files[2]), 30)

    def test_tail(self):
        self.assertEqual(sg.tail_pieces([10, 100], 4 * sg.MIB, 8 * sg.MIB), [99, 100])
        self.assertEqual(sg.tail_pieces([10, 11], 4 * sg.MIB, 64 * sg.MIB), [10, 11])


class DecideStreams(unittest.TestCase):
    def decide(self, streams, und=True):
        return sg.decide_streams({"streams": streams}, und)[0]

    def test_eng(self):
        self.assertEqual(self.decide([VIDEO, stream("subtitle", "eng")]), "pass")

    def test_hindi_audio_movies_only(self):
        hi = [VIDEO, stream("audio", "hin")]
        self.assertEqual(sg.decide_streams({"streams": hi}, True)[0], "fail")
        self.assertEqual(sg.decide_streams({"streams": hi}, True, hindi_ok=True)[0], "pass")
        titled = [VIDEO, stream("audio", "und", title="Hindi 5.1")]
        self.assertEqual(sg.decide_streams({"streams": titled}, True, hindi_ok=True)[0], "pass")
        tamil = [VIDEO, stream("audio", "tam")]
        self.assertEqual(sg.decide_streams({"streams": tamil}, True, hindi_ok=True)[0], "fail")

    def test_tur_eng(self):
        self.assertEqual(self.decide([VIDEO, stream("subtitle", "tur"), stream("subtitle", "eng")]), "pass")

    def test_only_tur(self):
        verdict, details = sg.decide_streams({"streams": [VIDEO, stream("subtitle", "tur")]}, True)
        self.assertEqual(verdict, "fail")
        self.assertIn("tur", details)

    def test_only_forced_eng(self):
        self.assertEqual(self.decide([VIDEO, stream("subtitle", "eng", forced=1)]), "fail")

    def test_forced_flag_on_full_track(self):
        full = stream("subtitle", "eng", forced=1)
        full["tags"]["NUMBER_OF_FRAMES-eng"] = "1450"
        self.assertEqual(self.decide([VIDEO, full]), "pass")

    def test_forced_flag_few_events(self):
        signs = stream("subtitle", "eng", forced=1)
        signs["tags"]["NUMBER_OF_FRAMES"] = "4"
        self.assertEqual(self.decide([VIDEO, signs]), "fail")

    def test_forced_title_beats_event_count(self):
        signs = stream("subtitle", "eng", title="Signs & Songs")
        signs["tags"]["NUMBER_OF_FRAMES"] = "900"
        self.assertEqual(self.decide([VIDEO, signs]), "fail")

    def test_fail_reason_marks_forced(self):
        _, why = sg.decide_streams({"streams": [VIDEO, stream("subtitle", "eng", forced=1),
                                                     stream("audio", "jpn")]}, True)
        self.assertIn("subtitles: eng forced", why)
        self.assertEqual(self.decide([VIDEO, stream("subtitle", "eng", title="English (Forced)")]), "fail")

    def test_signs_songs(self):
        self.assertEqual(self.decide([VIDEO, stream("subtitle", "eng", title="Signs & Songs")]), "fail")
        self.assertEqual(self.decide([VIDEO, stream("subtitle", "eng", title="Signs & Songs"),
                                      stream("subtitle", "eng", title="Full")]), "pass")

    def test_und(self):
        self.assertEqual(self.decide([VIDEO, stream("subtitle", "und")], und=True), "pass")
        self.assertEqual(self.decide([VIDEO, stream("subtitle")], und=True), "pass")
        self.assertEqual(self.decide([VIDEO, stream("subtitle", "und")], und=False), "fail")
        self.assertEqual(self.decide([VIDEO, stream("subtitle", "und", title="Spanish")], und=True), "fail")

    def test_title_names_english(self):
        self.assertEqual(self.decide([VIDEO, stream("subtitle", "und", title="English SDH")], und=False), "pass")

    def test_en_variants(self):
        self.assertEqual(self.decide([VIDEO, stream("subtitle", "en-US")]), "pass")
        self.assertEqual(self.decide([VIDEO, stream("subtitle", "en")]), "pass")

    def test_no_subs(self):
        verdict, details = sg.decide_streams({"streams": [VIDEO, stream("audio", "jpn")]}, True)
        self.assertEqual(verdict, "fail")
        self.assertIn("subtitles: none", details)

    def test_no_video_inconclusive(self):
        self.assertEqual(self.decide([stream("subtitle", "eng")]), "inconclusive")

    def test_bad_json(self):
        self.assertEqual(sg.decide_streams("not json", True)[0], "inconclusive")
        self.assertEqual(sg.decide_streams(json.dumps({"streams": [VIDEO, stream("subtitle", "eng")]}), True)[0], "pass")


class EnglishAudio(unittest.TestCase):
    def decide(self, streams, orig_en=False, und=True):
        return sg.decide_streams({"streams": [VIDEO] + streams}, und, orig_en)[0]

    def test_eng_dub_without_subs(self):
        self.assertEqual(self.decide([stream("audio", "jpn"), stream("audio", "eng")]), "pass")

    def test_foreign_subs_but_eng_audio(self):
        self.assertEqual(self.decide([stream("audio", "eng"), stream("subtitle", "tur")]), "pass")

    def test_audio_title_english(self):
        self.assertEqual(self.decide([stream("audio", "und", title="English Dub")]), "pass")

    def test_commentary_does_not_count(self):
        self.assertEqual(self.decide([stream("audio", "jpn"),
                                      stream("audio", "eng", title="Director's Commentary")]), "fail")
        s = stream("audio", "eng")
        s["disposition"]["comment"] = 1
        self.assertEqual(self.decide([stream("audio", "jpn"), s]), "fail")

    def test_und_audio_needs_english_original(self):
        # YTS mp4: audio "und", no subs (Split vs Pompo)
        self.assertEqual(self.decide([stream("audio", "und")], orig_en=True), "pass")
        self.assertEqual(self.decide([stream("audio", "und")], orig_en=False), "fail")

    def test_und_audio_not_affected_by_accept_undetermined(self):
        self.assertEqual(self.decide([stream("audio", "und")], orig_en=False, und=True), "fail")

    def test_jpn_audio_eng_original_still_fails(self):
        self.assertEqual(self.decide([stream("audio", "jpn")], orig_en=True), "fail")

    def test_needs_original_language(self):
        self.assertTrue(sg.needs_original_language({"streams": [VIDEO, stream("audio", "und")]}))
        self.assertTrue(sg.needs_original_language(json.dumps({"streams": [stream("audio")]})))
        self.assertFalse(sg.needs_original_language({"streams": [VIDEO, stream("audio", "jpn")]}))
        self.assertFalse(sg.needs_original_language("garbage"))


class Paths(unittest.TestCase):
    def test_incomplete_prefers_download_path(self):
        self.assertEqual(sg.resolve_paths(0.3, "/d/radarr", "/d/inc/radarr", "M/m.mkv"),
                         ["/d/inc/radarr/M/m.mkv", "/d/radarr/M/m.mkv",
                          "/d/inc/radarr/M/m.mkv.!qB", "/d/radarr/M/m.mkv.!qB"])

    def test_complete_prefers_save_path(self):
        self.assertEqual(sg.resolve_paths(1.0, "/d/radarr", "/d/inc/radarr", "m.mkv")[0], "/d/radarr/m.mkv")

    def test_no_download_path(self):
        self.assertEqual(sg.resolve_paths(0.3, "/d/radarr", "", "m.mkv"), ["/d/radarr/m.mkv", "/d/radarr/m.mkv.!qB"])


class States(unittest.TestCase):
    def test_active(self):
        for s in ("downloading", "stalledDL", "metaDL", "forcedDL", "forcedMetaDL"):
            self.assertTrue(sg.is_active_state(s))
        for s in ("queuedDL", "stoppedDL", "checkingDL", "uploading", "stalledUP"):
            self.assertFalse(sg.is_active_state(s))


if __name__ == "__main__":
    unittest.main()
