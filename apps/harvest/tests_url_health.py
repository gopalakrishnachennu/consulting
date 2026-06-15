from io import BytesIO
from unittest.mock import patch

from django.test import SimpleTestCase

from harvest.url_health import (
    LinkHealthResult,
    build_link_health_payload,
    check_job_posting_live,
    is_definitive_inactive,
    link_health_state,
)


class _Raw:
    def __init__(self, body: bytes):
        self._buf = BytesIO(body)

    def read(self, *args, **kwargs):
        return self._buf.read(*args)


class _Resp:
    def __init__(self, status_code=200, url="", body=b""):
        self.status_code = status_code
        self.url = url
        self.raw = _Raw(body)

    def close(self):
        return None


class UrlHealthTests(SimpleTestCase):
    @patch("harvest.url_health.requests.get")
    @patch("harvest.url_health.requests.head")
    @patch("harvest.url_health._workday_cxs_liveness", return_value=None)
    def test_workday_soft_404_detected(self, _m_cxs, m_head, m_get):
        m_head.return_value = _Resp(
            status_code=200,
            url="https://wgu.wd5.myworkdayjobs.com/External/job/foo",
        )
        m_get.return_value = _Resp(
            status_code=200,
            url="https://wgu.wd5.myworkdayjobs.com/External/job/foo",
            body=(
                b"<html><body>The page you are looking for doesn't exist. "
                b"Search for Jobs</body></html>"
            ),
        )
        result = check_job_posting_live(
            "https://wgu.wd5.myworkdayjobs.com/External/job/foo",
            platform_slug="workday",
        )
        self.assertFalse(result.is_live)
        self.assertEqual(result.reason, "soft_404_marker")
        self.assertTrue(is_definitive_inactive(result))

    @patch("harvest.url_health.requests.get")
    @patch("harvest.url_health.requests.head")
    @patch("harvest.url_health._workday_cxs_liveness", return_value=None)
    def test_workday_live_page_not_false_killed_by_search_label(self, _m_cxs, m_head, m_get):
        m_head.return_value = _Resp(
            status_code=200,
            url="https://wgu.wd5.myworkdayjobs.com/External/job/foo",
        )
        m_get.return_value = _Resp(
            status_code=200,
            url="https://wgu.wd5.myworkdayjobs.com/External/job/foo",
            body=(
                b"<html><body><h1>Job Description</h1>"
                b"<p>Responsibilities and qualifications. Apply now.</p>"
                b"<a>Search for Jobs</a></body></html>"
            ),
        )
        result = check_job_posting_live(
            "https://wgu.wd5.myworkdayjobs.com/External/job/foo",
            platform_slug="workday",
        )
        self.assertTrue(result.is_live)
        self.assertIn(result.reason, {"detail_live_markers", "ok", "detail_long_content"})

    @patch("harvest.url_health.requests.get")
    @patch("harvest.url_health.requests.head")
    def test_transient_503_not_marked_inactive(self, m_head, m_get):
        m_head.return_value = _Resp(status_code=200, url="https://example.com/job/123")
        m_get.return_value = _Resp(status_code=503, url="https://example.com/job/123")
        result = check_job_posting_live("https://example.com/job/123", platform_slug="")
        self.assertTrue(result.is_live)
        self.assertEqual(result.reason, "transient_http_503")
        self.assertFalse(is_definitive_inactive(result))
        self.assertEqual(link_health_state(result), "INCONCLUSIVE")

    @patch("harvest.url_health.requests.get")
    def test_workable_api_404_is_definitive_dead(self, m_get):
        m_get.return_value = _Resp(status_code=404, url="https://apply.workable.com/api/v1/accounts/acme/jobs/abc")
        result = check_job_posting_live(
            "https://apply.workable.com/acme/j/abc",
            platform_slug="workable",
        )
        self.assertFalse(result.is_live)
        self.assertEqual(result.reason, "workable_api_not_found")
        self.assertTrue(is_definitive_inactive(result))
        self.assertEqual(link_health_state(result), "DEAD")

    def test_build_payload_contains_state(self):
        result = LinkHealthResult(True, 200, "detail_live_markers", "https://example.com/job/123")
        payload = build_link_health_payload(result, checked_at_iso="2026-06-15T18:00:00+00:00")
        self.assertEqual(payload["state"], "LIVE")
        self.assertEqual(payload["reason"], "detail_live_markers")
        self.assertEqual(payload["status_code"], 200)

    def test_definitive_policy(self):
        self.assertTrue(is_definitive_inactive(LinkHealthResult(False, 404, "http_404", "")))
        self.assertTrue(is_definitive_inactive(LinkHealthResult(False, 200, "soft_404_marker", "")))
        self.assertFalse(is_definitive_inactive(LinkHealthResult(False, 0, "request_error_unknown", "")))
        self.assertFalse(is_definitive_inactive(LinkHealthResult(True, 200, "ok", "")))
