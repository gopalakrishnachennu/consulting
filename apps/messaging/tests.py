from django.core.cache import cache
from django.test import TestCase, Client
from django.urls import reverse
from users.models import User
from .models import Thread, Message


class ThreadModelTests(TestCase):
    def setUp(self):
        self.u1 = User.objects.create_user(username="u1", password="pass")
        self.u2 = User.objects.create_user(username="u2", password="pass")

    def test_create_thread(self):
        t = Thread.objects.create()
        t.participants.add(self.u1, self.u2)
        self.assertEqual(t.participants.count(), 2)

    def test_thread_str(self):
        t = Thread.objects.create()
        t.participants.add(self.u1, self.u2)
        s = str(t)
        self.assertIn("u1", s)
        self.assertIn("u2", s)


class MessageModelTests(TestCase):
    def setUp(self):
        self.u1 = User.objects.create_user(username="u1", password="pass")
        self.u2 = User.objects.create_user(username="u2", password="pass")
        self.thread = Thread.objects.create()
        self.thread.participants.add(self.u1, self.u2)

    def test_create_message(self):
        m = Message.objects.create(thread=self.thread, sender=self.u1, content="Hello")
        self.assertFalse(m.is_read)
        self.assertIn("u1", str(m))

    def test_message_ordering(self):
        m1 = Message.objects.create(thread=self.thread, sender=self.u1, content="First")
        m2 = Message.objects.create(thread=self.thread, sender=self.u2, content="Second")
        msgs = list(self.thread.messages.all())
        self.assertEqual(msgs[0], m1)
        self.assertEqual(msgs[1], m2)


class MessagingViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.u1 = User.objects.create_user(
            username="u1", password="testpass", role=User.Role.EMPLOYEE
        )
        self.u2 = User.objects.create_user(
            username="u2", password="testpass", role=User.Role.CONSULTANT
        )

    def test_inbox_requires_login(self):
        resp = self.client.get(reverse("inbox"))
        self.assertEqual(resp.status_code, 302)

    def test_inbox_authenticated(self):
        self.client.login(username="u1", password="testpass")
        resp = self.client.get(reverse("inbox"))
        self.assertEqual(resp.status_code, 200)

    def test_inbox_with_thread_shows_pane(self):
        self.client.login(username="u1", password="testpass")
        thread = Thread.objects.create()
        thread.participants.add(self.u1, self.u2)
        url = f"{reverse('inbox')}?thread={thread.pk}"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="thread-conversation"')

    def test_thread_post_htmx_returns_messages_partial(self):
        self.client.login(username="u1", password="testpass")
        thread = Thread.objects.create()
        thread.participants.add(self.u1, self.u2)
        url = reverse("thread-detail", kwargs={"pk": thread.pk})
        resp = self.client.post(
            url,
            {"content": "Hello via HTMX"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Hello via HTMX")
        self.assertContains(resp, 'id="msg-scroll"')
        self.assertTrue(resp.content.strip().startswith(b"<div"))

    def test_messaging_search_non_htmx_redirects(self):
        self.client.login(username="u1", password="testpass")
        resp = self.client.get(reverse("messaging-search") + "?q=ab")
        self.assertEqual(resp.status_code, 302)

    def test_messaging_search_staff_finds_consultant(self):
        self.client.login(username="u1", password="testpass")
        resp = self.client.get(
            reverse("messaging-search") + "?q=u2",
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "u2")

    def test_messaging_search_consultant_does_not_see_other_consultant(self):
        User.objects.create_user(username="u3", password="testpass", role=User.Role.CONSULTANT)
        self.client.login(username="u2", password="testpass")
        resp = self.client.get(
            reverse("messaging-search") + "?q=u3",
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "No matches")

    def test_messaging_search_consultant_finds_employee(self):
        self.client.login(username="u2", password="testpass")
        resp = self.client.get(
            reverse("messaging-search") + "?q=u1",
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "u1")

    def test_start_thread_blocked_consultant_to_consultant(self):
        u3 = User.objects.create_user(username="u3", password="testpass", role=User.Role.CONSULTANT)
        self.client.login(username="u2", password="testpass")
        resp = self.client.post(reverse("start-thread", kwargs={"user_id": u3.pk}))
        self.assertEqual(resp.status_code, 302)

    def test_start_thread_get_redirects_to_inbox(self):
        self.client.login(username="u1", password="testpass")
        resp = self.client.get(reverse("start-thread", kwargs={"user_id": self.u2.pk}))
        self.assertEqual(resp.status_code, 302)

    def test_typing_ping_then_other_sees_status(self):
        cache.clear()
        thread = Thread.objects.create()
        thread.participants.add(self.u1, self.u2)
        self.client.login(username="u1", password="testpass")
        r = self.client.post(reverse("thread-typing-ping", kwargs={"pk": thread.pk}))
        self.assertEqual(r.status_code, 204)
        self.client.login(username="u2", password="testpass")
        r2 = self.client.get(
            reverse("thread-typing-status", kwargs={"pk": thread.pk}),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(r2.status_code, 200)
        self.assertContains(r2, "typing")

    def test_seen_label_after_recipient_opens_thread(self):
        thread = Thread.objects.create()
        thread.participants.add(self.u1, self.u2)
        Message.objects.create(thread=thread, sender=self.u1, content="Hi there", is_read=False)
        self.client.login(username="u2", password="testpass")
        self.client.get(reverse("thread-detail", kwargs={"pk": thread.pk}))
        self.client.login(username="u1", password="testpass")
        resp = self.client.get(reverse("thread-detail", kwargs={"pk": thread.pk}))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Seen")
