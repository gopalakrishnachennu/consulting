from django import forms
from django.core.exceptions import ValidationError

from .models import Message

MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024  # 5 MB


class MessageForm(forms.ModelForm):
    class Meta:
        model = Message
        fields = ["content", "attachment"]
        labels = {"content": "", "attachment": ""}
        widgets = {
            "content": forms.Textarea(
                attrs={
                    "rows": 2,
                    "placeholder": "Write a message…",
                    "id": "id_message_content",
                    "class": (
                        "block w-full min-h-[44px] max-h-40 resize-none border-0 bg-transparent px-0 py-1 "
                        "text-[15px] leading-normal text-gray-900 placeholder:text-gray-400 "
                        "focus:outline-none focus:ring-0"
                    ),
                }
            ),
            "attachment": forms.FileInput(
                attrs={
                    "class": (
                        "block w-full cursor-pointer text-xs font-semibold text-blue-600 file:mr-2 "
                        "file:cursor-pointer file:rounded-full file:border-0 file:bg-gray-200 file:px-3 "
                        "file:py-1.5 file:text-xs file:font-bold file:text-gray-800 hover:file:bg-gray-300"
                    ),
                }
            ),
        }

    def clean(self):
        cleaned = super().clean()
        content = (cleaned.get("content") or "").strip()
        f = cleaned.get("attachment")
        if not content and not f:
            raise ValidationError("Enter a message or attach a file.")
        return cleaned

    def clean_attachment(self):
        f = self.cleaned_data.get("attachment")
        if f and f.size > MAX_ATTACHMENT_BYTES:
            raise ValidationError("Attachment must be 5 MB or smaller.")
        return f


class MessageEditForm(forms.Form):
    content = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "class": (
                    "w-full rounded-xl border border-gray-200 bg-white px-4 py-3 text-sm "
                    "text-gray-900 focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 focus:outline-none"
                ),
            }
        ),
        max_length=10000,
    )
