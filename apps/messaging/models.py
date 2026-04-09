from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

MESSAGE_EDIT_WINDOW = timedelta(minutes=15)


class Thread(models.Model):
    class ThreadType(models.TextChoices):
        DIRECT = "direct", _("Direct")
        ORG_SHARED = "org_shared", _("Organisation team thread")

    participants = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name="threads")
    organisation = models.ForeignKey(
        "core.Organisation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="message_threads",
    )
    thread_type = models.CharField(
        max_length=20,
        choices=ThreadType.choices,
        default=ThreadType.DIRECT,
        db_index=True,
    )
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Thread {self.id} - {', '.join([user.username for user in self.participants.all()])}"

    class Meta:
        ordering = ["-updated_at"]

    @property
    def is_org_shared_thread(self) -> bool:
        return self.thread_type == self.ThreadType.ORG_SHARED

    def other_participant(self, user):
        """The counterparty in a 1:1 thread (first user that is not `user`)."""
        return self.participants.exclude(pk=user.pk).first()


class Message(models.Model):
    thread = models.ForeignKey(Thread, on_delete=models.CASCADE, related_name="messages")
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sent_messages")
    content = models.TextField()
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    edited_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    attachment = models.FileField(upload_to="message_attachments/%Y/%m/", blank=True, null=True)

    def can_edit(self, user) -> bool:
        if self.deleted_at or self.sender_id != user.pk:
            return False
        return timezone.now() - self.created_at <= MESSAGE_EDIT_WINDOW

    def can_be_removed_by(self, user) -> bool:
        if self.deleted_at:
            return False
        if self.sender_id == user.pk:
            return True
        thread = self.thread
        if thread.thread_type == Thread.ThreadType.ORG_SHARED and thread.organisation_id:
            if user.is_superuser:
                return True
            if user.role in ("ADMIN", "EMPLOYEE") and user.organisation_id == thread.organisation_id:
                return True
        return False

    def __str__(self):
        return f"Message from {self.sender.username} in Thread {self.thread.id}"

    class Meta:
        ordering = ["created_at"]
