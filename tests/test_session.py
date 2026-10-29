import unittest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from src.utils.session import SessionManager
from src.utils import comix_session


class SessionManagerTests(unittest.TestCase):
    def setUp(self):
        comix_session._credential_store.clear_for_tests()

    def tearDown(self):
        comix_session._credential_store.clear_for_tests()

    def test_default_session_headers_do_not_include_referer(self):
        manager = SessionManager()
        session = manager.session
        self.assertNotIn("Referer", manager._headers())
        self.assertNotIn("Referer", session.headers)
        self.assertIn("User-Agent", session.headers)

    def test_comix_domain_receives_referer_and_cookies(self):
        comix_session.publish_comix_credentials(
            "Custom-UA",
            [
                SimpleNamespace(name="waf_pass", value="123", domain="comix.to"),
                SimpleNamespace(name="other", value="abc", domain="other.com"),
            ],
        )

        manager = SessionManager()
        with patch.object(manager.session, "get") as mock_get:
            manager.get("https://comix.to/api/v1/test")
            self.assertEqual(mock_get.call_count, 1)
            _, kwargs = mock_get.call_args
            headers = kwargs.get("headers", {})
            self.assertEqual(headers.get("Referer"), "https://comix.to/")
            self.assertEqual(headers.get("Cookie"), "waf_pass=123")

    def test_comix_subdomain_receives_referer(self):
        manager = SessionManager()
        with patch.object(manager.session, "get") as mock_get:
            manager.get("https://static.comix.to/img/cover.jpg")
            self.assertEqual(mock_get.call_count, 1)
            _, kwargs = mock_get.call_args
            headers = kwargs.get("headers", {})
            self.assertEqual(headers.get("Referer"), "https://comix.to/")

    def test_external_cdn_does_not_receive_referer_or_cookies(self):
        comix_session.publish_comix_credentials(
            "Custom-UA",
            [
                SimpleNamespace(name="waf_pass", value="123", domain="comix.to"),
            ],
        )

        manager = SessionManager()
        cdn_urls = [
            "https://8pl.ryancorner.site/hi/01.webp",
            "https://8pl.gentlelivingnotes.site/hi/02.webp",
            "https://rxn.somesite.site/img/03.jpg",
        ]

        for url in cdn_urls:
            with patch.object(manager.session, "get") as mock_get:
                manager.get(url)
                self.assertEqual(mock_get.call_count, 1)
                _, kwargs = mock_get.call_args
                headers = kwargs.get("headers") or {}
                self.assertNotIn("Referer", headers)
                self.assertNotIn("Cookie", headers)

    def test_caller_headers_are_preserved(self):
        manager = SessionManager()
        with patch.object(manager.session, "get") as mock_get:
            manager.get("https://8pl.ryancorner.site/hi/01.webp", headers={"Connection": "close"})
            self.assertEqual(mock_get.call_count, 1)
            _, kwargs = mock_get.call_args
            headers = kwargs.get("headers", {})
            self.assertEqual(headers.get("Connection"), "close")
            self.assertNotIn("Referer", headers)


if __name__ == "__main__":
    unittest.main()
