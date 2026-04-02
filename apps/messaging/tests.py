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
