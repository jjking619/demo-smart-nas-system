import importlib
import inspect
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import local_voice_chat


class OpenSourceWakeCleanupTest(unittest.TestCase):
    def test_legacy_kws_helpers_removed(self):
        source = inspect.getsource(local_voice_chat)
        for needle in (
            "_resolve_legacy_kws_root",
            "ivw_demo",
            "keyword_*.bin",
            "res_shuffnet_v2",
        ):
            self.assertNotIn(needle, source)


class WakeLoopNoiseGateTest(unittest.TestCase):
    def test_wake_min_level_dbfs_argument_exists(self):
        voice_bridge = importlib.import_module("local_voice_chat.voice_bridge")
        old_argv = sys.argv[:]
        try:
            sys.argv = ["voice_bridge.py", "--wake-min-level-dbfs", "-40"]
            args = voice_bridge.parse_args()
            self.assertEqual(args.wake_min_level_dbfs, -40.0)
        finally:
            sys.argv = old_argv

    def test_default_wake_words_are_in_hotword_list(self):
        model_mod = importlib.import_module("local_voice_chat.local_voice_chat")
        self.assertIn("小远同学", model_mod.DEFAULT_HOTWORDS)
        self.assertIn("xiaoyuan", model_mod.DEFAULT_HOTWORDS)

        bridge_mod = importlib.import_module("local_voice_chat.voice_bridge")
        old_argv = sys.argv[:]
        try:
            sys.argv = ["voice_bridge.py"]
            args = bridge_mod.parse_args()
        finally:
            sys.argv = old_argv
        self.assertGreaterEqual(args.hotwords_score, 3.0)

    def test_default_wake_threshold_is_permissive_for_quiet_mics(self):
        bridge_mod = importlib.import_module("local_voice_chat.voice_bridge")
        old_argv = sys.argv[:]
        try:
            sys.argv = ["voice_bridge.py"]
            args = bridge_mod.parse_args()
            self.assertLessEqual(args.wake_min_level_dbfs, -70.0)
            self.assertLessEqual(args.wake_low_level_dbfs, -50.0)
        finally:
            sys.argv = old_argv

    def test_download_media_library_name_is_correct(self):
        voice_bridge = importlib.import_module("local_voice_chat.voice_bridge")
        self.assertTrue(hasattr(voice_bridge, "DOWNLOAD_MEDIA_LIBRARY"))
        self.assertTrue(hasattr(voice_bridge, "_DOWNLOAD_MEDIA_LIBRARY"))
        self.assertIs(voice_bridge._DOWNLOAD_MEDIA_LIBRARY, voice_bridge.DOWNLOAD_MEDIA_LIBRARY)

    def test_wakeword_only_text_detection(self):
        voice_bridge = importlib.import_module("local_voice_chat.voice_bridge")
        self.assertTrue(voice_bridge._is_wakeword_only_text("小远同学。", ["小远同学", "xiaoyuan"]))
        self.assertTrue(voice_bridge._is_wakeword_only_text("xiaoyuan", ["小远同学", "xiaoyuan"]))
        self.assertFalse(voice_bridge._is_wakeword_only_text("小远同学，播放测试视频", ["小远同学", "xiaoyuan"]))

    def test_play_aliases_compatibility_name_exists(self):
        voice_bridge = importlib.import_module("local_voice_chat.voice_bridge")
        self.assertTrue(hasattr(voice_bridge, "PLAY_ALIASES"))
        self.assertTrue(hasattr(voice_bridge, "_PLAY_ALIASES"))
        self.assertIs(voice_bridge._PLAY_ALIASES, voice_bridge.PLAY_ALIASES)

    def test_play_aliases_cover_test_video_keyword(self):
        voice_bridge = importlib.import_module("local_voice_chat.voice_bridge")
        self.assertEqual(voice_bridge.PLAY_ALIASES.get("测试视频"), "Oceans")
        self.assertEqual(voice_bridge.PLAY_ALIASES.get("测试"), "Oceans")

    def test_resolve_play_alias_uses_full_user_text_when_term_is_short(self):
        voice_bridge = importlib.import_module("local_voice_chat.voice_bridge")
        self.assertEqual(voice_bridge._resolve_play_alias("测试", "帮我播放特视视频"), "Oceans")
        self.assertEqual(voice_bridge._resolve_play_alias("测试", "放测试视频"), "Oceans")

    def test_download_failure_message_is_explicit(self):
        voice_bridge = importlib.import_module("local_voice_chat.voice_bridge")

        class DummyArgs:
            jellyfin_api_key = None

        msg = voice_bridge._download_failed_play_test_video(
            DummyArgs(),
            "外部视频源无法解析",
        )
        self.assertIn("我已经尝试下载", msg)
        self.assertIn("没有写入 Movies 文件夹", msg)
        self.assertIn("外部视频源无法解析", msg)

    def test_english_download_triggers_jellyfin_workflow(self):
        voice_bridge = importlib.import_module("local_voice_chat.voice_bridge")
        source = inspect.getsource(voice_bridge._jellyfin_play_after_download)
        self.assertIn('"download"', source)
        self.assertIn('"download complete"', source)
        self.assertIn("file name", source)


class FilterEdgeCaseRegressionTest(unittest.TestCase):
    def test_filter_request_maps_my_album_to_family_album(self):
        voice_bridge = importlib.import_module("local_voice_chat.voice_bridge")
        req = voice_bridge._extract_image_filter_request("Add a vintage filter to my album, preview first")
        self.assertIsNotNone(req)
        self.assertEqual(req["target"], "家庭相册")
        self.assertEqual(req["style"], "vintage")
        self.assertTrue(req["dry"])

    def test_filter_request_with_missing_style_asks_for_style(self):
        voice_bridge = importlib.import_module("local_voice_chat.voice_bridge")
        reply = voice_bridge._fast_local_image_filter_reply(None, "Please process my family album photos")
        self.assertIsNotNone(reply)
        self.assertIn("style", reply.lower())

    def test_filter_request_detects_capitalized_english_style(self):
        voice_bridge = importlib.import_module("local_voice_chat.voice_bridge")
        req = voice_bridge._extract_image_filter_request("Please add a Japanese style filter to my family album photos and preview it first")
        self.assertIsNotNone(req)
        self.assertEqual(req["target"], "家庭相册")
        self.assertEqual(req["style"], "japanese")

    def test_filter_request_accepts_chinese_style_aliases(self):
        voice_bridge = importlib.import_module("local_voice_chat.voice_bridge")
        req = voice_bridge._extract_image_filter_request("把家庭相册的照片加胶片风镜，预览一下")
        self.assertIsNotNone(req)
        self.assertEqual(req["target"], "家庭相册")
        self.assertEqual(req["style"], "film")


class AudioGainNormalizeTest(unittest.TestCase):
    """低电平麦克风必须被补偿到 ASR 可用响度。"""

    def _dbfs(self, samples):
        import numpy as np

        rms = float(np.sqrt(np.mean(np.square(samples.astype("float32")))))
        return 20.0 * __import__("math").log10(max(rms, 1e-9))

    def test_quiet_clip_is_boosted_toward_target(self):
        import numpy as np

        model_mod = importlib.import_module("local_voice_chat.local_voice_chat")
        t = np.arange(16000, dtype="float32") / 16000.0
        # 约 -70 dBFS，模拟实测的极低电平录音
        quiet = (0.0003 * np.sin(2 * np.pi * 220 * t)).astype("float32")
        boosted, gain_db = model_mod.normalize_audio_gain(quiet)

        self.assertGreater(gain_db, 20.0)
        self.assertGreater(self._dbfs(boosted), self._dbfs(quiet) + 20.0)

    def test_boost_never_clips(self):
        import numpy as np

        model_mod = importlib.import_module("local_voice_chat.local_voice_chat")
        t = np.arange(16000, dtype="float32") / 16000.0
        quiet = (0.0003 * np.sin(2 * np.pi * 220 * t)).astype("float32")
        boosted, _ = model_mod.normalize_audio_gain(quiet)
        self.assertLessEqual(float(np.max(np.abs(boosted))), 1.0)

    def test_loud_clip_is_not_attenuated(self):
        import numpy as np

        model_mod = importlib.import_module("local_voice_chat.local_voice_chat")
        t = np.arange(16000, dtype="float32") / 16000.0
        loud = (0.5 * np.sin(2 * np.pi * 220 * t)).astype("float32")
        boosted, gain_db = model_mod.normalize_audio_gain(loud)
        self.assertEqual(gain_db, 0.0)
        self.assertTrue(np.array_equal(boosted, loud))

    def test_empty_clip_is_safe(self):
        import numpy as np

        model_mod = importlib.import_module("local_voice_chat.local_voice_chat")
        boosted, gain_db = model_mod.normalize_audio_gain(np.array([], dtype="float32"))
        self.assertEqual(gain_db, 0.0)
        self.assertEqual(boosted.size, 0)

    def test_runtime_env_gain_overrides_are_respected(self):
        import os
        import numpy as np

        model_mod = importlib.import_module("local_voice_chat.local_voice_chat")
        old = {
            "VOICE_ASR_GAIN_TARGET_DBFS": os.environ.get("VOICE_ASR_GAIN_TARGET_DBFS"),
            "VOICE_ASR_TARGET_RMS_DBFS": os.environ.get("VOICE_ASR_TARGET_RMS_DBFS"),
            "VOICE_ASR_MAX_GAIN_DB": os.environ.get("VOICE_ASR_MAX_GAIN_DB"),
            "VOICE_ASR_PEAK_CEILING_DBFS": os.environ.get("VOICE_ASR_PEAK_CEILING_DBFS"),
        }
        try:
            os.environ["VOICE_ASR_GAIN_TARGET_DBFS"] = "-10.0"
            os.environ["VOICE_ASR_TARGET_RMS_DBFS"] = "-10.0"
            os.environ["VOICE_ASR_MAX_GAIN_DB"] = "10.0"
            os.environ["VOICE_ASR_PEAK_CEILING_DBFS"] = "-3.0"

            t = np.arange(16000, dtype="float32") / 16000.0
            quiet = (0.0003 * np.sin(2 * np.pi * 220 * t)).astype("float32")
            _, gain_db = model_mod.normalize_audio_gain(quiet)
            self.assertLess(gain_db, 15.0)
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class MicResolverSelectionTest(unittest.TestCase):
    def test_build_mic_options_priority_order(self):
        model_mod = importlib.import_module("local_voice_chat.local_voice_chat")
        opts = model_mod.build_mic_options(
            "usb,onboard",
            "alsa",
            "plughw:Audio,0",
            "pulse",
            "regular0",
        )
        self.assertEqual(len(opts), 2)
        self.assertEqual(opts[0].backend, "alsa")
        self.assertEqual(opts[0].device, "plughw:Audio,0")
        self.assertEqual(opts[1].backend, "pulse")
        self.assertEqual(opts[1].device, "regular0")

    def test_legacy_auto_keeps_usb_then_onboard(self):
        model_mod = importlib.import_module("local_voice_chat.local_voice_chat")
        opts = model_mod.build_legacy_mic_options("plughw:Audio,0", "auto")
        self.assertEqual([o.name for o in opts], ["usb", "onboard"])
        self.assertEqual(opts[1].backend, "pulse")

    def test_resolver_fallback_to_onboard_when_usb_unavailable(self):
        import unittest.mock as mock

        model_mod = importlib.import_module("local_voice_chat.local_voice_chat")
        resolver = model_mod.MicResolver(
            model_mod.build_mic_options(
                "usb,onboard",
                "alsa",
                "plughw:Audio,0",
                "pulse",
                "regular0",
            ),
            strict=False,
            recheck_sec=0,
        )

        def fake_probe(opt):
            if opt.name == "usb":
                return "No such device"
            return None

        with mock.patch.object(resolver, "_probe", side_effect=fake_probe):
            resolver._select("test")

        self.assertIsNotNone(resolver.selected())
        self.assertEqual(resolver.selected().name, "onboard")

    def test_resolver_strict_disables_fallback(self):
        import unittest.mock as mock

        model_mod = importlib.import_module("local_voice_chat.local_voice_chat")
        resolver = model_mod.MicResolver(
            model_mod.build_mic_options(
                "usb,onboard",
                "alsa",
                "plughw:Audio,0",
                "pulse",
                "regular0",
            ),
            strict=True,
        )

        with mock.patch.object(resolver, "_probe", return_value="No such device"):
            with self.assertRaises(RuntimeError):
                resolver._select("test")




class ConversationGuardRegressionTest(unittest.TestCase):
    def test_immich_spoken_name_prefers_filename(self):
        voice_bridge = importlib.import_module("local_voice_chat.voice_bridge")
        item = {
            "originalFileName": "landscape_sea_01.jpg",
            "originalPath": "/nas_share/家庭相册/测试样例/landscape_sea_01.jpg",
        }
        spoken = voice_bridge._immich_item_to_spoken(item, 1)
        self.assertIn("landscape", spoken.lower())
        self.assertNotIn("第1张", spoken)

    def test_english_number_words_for_tts(self):
        voice_bridge = importlib.import_module("local_voice_chat.voice_bridge")
        self.assertEqual(voice_bridge._num_to_en_words(0), "zero")
        self.assertEqual(voice_bridge._num_to_en_words(10), "ten")
        self.assertEqual(voice_bridge._num_to_en_words(25), "twenty-five")

    def test_immich_semantic_strip_removes_english_prepositions(self):
        voice_bridge = importlib.import_module("local_voice_chat.voice_bridge")
        semantic = voice_bridge._strip_immich_semantic("Show me photos from the seaside")
        self.assertEqual(semantic, "seaside")

    def test_immich_semantic_strip_removes_chinese_particle_de(self):
        voice_bridge = importlib.import_module("local_voice_chat.voice_bridge")
        semantic = voice_bridge._strip_immich_semantic("找海边的照片")
        self.assertEqual(semantic, "海边")

    def test_immich_select_relevant_items_prefers_keyword_matches(self):
        voice_bridge = importlib.import_module("local_voice_chat.voice_bridge")
        items = [
            {"type": "IMAGE", "originalFileName": "landscape sea 01.jpg", "originalPath": "/nas_share/家庭相册/landscape sea 01.jpg"},
            {"type": "IMAGE", "originalFileName": "food manti 01.jpg", "originalPath": "/nas_share/家庭相册/food manti 01.jpg"},
            {"type": "IMAGE", "originalFileName": "person market 01.jpg", "originalPath": "/nas_share/家庭相册/person market 01.jpg"},
        ]
        selected = voice_bridge._immich_select_relevant_items(items, "seaside", limit=5)
        self.assertEqual(len(selected), 1)
        self.assertIn("sea", selected[0].get("originalFileName", "").lower())

    def test_short_style_only_text_is_incomplete(self):
        voice_bridge = importlib.import_module("local_voice_chat.voice_bridge")
        self.assertTrue(voice_bridge._looks_like_incomplete_command("日系"))
        self.assertTrue(voice_bridge._looks_like_incomplete_command("复古"))
        self.assertTrue(voice_bridge._is_irrelevant_speech("日系"))

    def test_pending_filter_does_not_reuse_stale_style(self):
        import unittest.mock as mock

        voice_bridge = importlib.import_module("local_voice_chat.voice_bridge")
        pending = {"target": "旅行", "style": "vintage", "dry": True, "attempts": 0}

        with mock.patch.object(
            voice_bridge,
            "_run_image_batch_reply",
            side_effect=AssertionError("stale pending filter should not execute"),
        ):
            reply, new_pending = voice_bridge._consume_pending_image_filter(
                "把家庭相册的照片加个滤镜",
                pending,
            )

        self.assertIn("风格", reply)
        self.assertEqual(new_pending["target"], "家庭相册")
        self.assertIsNone(new_pending["style"])

    def test_tts_full_reply_not_cut(self):
        voice_bridge = importlib.import_module("local_voice_chat.voice_bridge")
        reply = "已处理完成：家庭相册已按复古风格处理，成功23张，输出到各原目录/复古风格/。"
        spoken, tail = voice_bridge._adaptive_tts_reply(
            "帮我把家庭相册的照片加复古滤镜，先预览",
            reply,
            max_chars=80,
            brief_max_chars=36,
            brief_user_len=12,
        )
        self.assertEqual(spoken, reply)
        self.assertEqual(tail, "")

    def test_immich_skips_file_management_queries(self):
        import unittest.mock as mock

        voice_bridge = importlib.import_module("local_voice_chat.voice_bridge")
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("Immich should not run")):
            self.assertIsNone(voice_bridge._fast_local_immich_reply(None, "家庭相册里有哪些文件"))

    def test_directory_listing_query_uses_local_filesystem(self):
        voice_bridge = importlib.import_module("local_voice_chat.voice_bridge")
        reply = voice_bridge._fast_local_directory_listing_reply(None, "家庭相册里有哪些文件")
        self.assertIsNotNone(reply)
        self.assertIn("家庭相册", reply)
        self.assertTrue("文件" in reply or "有" in reply)

    def test_download_filename_is_sanitized(self):
        voice_bridge = importlib.import_module("local_voice_chat.voice_bridge")
        self.assertEqual(voice_bridge._sanitize_download_filename("oceans [oceans].mp4"), "oceans.mp4")


if __name__ == "__main__":
    unittest.main()
