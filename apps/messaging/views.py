from urllib.parse import quote

from django.contrib import messages as django_messages
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.generic import ListView, View
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.decorators.http import require_POST
from django.utils.decorators import method_decorator

from .models import Thread, Message
from .forms import MessageForm, MessageEditForm
from .notify import notify_recipients_new_message
from .utils import (
    ensure_thread_participant,
    filter_inbox_queryset,
    inbox_threads_base_queryset,
    invalidate_messaging_unread_cache,
    messaging_search_rate_ok,
    user_can_access_thread,
    users_may_message_each_other,
)
from users.models import User

def _role_short_label(user: User) -> str:
    if user.is_superuser:
        return "Admin"
    mapping = {
        User.Role.ADMIN: "Admin",
        User.Role.EMPLOYEE: "Employee",
        User.Role.CONSULTANT: "Consultant",
    }
    return mapping.get(user.role, user.role or "User")


def _htmx(request) -> bool:
    return request.headers.get("HX-Request") == "true"


def resolve_thread_organisation(u1: User, u2: User):
    if u1.organisation_id:
        return u1.organisation_id
    if u2.organisation_id:
        return u2.organisation_id
    return None


TYPING_CACHE_TTL = 8  # seconds
TYPING_CACHE_PREFIX = "msgtyping"


def _typing_cache_key(thread_id: int, user_id: int) -> str:
    return f"{TYPING_CACHE_PREFIX}:{thread_id}:{user_id}"


def build_thread_context(request, thread: Thread, form: MessageForm, *, is_pane: bool = False, history_q: str | None = None):
    mq = thread.messages.select_related("sender").order_by("created_at")
    if history_q and history_q.strip():
        mq = mq.filter(content__icontains=history_q.strip())
    other = thread.other_participant(request.user)
    pc = thread.participants.count()
    msg_list = list(mq)
    last_own_pk = None
    for m in reversed(msg_list):
        if m.sender_id == request.user.pk:
            last_own_pk = m.pk
            break
    return {
        "thread": thread,
        "messages": msg_list,
        "messages_history_q": (history_q or "").strip(),
        "form": form,
        "other_user": other,
        "other_role_label": _role_short_label(other) if other else "",
        "other_display_name": (other.get_full_name() or other.username) if other else "",
        "is_pane": is_pane,
        "thread_is_group": pc > 2,
        "thread_participant_count": pc,
        "history_filter_active": bool((history_q or "").strip()),
        "show_read_receipts": pc == 2,
        "last_own_message_id": last_own_pk,
    }


def _mark_thread_read(thread: Thread, user: User):
    Message.objects.filter(thread=thread).exclude(sender=user).update(is_read=True)
    invalidate_messaging_unread_cache(user.pk)


def messaging_search_staff_only(user: User) -> bool:
    return user.role == User.Role.CONSULTANT and not user.is_superuser


def recipient_search_queryset(viewer: User):
    qs = User.objects.filter(is_active=True).exclude(pk=viewer.pk)
    if messaging_search_staff_only(viewer):
        qs = qs.filter(Q(role__in=(User.Role.ADMIN, User.Role.EMPLOYEE)) | Q(is_superuser=True))
    else:
        qs = qs.filter(
            Q(role__in=(User.Role.ADMIN, User.Role.EMPLOYEE, User.Role.CONSULTANT)) | Q(is_superuser=True)
        )
    if viewer.organisation_id:
        qs = qs.filter(Q(organisation_id=viewer.organisation_id) | Q(organisation_id__isnull=True))
    return qs


class MessagingRecipientSearchView(LoginRequiredMixin, View):
    def get(self, request):
        if not _htmx(request):
            return redirect("inbox")
        if not messaging_search_rate_ok(
            request.user.pk,
            request.META.get("REMOTE_ADDR", "") or "unknown",
        ):
            return render(
                request,
                "messaging/_search_results.html",
                {
                    "q": "",
                    "results": [],
                    "too_short": False,
                    "staff_only": messaging_search_staff_only(request.user),
                    "rate_limited": True,
                },
            )
        q = (request.GET.get("q") or "").strip()
        if len(q) < 2:
            return render(
                request,
                "messaging/_search_results.html",
                {
                    "q": q,
                    "results": [],
                    "too_short": True,
                    "staff_only": messaging_search_staff_only(request.user),
                    "rate_limited": False,
                },
            )
        base = recipient_search_queryset(request.user)
        results = base.filter(
            Q(username__icontains=q)
            | Q(first_name__icontains=q)
            | Q(last_name__icontains=q)
            | Q(email__icontains=q)
        ).order_by("first_name", "last_name", "username")[:25]
        return render(
            request,
            "messaging/_search_results.html",
            {
                "q": q,
                "results": results,
                "too_short": False,
                "staff_only": messaging_search_staff_only(request.user),
                "rate_limited": False,
            },
        )


class InboxView(LoginRequiredMixin, ListView):
    model = Thread
    template_name = "messaging/inbox.html"
    context_object_name = "threads"

    def get_queryset(self):
        qs = inbox_threads_base_queryset(self.request.user)
        list_q = (self.request.GET.get("list_q") or "").strip()
        return filter_inbox_queryset(qs, list_q)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["selected_thread_id"] = None
        context["thread_pane"] = None
        context["list_q"] = (self.request.GET.get("list_q") or "").strip()
        tid = self.request.GET.get("thread")
        history_q = (self.request.GET.get("history_q") or "").strip()
        if tid and str(tid).isdigit():
            thread = (
                Thread.objects.filter(pk=int(tid))
                .prefetch_related("participants")
                .first()
            )
            if thread and user_can_access_thread(self.request.user, thread):
                if thread.participants.filter(pk=self.request.user.pk).exists():
                    _mark_thread_read(thread, self.request.user)
                thread = Thread.objects.prefetch_related("participants").get(pk=thread.pk)
                context["selected_thread_id"] = thread.pk
                context["thread_pane"] = build_thread_context(
                    self.request,
                    thread,
                    MessageForm(),
                    is_pane=True,
                    history_q=history_q,
                )
        context["messaging_search_staff_only"] = messaging_search_staff_only(self.request.user)
        context["can_start_org_thread"] = (
            self.request.user.role == User.Role.CONSULTANT and self.request.user.organisation_id
        )
        return context


class ThreadDetailView(LoginRequiredMixin, View):
    def get(self, request, pk):
        thread = get_object_or_404(
            Thread.objects.select_related("organisation").prefetch_related("participants"),
            pk=pk,
        )
        if not user_can_access_thread(request.user, thread):
            return redirect("inbox")

        if thread.participants.filter(pk=request.user.pk).exists():
            _mark_thread_read(thread, request.user)
        thread = (
            Thread.objects.select_related("organisation")
            .prefetch_related("participants")
            .get(pk=thread.pk)
        )

        history_q = (request.GET.get("history_q") or "").strip()
        fragment = (request.GET.get("fragment") or "").strip()

        if fragment == "messages":
            if not _htmx(request):
                return redirect("thread-detail", pk=pk)
            ctx = build_thread_context(
                request,
                thread,
                MessageForm(),
                is_pane=False,
                history_q=history_q,
            )
            return render(request, "messaging/_thread_messages_list.html", ctx)

        form = MessageForm()
        ctx = build_thread_context(request, thread, form, is_pane=_htmx(request), history_q=history_q)
        ctx["messaging_search_staff_only"] = messaging_search_staff_only(request.user)
        if _htmx(request):
            return render(request, "messaging/_thread_pane.html", ctx)
        return render(request, "messaging/thread_detail.html", ctx)

    def post(self, request, pk):
        thread = get_object_or_404(
            Thread.objects.select_related("organisation").prefetch_related("participants"),
            pk=pk,
        )
        if not user_can_access_thread(request.user, thread):
            return redirect("inbox")

        ensure_thread_participant(thread, request.user)

        history_q = (request.POST.get("history_q") or request.GET.get("history_q") or "").strip()
        form = MessageForm(request.POST, request.FILES)
        if form.is_valid():
            message = form.save(commit=False)
            message.thread = thread
            message.sender = request.user
            message.save()
            thread.save()
            notify_recipients_new_message(message)
            if _htmx(request):
                thread = Thread.objects.prefetch_related("participants").get(pk=thread.pk)
                ctx = build_thread_context(
                    request,
                    thread,
                    MessageForm(),
                    is_pane=False,
                    history_q=history_q,
                )
                return render(request, "messaging/_thread_messages_form.html", ctx)
            if history_q:
                return redirect(f"{reverse('thread-detail', kwargs={'pk': pk})}?history_q={quote(history_q)}")
            return redirect("thread-detail", pk=pk)

        ctx = build_thread_context(
            request,
            thread,
            form,
            is_pane=False,
            history_q=history_q,
        )
        ctx["messaging_search_staff_only"] = messaging_search_staff_only(request.user)
        if _htmx(request):
            return render(request, "messaging/_thread_messages_form.html", ctx)
        return render(request, "messaging/thread_detail.html", ctx)


class StartThreadView(LoginRequiredMixin, View):
    def get(self, request, user_id):
        return redirect("inbox")

    def post(self, request, user_id):
        other_user = get_object_or_404(User, pk=user_id)
        if not recipient_search_queryset(request.user).filter(pk=other_user.pk).exists():
            django_messages.error(request, "You cannot start a conversation with that user.")
            return redirect("inbox")
        if not users_may_message_each_other(request.user, other_user):
            django_messages.error(request, "Messaging is limited to users in your organisation.")
            return redirect("inbox")

        thread = Thread.objects.filter(participants=request.user).filter(participants=other_user).first()

        if not thread:
            org_id = resolve_thread_organisation(request.user, other_user)
            with transaction.atomic():
                thread = Thread.objects.create(
                    organisation_id=org_id,
                    thread_type=Thread.ThreadType.DIRECT,
                )
                thread.participants.add(request.user, other_user)

        return redirect(f"{reverse('inbox')}?thread={thread.pk}")


class StartOrgThreadView(LoginRequiredMixin, View):
    """Consultant: one shared thread with org staff (visible to all staff in the org)."""

    def get(self, request):
        return redirect("inbox")

    def post(self, request):
        user = request.user
        if user.role != User.Role.CONSULTANT or not user.organisation_id:
            return redirect("inbox")

        from django.db.models import Case, When

        staff = (
            User.objects.filter(
                organisation_id=user.organisation_id,
                role__in=(User.Role.ADMIN, User.Role.EMPLOYEE),
                is_active=True,
            )
            .annotate(
                role_rank=Case(
                    When(role=User.Role.ADMIN, then=0),
                    default=1,
                )
            )
            .order_by("role_rank", "pk")
            .first()
        )
        if not staff:
            django_messages.error(request, "No staff member is available for your organisation yet.")
            return redirect("inbox")

        existing = (
            Thread.objects.filter(
                thread_type=Thread.ThreadType.ORG_SHARED,
                organisation_id=user.organisation_id,
                participants=user,
            )
            .first()
        )
        if existing:
            return redirect(f"{reverse('inbox')}?thread={existing.pk}")

        with transaction.atomic():
            thread = Thread.objects.create(
                organisation_id=user.organisation_id,
                thread_type=Thread.ThreadType.ORG_SHARED,
            )
            thread.participants.add(user, staff)

        return redirect(f"{reverse('inbox')}?thread={thread.pk}")


@method_decorator(require_POST, name="dispatch")
class MessageEditView(LoginRequiredMixin, View):
    def post(self, request, pk):
        message = get_object_or_404(Message.objects.select_related("thread", "sender"), pk=pk)
        thread = message.thread
        if not user_can_access_thread(request.user, thread):
            return redirect("inbox")
        if not message.can_edit(request.user):
            django_messages.error(request, "You cannot edit this message.")
            return redirect("thread-detail", pk=thread.pk)

        form = MessageEditForm(request.POST)
        if form.is_valid():
            message.content = form.cleaned_data["content"].strip()
            message.edited_at = timezone.now()
            message.save(update_fields=["content", "edited_at"])
            thread.save(update_fields=["updated_at"])
        return redirect("thread-detail", pk=thread.pk)


@method_decorator(require_POST, name="dispatch")
class MessageDeleteView(LoginRequiredMixin, View):
    def post(self, request, pk):
        message = get_object_or_404(Message.objects.select_related("thread", "sender"), pk=pk)
        thread = message.thread
        if not user_can_access_thread(request.user, thread):
            return redirect("inbox")

        if not message.can_be_removed_by(request.user):
            return HttpResponseForbidden("You cannot remove this message.")

        message.content = ""
        message.deleted_at = timezone.now()
        message.save(update_fields=["content", "deleted_at"])
        thread.save(update_fields=["updated_at"])
        django_messages.success(request, "Message removed.")
        return redirect("thread-detail", pk=thread.pk)


@method_decorator(require_POST, name="dispatch")
class ThreadTypingPingView(LoginRequiredMixin, View):
    """Record that the current user is typing (cache TTL ~8s)."""

    def post(self, request, pk):
        thread = get_object_or_404(Thread, pk=pk)
        if not user_can_access_thread(request.user, thread):
            return HttpResponseForbidden()
        if not thread.participants.filter(pk=request.user.pk).exists():
            return HttpResponseForbidden()
        cache.set(
            _typing_cache_key(thread.pk, request.user.pk),
            timezone.now().timestamp(),
            TYPING_CACHE_TTL,
        )
        return HttpResponse(status=204)


class ThreadTypingStatusView(LoginRequiredMixin, View):
    """HTMX fragment: who else in the thread is typing."""

    def get(self, request, pk):
        thread = get_object_or_404(Thread.objects.prefetch_related("participants"), pk=pk)
        if not user_can_access_thread(request.user, thread):
            return HttpResponseForbidden()
        if not _htmx(request):
            return HttpResponse(status=204)
        other_ids = [p.pk for p in thread.participants.all() if p.pk != request.user.pk]
        now_ts = timezone.now().timestamp()
        typing_names = []
        for oid in other_ids:
            ts = cache.get(_typing_cache_key(thread.pk, oid))
            if ts is not None and (now_ts - float(ts)) < TYPING_CACHE_TTL:
                u = User.objects.filter(pk=oid).first()
                if u:
                    typing_names.append((u.get_full_name() or u.username).strip())
        return render(
            request,
            "messaging/_typing_indicator.html",
            {
                "typing_names": typing_names,
                "thread": thread,
            },
        )
