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
        self.assertEqual(result.reason, "workday_html_inconclusive")
        self.assertEqual(link_health_state(result), "INCONCLUSIVE")

    @patch("harvest.url_health._workday_search_by_req_id")
    @patch("harvest.url_health.requests.get")
    def test_workday_cxs_404_with_search_no_match_is_dead(self, m_get, m_search):
        m_get.return_value = _Resp(
            status_code=404,
            url="https://thermofisher.wd5.myworkdayjobs.com/wday/cxs/thermofisher/thermofishercareers/job/foo_R-01344461",
            body=b'{"errorCode":"HTTP_404","httpStatus":404}',
        )
        m_search.return_value = LinkHealthResult(
            False, 200, "workday_search_no_match", "https://example.com/jobs",
        )
        result = check_job_posting_live(
            "https://thermofisher.wd5.myworkdayjobs.com/thermofishercareers/job/Chengdu-China/Sr-Systems-Engineer_R-01344461",
            platform_slug="workday",
        )
        self.assertFalse(result.is_live)
        self.assertEqual(result.reason, "workday_search_no_match")
        self.assertEqual(link_health_state(result), "DEAD")
        self.assertTrue(is_definitive_inactive(result))

    @patch("harvest.url_health.requests.get")
    @patch("harvest.url_health.requests.head")
    @patch("harvest.url_health._workday_cxs_liveness", return_value=None)
    def test_workday_shell_posting_unavailable_is_dead(self, _m_cxs, m_head, m_get):
        m_head.return_value = _Resp(
            status_code=200,
            url="https://thermofisher.wd5.myworkdayjobs.com/thermofishercareers/job/foo_R-01344461",
        )
        m_get.return_value = _Resp(
            status_code=200,
            url="https://thermofisher.wd5.myworkdayjobs.com/thermofishercareers/job/foo_R-01344461",
            body=(
                b"<html><script>window.workday = {postingAvailable: false};</script>"
                b"<meta property='og:description' content='"
                b"Thank you for your interest as you consider starting a new career journey with us. "
                b"As the world leader in serving science, our colleagues develop critical solutions. "
                b"'></meta></html>"
            ),
        )
        result = check_job_posting_live(
            "https://thermofisher.wd5.myworkdayjobs.com/thermofishercareers/job/foo_R-01344461",
            platform_slug="workday",
        )
        self.assertFalse(result.is_live)
        self.assertEqual(result.reason, "workday_posting_unavailable")
        self.assertEqual(link_health_state(result), "DEAD")

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
        self.assertTrue(is_definitive_inactive(LinkHealthResult(False, 200, "greenhouse_posting_unavailable", "")))
        self.assertFalse(is_definitive_inactive(LinkHealthResult(False, 0, "request_error_unknown", "")))
        self.assertFalse(is_definitive_inactive(LinkHealthResult(True, 200, "ok", "")))

    @patch("harvest.url_health.requests.get")
    @patch("harvest.url_health.requests.head")
    @patch("harvest.url_health._greenhouse_liveness", return_value=None)
    def test_known_platform_weak_html_is_inconclusive_not_live(self, _m_api, m_head, m_get):
        m_head.return_value = _Resp(
            status_code=200,
            url="https://boards.greenhouse.io/acme/jobs/123",
        )
        m_get.return_value = _Resp(
            status_code=200,
            url="https://boards.greenhouse.io/acme/jobs/123",
            body=(
                b"<html><body><h1>Job Description</h1>"
                b"<p>Responsibilities and qualifications. Apply now.</p>"
                b"</body></html>"
            ),
        )
        result = check_job_posting_live(
            "https://boards.greenhouse.io/acme/jobs/123",
            platform_slug="greenhouse",
        )
        self.assertTrue(result.is_live)
        self.assertEqual(result.reason, "greenhouse_html_inconclusive")
        self.assertEqual(link_health_state(result), "INCONCLUSIVE")

    @patch("harvest.url_health.requests.get")
    @patch("harvest.url_health.requests.head")
    @patch("harvest.url_health._icims_liveness", return_value=None)
    def test_icims_weak_html_is_inconclusive(self, _m_api, m_head, m_get):
        m_head.return_value = _Resp(
            status_code=200,
            url="https://acme.icims.com/jobs/12345/job",
        )
        m_get.return_value = _Resp(
            status_code=200,
            url="https://acme.icims.com/jobs/12345/job",
            body=b"<html><body>Job description apply for this job</body></html>",
        )
        result = check_job_posting_live(
            "https://acme.icims.com/jobs/12345/job",
            platform_slug="icims",
        )
        self.assertEqual(result.reason, "icims_html_inconclusive")
        self.assertEqual(link_health_state(result), "INCONCLUSIVE")

    @patch("harvest.url_health.requests.get")
    @patch("harvest.url_health.requests.head")
    @patch("harvest.url_health._greenhouse_liveness", return_value=None)
    def test_greenhouse_shell_not_found_is_dead(self, _m_api, m_head, m_get):
        m_head.return_value = _Resp(
            status_code=200,
            url="https://boards.greenhouse.io/acme/jobs/123",
        )
        m_get.return_value = _Resp(
            status_code=200,
            url="https://boards.greenhouse.io/acme/jobs/123",
            body=b"<html><body>The job you were looking for was not found.</body></html>",
        )
        result = check_job_posting_live(
            "https://boards.greenhouse.io/acme/jobs/123",
            platform_slug="greenhouse",
        )
        self.assertFalse(result.is_live)
        self.assertIn(result.reason, {"soft_404_marker", "greenhouse_posting_unavailable"})
        self.assertEqual(link_health_state(result), "DEAD")

    @patch("harvest.url_health.requests.get")
    @patch("harvest.url_health.requests.head")
    def test_unknown_host_weak_html_can_still_be_live(self, m_head, m_get):
        m_head.return_value = _Resp(status_code=200, url="https://careers.example.com/job/123")
        m_get.return_value = _Resp(
            status_code=200,
            url="https://careers.example.com/job/123",
            body=b"<html><body><h1>Job Description</h1><p>Apply now for responsibilities.</p></body></html>",
        )
        result = check_job_posting_live("https://careers.example.com/job/123", platform_slug="")
        self.assertTrue(result.is_live)
        self.assertEqual(result.reason, "detail_live_markers")
        self.assertEqual(link_health_state(result), "LIVE")
