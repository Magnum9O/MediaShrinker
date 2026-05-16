import unittest
import tempfile

from pathlib import Path
from unittest.mock import patch

from mediashrinker import (
    Analysis,
    SubtitleInventory,
    SubtitleTrack,
    build_sub_plan,
    detect_lang_from_text,
    detect_lang_from_subtitle_payload,
    extract_pgs_for_pgsrip,
    extract_vobsub_for_ocr,
    infer_non_text_lang_via_probe_ocr,
    is_supported_bitmap_sub_codec,
    parse_vobsub_idx_timestamps,
    pick_transcode_cq,
)


def mk_track(
    tid: int,
    codec: str,
    lang: str,
    *,
    name: str = "",
    forced: bool = False,
    default: bool = False,
) -> SubtitleTrack:
    return SubtitleTrack(
        id=tid,
        codec=codec,
        lang=lang,
        name=name,
        forced=forced,
        default=default,
    )


class JellyfixSubtitlePolicyTests(unittest.TestCase):
    def test_drop_bitmap_when_internal_text_exists_same_lang(self) -> None:
        inv = SubtitleInventory(
            text=[mk_track(1, "SubRip/SRT", "ita")],
            non_text=[mk_track(2, "HDMV PGS", "ita")],
        )
        sp = build_sub_plan(inv, external_text_langs=set())
        self.assertEqual(sp.keep_ids, [1])
        self.assertEqual(sp.drop_ids, [2])
        self.assertTrue(sp.need_subfix)
        self.assertEqual(sp.ocr_tasks, [])

    def test_ocr_for_missing_target_lang_only(self) -> None:
        inv = SubtitleInventory(
            text=[mk_track(1, "SubRip/SRT", "ita")],
            non_text=[mk_track(2, "HDMV PGS", "eng")],
        )
        sp = build_sub_plan(inv, external_text_langs=set())
        self.assertEqual(len(sp.ocr_tasks), 1)
        self.assertEqual(sp.ocr_tasks[0].track_id, 2)
        self.assertEqual(sp.ocr_tasks[0].lang, "eng")
        self.assertTrue(sp.need_subfix)

    def test_ocr_when_only_non_text_target_exists(self) -> None:
        inv = SubtitleInventory(
            text=[],
            non_text=[mk_track(10, "HDMV PGS", "ita")],
        )
        sp = build_sub_plan(inv, external_text_langs=set())
        self.assertEqual([t.track_id for t in sp.ocr_tasks], [10])
        self.assertTrue(sp.need_subfix)

    def test_other_language_text_does_not_block_bitmap_ocr(self) -> None:
        inv = SubtitleInventory(
            text=[mk_track(1, "SubRip/SRT", "spa")],
            non_text=[
                mk_track(2, "HDMV PGS", "ita"),
                mk_track(3, "VobSub", "eng"),
            ],
        )
        sp = build_sub_plan(inv, external_text_langs=set())
        self.assertEqual(sorted(x.track_id for x in sp.ocr_tasks), [2, 3])
        self.assertEqual(sp.drop_ids, [2, 3])
        self.assertEqual(sp.keep_ids, [1])
        self.assertTrue(sp.need_subfix)

    def test_non_target_non_text_is_kept_without_ocr(self) -> None:
        inv = SubtitleInventory(
            text=[],
            non_text=[mk_track(7, "VobSub", "fra")],
        )
        sp = build_sub_plan(inv, external_text_langs=set())
        self.assertEqual(sp.ocr_tasks, [])
        self.assertEqual(sp.drop_ids, [])
        self.assertEqual(sp.keep_ids, [7])
        self.assertFalse(sp.need_subfix)

    def test_und_non_text_is_kept_without_ocr(self) -> None:
        inv = SubtitleInventory(
            text=[],
            non_text=[mk_track(8, "HDMV PGS", "und")],
        )
        sp = build_sub_plan(inv, external_text_langs=set())
        self.assertEqual(sp.ocr_tasks, [])
        self.assertEqual(sp.drop_ids, [])
        self.assertEqual(sp.keep_ids, [8])
        self.assertFalse(sp.need_subfix)

    def test_ocr_task_preserves_forced_flag(self) -> None:
        inv = SubtitleInventory(
            text=[],
            non_text=[mk_track(9, "HDMV PGS", "eng", forced=True)],
        )
        sp = build_sub_plan(inv, external_text_langs=set())
        self.assertEqual(len(sp.ocr_tasks), 1)
        self.assertTrue(sp.ocr_tasks[0].forced)
        self.assertTrue(sp.need_subfix)

    def test_vobsub_target_lang_is_sent_to_ocr(self) -> None:
        inv = SubtitleInventory(
            text=[],
            non_text=[mk_track(12, "VobSub", "ita")],
        )
        sp = build_sub_plan(inv, external_text_langs=set())
        self.assertEqual([x.track_id for x in sp.ocr_tasks], [12])
        self.assertEqual(sp.drop_ids, [12])
        self.assertTrue(sp.need_subfix)
        ita = next(x for x in sp.audit if x.lang == "ita")
        self.assertEqual(ita.decision_ocr, "all")

    def test_mixed_pgs_and_vobsub_both_are_ocr_tasks(self) -> None:
        inv = SubtitleInventory(
            text=[],
            non_text=[
                mk_track(13, "HDMV PGS", "eng"),
                mk_track(14, "VobSub", "eng"),
            ],
        )
        sp = build_sub_plan(inv, external_text_langs=set())
        self.assertEqual([x.track_id for x in sp.ocr_tasks], [13, 14])
        eng = next(x for x in sp.audit if x.lang == "eng")
        self.assertEqual(eng.decision_ocr, "all")
        self.assertTrue(sp.need_subfix)

    def test_ambiguous_external_text_counts_and_drops_bitmap(self) -> None:
        inv = SubtitleInventory(
            text=[],
            non_text=[mk_track(5, "HDMV PGS", "ita")],
        )
        sp = build_sub_plan(inv, external_text_langs={"und"})
        self.assertEqual(sp.ocr_tasks, [])
        self.assertEqual(sp.external_text_langs, ["und"])
        self.assertEqual(sp.drop_ids, [5])
        self.assertTrue(sp.need_subfix)

    def test_force_extract_subs_sets_need_subfix_even_without_ocr(self) -> None:
        inv = SubtitleInventory(text=[], non_text=[])
        sp = build_sub_plan(inv, external_text_langs=set(), force_extract_subs=True)
        self.assertTrue(sp.need_subfix)

    def test_detect_lang_from_text_known_aliases_only(self) -> None:
        self.assertEqual(detect_lang_from_text("movie.italiano.srt"), "ita")
        self.assertEqual(detect_lang_from_text("track.en-US.ass"), "eng")
        self.assertIsNone(detect_lang_from_text("random.abc.subtitle"))

    def test_detect_lang_from_subtitle_payload_ita(self) -> None:
        sample = (
            "Ciao, come stai? Questo e un test. "
            "Perche non vieni con noi? Grazie, allora ci vediamo presto."
        )
        self.assertEqual(detect_lang_from_subtitle_payload(sample), "ita")

    def test_detect_lang_from_subtitle_payload_eng(self) -> None:
        sample = (
            "Hello, this is a test. "
            "What are you doing there? Please come with us because they have your keys."
        )
        self.assertEqual(detect_lang_from_subtitle_payload(sample), "eng")

    def test_detect_lang_from_subtitle_payload_ambiguous(self) -> None:
        sample = "la la la test test test short words only"
        self.assertIsNone(detect_lang_from_subtitle_payload(sample))

    def test_pick_transcode_cq_movie_1080p(self) -> None:
        a = Analysis(
            path="x.mkv",
            size_bytes=1,
            container="matroska",
            v_codec="h264",
            v_bitrate_bps=39_500_000,
            v_width=1920,
            v_height=1080,
            dv_profile=None,
            dv_el_present=None,
            a_codecs=[],
            should_transcode=True,
            reasons=[],
        )
        self.assertEqual(pick_transcode_cq(a, "movie"), 24)

    def test_pick_transcode_cq_movie_4k(self) -> None:
        a = Analysis(
            path="x.mkv",
            size_bytes=1,
            container="matroska",
            v_codec="h264",
            v_bitrate_bps=41_000_000,
            v_width=3840,
            v_height=2160,
            dv_profile=None,
            dv_el_present=None,
            a_codecs=[],
            should_transcode=True,
            reasons=[],
        )
        self.assertEqual(pick_transcode_cq(a, "movie"), 22)

    def test_pick_transcode_cq_movie_1080p_unknown_bitrate(self) -> None:
        a = Analysis(
            path="x.mkv",
            size_bytes=1,
            container="matroska",
            v_codec="h264",
            v_bitrate_bps=None,
            v_width=1920,
            v_height=1080,
            dv_profile=None,
            dv_el_present=None,
            a_codecs=[],
            should_transcode=True,
            reasons=[],
        )
        self.assertEqual(pick_transcode_cq(a, "movie"), 24)

    def test_pick_transcode_cq_series_1080p(self) -> None:
        a = Analysis(
            path="x.mkv",
            size_bytes=1,
            container="matroska",
            v_codec="h264",
            v_bitrate_bps=20_000_000,
            v_width=1920,
            v_height=1080,
            dv_profile=None,
            dv_el_present=None,
            a_codecs=[],
            should_transcode=True,
            reasons=[],
        )
        self.assertEqual(pick_transcode_cq(a, "series"), 26)

    def test_infer_non_text_lang_via_probe_ocr_success(self) -> None:
        tr = mk_track(3, "HDMV PGS", "und")

        def fake_run_cmd_capture(cmd, *, env=None):
            outp = Path(cmd[-1].split(":", 1)[1])
            outp.write_text("dummy", encoding="utf-8")
            return (0, "", "")

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            with patch("mediashrinker.run_cmd_capture", side_effect=fake_run_cmd_capture), \
                 patch("mediashrinker.pgsrip_sup_to_srt") as pgsrip_mock:
                tmp_srt = td_path / "mediashrinker-test-probe.srt"
                tmp_srt.write_text(
                    "Hello this is a test and you are there because they have your keys",
                    encoding="utf-8",
                )
                pgsrip_mock.return_value = tmp_srt
                guessed = infer_non_text_lang_via_probe_ocr(
                    mkvextract="mkvextract",
                    mkv_path=td_path / "in.mkv",
                    tr=tr,
                    pgsrip_bin="pgsrip",
                    tessdata_prefix=td,
                )
                self.assertEqual(guessed, "eng")

    def test_infer_non_text_lang_via_probe_ocr_not_pgs(self) -> None:
        tr = mk_track(4, "VobSub", "und")
        guessed = infer_non_text_lang_via_probe_ocr(
            mkvextract="mkvextract",
            mkv_path=Path("C:/Temp/in.mkv"),
            tr=tr,
            pgsrip_bin="pgsrip",
            tessdata_prefix="C:/Temp",
        )
        self.assertIsNone(guessed)

    def test_extract_pgs_for_pgsrip_rejects_non_pgs_codec(self) -> None:
        tr = mk_track(15, "VobSub", "ita")
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(RuntimeError) as ctx:
                extract_pgs_for_pgsrip(
                    mkvextract="mkvextract",
                    local_src=Path(td) / "in.mkv",
                    tr=tr,
                    out_dir=Path(td),
                )
        self.assertIn("pgsrip OCR supports PGS only", str(ctx.exception))

    def test_supported_bitmap_codec_detection(self) -> None:
        self.assertTrue(is_supported_bitmap_sub_codec("HDMV PGS"))
        self.assertTrue(is_supported_bitmap_sub_codec("VobSub"))
        self.assertTrue(is_supported_bitmap_sub_codec("dvd_subtitle"))
        self.assertFalse(is_supported_bitmap_sub_codec("SubRip/SRT"))

    def test_parse_vobsub_idx_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            idx_path = Path(td) / "track.idx"
            idx_path.write_text(
                "# VobSub index file\n"
                "timestamp: 00:00:01:500, filepos: 000000000\n"
                "timestamp: 00:00:03:000, filepos: 000000111\n",
                encoding="utf-8",
            )
            self.assertEqual(parse_vobsub_idx_timestamps(idx_path), [(1.5, 2.999), (3.0, 7.999)])

    def test_extract_vobsub_for_ocr_uses_idx_output(self) -> None:
        tr = mk_track(16, "VobSub", "ita")

        def fake_run_cmd_capture(cmd, *, env=None):
            outp = Path(cmd[-1].split(":", 1)[1])
            outp.write_text("# idx", encoding="utf-8")
            return (0, "", "")

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            with patch("mediashrinker.run_cmd_capture", side_effect=fake_run_cmd_capture):
                outp = extract_vobsub_for_ocr(
                    mkvextract="mkvextract",
                    local_src=td_path / "in.mkv",
                    tr=tr,
                    out_dir=td_path,
                )
            self.assertEqual(outp.suffix, ".idx")
            self.assertTrue(outp.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
