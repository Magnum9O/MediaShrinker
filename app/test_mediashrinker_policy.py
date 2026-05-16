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
    iter_bitmap_ocr_sample_times,
    parse_vobsub_idx_timestamps,
    pick_transcode_cq,
    score_ocr_text,
    vobsub_idx_to_srt,
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

    def test_got_style_vobsub_inventory_only_targets_eng_ita_for_ocr(self) -> None:
        inv = SubtitleInventory(
            text=[],
            non_text=[
                mk_track(7, "VobSub", "dan"),
                mk_track(8, "VobSub", "dut"),
                mk_track(9, "VobSub", "eng"),
                mk_track(10, "VobSub", "eng"),
                mk_track(11, "VobSub", "fin"),
                mk_track(12, "VobSub", "fre"),
                mk_track(13, "VobSub", "fre"),
                mk_track(14, "VobSub", "ger"),
                mk_track(15, "VobSub", "ger"),
                mk_track(16, "VobSub", "ita"),
                mk_track(17, "VobSub", "ita"),
                mk_track(18, "VobSub", "nob"),
                mk_track(19, "VobSub", "por"),
                mk_track(20, "VobSub", "por"),
                mk_track(21, "VobSub", "spa"),
                mk_track(22, "VobSub", "spa"),
                mk_track(23, "VobSub", "spa"),
                mk_track(24, "VobSub", "swe"),
            ],
        )
        sp = build_sub_plan(inv, external_text_langs=set())
        self.assertEqual([x.track_id for x in sp.ocr_tasks], [9, 10, 16, 17])
        self.assertEqual(sp.drop_ids, [9, 10, 16, 17])
        self.assertEqual(
            sp.keep_ids,
            [7, 8, 11, 12, 13, 14, 15, 18, 19, 20, 21, 22, 23, 24],
        )
        eng = next(x for x in sp.audit if x.lang == "eng")
        ita = next(x for x in sp.audit if x.lang == "ita")
        spa = next(x for x in sp.audit if x.lang == "spa")
        self.assertEqual(eng.decision_ocr, "all")
        self.assertEqual(ita.decision_ocr, "all")
        self.assertEqual(spa.decision_ocr, "none")
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
        self.assertFalse(is_supported_bitmap_sub_codec("ASS"))
        self.assertFalse(is_supported_bitmap_sub_codec(""))

    def test_parse_vobsub_idx_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            idx_path = Path(td) / "track.idx"
            idx_path.write_text(
                "# VobSub index file\n"
                "timestamp: 00:00:01:500, filepos: 000000000\n"
                "timestamp: 00:00:03:000, filepos: 000000111\n",
                encoding="utf-8",
            )
            windows = parse_vobsub_idx_timestamps(idx_path)
            self.assertEqual(len(windows), 2)
            self.assertAlmostEqual(windows[0][0], 1.5)
            self.assertAlmostEqual(windows[0][1], 2.999)
            self.assertAlmostEqual(windows[1][0], 3.0)
            self.assertAlmostEqual(windows[1][1], 7.999)

    def test_iter_bitmap_ocr_sample_times_spans_window(self) -> None:
        samples = iter_bitmap_ocr_sample_times(10.0, 12.0)
        self.assertGreaterEqual(len(samples), 3)
        self.assertGreater(samples[0], 10.0)
        self.assertLess(samples[-1], 12.0)
        self.assertEqual(samples, sorted(samples))

    def test_score_ocr_text_prefers_longer_real_text(self) -> None:
        self.assertGreater(score_ocr_text("Hello there"), score_ocr_text("Hi"))
        self.assertEqual(score_ocr_text(" \n "), 0)

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

    def test_vobsub_idx_to_srt_uses_best_non_empty_sample_in_window(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            idx_path = td_path / "track.idx"
            idx_path.write_text(
                "# VobSub index file\n"
                "timestamp: 00:00:10:000, filepos: 000000000\n",
                encoding="utf-8",
            )

            def fake_render(ffmpeg_bin, idx_path_arg, out_png, *, at_sec):
                out_png.write_text(f"{at_sec:.3f}", encoding="utf-8")

            def fake_ocr(image_path, *, tessdata_prefix, ocr_langs):
                sample_sec = float(image_path.read_text(encoding="utf-8"))
                if sample_sec < 10.2:
                    return ""
                if sample_sec < 10.5:
                    return "Hi"
                return "This is the subtitle text"

            with patch("mediashrinker.render_vobsub_event_image", side_effect=fake_render), \
                 patch("mediashrinker.ocr_bitmap_image_to_text", side_effect=fake_ocr):
                srt = vobsub_idx_to_srt(
                    ffmpeg_bin="ffmpeg",
                    idx_path=idx_path,
                    tessdata_prefix=td,
                    ocr_langs=["eng"],
                )

            content = srt.read_text(encoding="utf-8")
            self.assertIn("This is the subtitle text", content)
            self.assertNotIn("\nHi\n", content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
