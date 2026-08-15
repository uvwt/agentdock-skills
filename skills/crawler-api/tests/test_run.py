import importlib.util
import os
import pathlib
import unittest
from unittest import mock


MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "run.py"
SPEC = importlib.util.spec_from_file_location("crawler_api_run", MODULE_PATH)
RUN = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(RUN)


class CrawlerApiSkillTest(unittest.TestCase):
    def test_base_url_requires_http_service(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RUN.SkillError) as ctx:
                RUN.base_url()
        self.assertEqual(ctx.exception.code, "missing_config")

        with mock.patch.dict(os.environ, {"CRAWLER_BASE_URL": "ftp://example.com"}, clear=True):
            with self.assertRaises(RUN.SkillError) as ctx:
                RUN.base_url()
        self.assertEqual(ctx.exception.code, "invalid_config")

    def test_normalize_awemes_filters_images_and_deduplicates(self):
        payload = {
            "data": [
                {
                    "aweme_info": {
                        "aweme_id": "1001",
                        "desc": "图文作品",
                        "aweme_type": 68,
                        "images": [{"url_list": ["https://example.com/a.jpg"]}],
                        "author": {"nickname": "作者A", "uid": "u1"},
                        "share_info": {"share_url": "https://example.com/note/1001"},
                    }
                },
                {
                    "aweme_info": {
                        "aweme_id": "1002",
                        "desc": "视频作品",
                        "video": {"play_addr": {}},
                        "author": {"nickname": "作者B", "uid": "u2"},
                    }
                },
                {
                    "aweme_id": "1001",
                    "desc": "重复图文",
                    "images": [{"url_list": ["https://example.com/b.jpg"]}],
                },
            ]
        }

        images = RUN.normalize_awemes(payload, "image")
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["aweme_id"], "1001")
        self.assertEqual(images[0]["kind"], "image")
        self.assertEqual(images[0]["image_count"], 1)

        videos = RUN.normalize_awemes(payload, "video")
        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0]["aweme_id"], "1002")

    def test_crawler_error_keeps_business_code(self):
        with self.assertRaises(RUN.SkillError) as ctx:
            RUN.check_crawler_response({"code": 3, "msg": "请先添加账号", "data": None})
        self.assertEqual(ctx.exception.code, "crawler_error")
        self.assertEqual(ctx.exception.details["crawler_code"], 3)
        self.assertEqual(ctx.exception.message, "请先添加账号")

    def test_douyin_search_normalizes_success_response(self):
        response = {
            "code": 0,
            "msg": "成功",
            "data": [
                {
                    "aweme_info": {
                        "aweme_id": "2001",
                        "desc": "露营图文",
                        "images": [{"url_list": ["https://example.com/1.jpg"]}],
                        "author": {"nickname": "露营作者", "uid": "author-1"},
                    }
                }
            ],
        }

        with mock.patch.object(RUN, "request_json", return_value=(200, response)) as request_json:
            result = RUN.action_douyin_search(
                {"keyword": "露营", "offset": 0, "limit": 10, "kind": "image"}
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["aweme_id"], "2001")
        request_json.assert_called_once_with(
            "/douyin/search",
            {"keyword": "露营", "offset": 0, "limit": 10},
        )

    def test_douyin_search_rejects_unknown_kind(self):
        with self.assertRaises(RUN.SkillError) as ctx:
            RUN.action_douyin_search({"keyword": "露营", "kind": "photo"})
        self.assertEqual(ctx.exception.code, "invalid_input")


if __name__ == "__main__":
    unittest.main()
