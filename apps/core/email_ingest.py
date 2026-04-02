import imaplib
import email
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import Optional, Tuple
import socket

from django.utils import timezone
from django.db import transaction

from .models import PlatformConfig
from .security import decrypt_value
from submissions.models import ApplicationSubmission, EmailEvent, record_submission_status_change


def _decode_header(value: str) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for text, encoding in parts:
        if isinstance(text, bytes):
            decoded.append(text.decode(encoding or "utf-8", errors="ignore"))
        else:
            decoded.append(text)
    return "".join(decoded)


def _get_body_snippet(msg: email.message.Message, max_chars: int = 500) -> str:
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain":
                try:
                    body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="ignore")
                except Exception:
                    continue
                break
    else:
        try:
            body = msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", errors="ignore")
        except Exception:
            body = ""
    body = body.strip()
    if len(body) > max_chars:
        return body[:max_chars] + "..."
    return body


def _detect_status(subject: str, body: str) -> Tuple[str, int]:
    """
    Very simple rules-based detector that returns (detected_status, confidence).
    """
    text = f"{subject} {body}".lower()
    if any(keyword in text for keyword in ["interview", "schedule", "scheduling", "screening call"]):
        return EmailEvent.DetectedStatus.INTERVIEW, 85
    if any(keyword in text for keyword in ["offer", "congratulations", "we are pleased to", "compensation package"]):
        return EmailEvent.DetectedStatus.OFFER, 90
    if any(keyword in text for keyword in ["regret", "unfortunately", "not moving forward", "decline", "reject"]):
        return EmailEvent.DetectedStatus.REJECTED, 85
    return EmailEvent.DetectedStatus.UNKNOWN, 0


def _ai_fallback(subject: str, body: str) -> dict:
    """
    Uses the existing LLMService (if configured) to classify an email.
    Returns dict: {status, confidence, candidate_name, company, job_title, error}
    """
    try:
        import json
        from resumes.services import LLMService
    except Exception as exc:
        return {"status": EmailEvent.DetectedStatus.UNKNOWN, "confidence": 0, "error": str(exc)}

    llm = LLMService()
    if not getattr(llm, "client", None):
        return {"status": EmailEvent.DetectedStatus.UNKNOWN, "confidence": 0, "error": "llm_not_configured"}

    system_prompt = (
        "You classify recruitment status update emails.\n"
        "Return ONLY strict JSON with keys: status, confidence, candidate_name, company, job_title.\n"
        "status must be one of: IN_PROGRESS, APPLIED, INTERVIEW, OFFER, REJECTED, UNKNOWN.\n"
        "confidence is an integer 0-100.\n"
    )
    user_prompt = (
        f"Subject:\n{subject}\n\n"
        f"Body:\n{body}\n\n"
        "Return JSON only."
    )

    content, _tokens, error = llm.generate_with_prompts(
        job=None,
        consultant=None,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        actor=None,
        temperature_override=0.0,
    )
    if error or not content:
        return {"status": EmailEvent.DetectedStatus.UNKNOWN, "confidence": 0, "error": error or "empty_response"}

    try:
        data = json.loads(content.strip())
    except Exception:
        return {"status": EmailEvent.DetectedStatus.UNKNOWN, "confidence": 0, "error": "invalid_json"}

    raw_status = (data.get("status") or "UNKNOWN").strip().upper()
    status_map = {
        "IN_PROGRESS": ApplicationSubmission.Status.IN_PROGRESS,
        "APPLIED": ApplicationSubmission.Status.APPLIED,
        "INTERVIEW": ApplicationSubmission.Status.INTERVIEW,
        "OFFER": ApplicationSubmission.Status.OFFER,
        "REJECTED": ApplicationSubmission.Status.REJECTED,
        "UNKNOWN": EmailEvent.DetectedStatus.UNKNOWN,
    }
    status = status_map.get(raw_status, EmailEvent.DetectedStatus.UNKNOWN)
    try:
        confidence = int(data.get("confidence") or 0)
    except Exception:
        confidence = 0

    return {
        "status": status,
        "confidence": max(0, min(100, confidence)),
        "candidate_name": (data.get("candidate_name") or "").strip(),
        "company": (data.get("company") or "").strip(),
        "job_title": (data.get("job_title") or "").strip(),
        "error": None,
    }


def _is_transition_valid(current: str, new: str) -> bool:
    if new == EmailEvent.DetectedStatus.UNKNOWN:
        return False
    terminal = {
        ApplicationSubmission.Status.REJECTED,
        ApplicationSubmission.Status.WITHDRAWN,
    }
    if current in terminal:
        return False
    if current == new:
        return False
    return True


def _find_single_matching_submission(subject: str, body: str) -> Optional[ApplicationSubmission]:
    """
    Simple heuristic: look for job title tokens and consultant name tokens.
    To keep it cheap, we only search among recent submissions.
    """
    recent_submissions = ApplicationSubmission.objects.select_related("job", "consultant__user")[:200]
    matches = []
    text = f"{subject} {body}".lower()
    for submission in recent_submissions:
        title = (submission.job.title or "").lower()
        consultant_name = submission.consultant.user.get_full_name() or submission.consultant.user.username
        consultant_name = (consultant_name or "").lower()
        score = 0
        if title and title in text:
            score += 1
        if consultant_name and consultant_name in text:
            score += 1
        if score >= 1:
            matches.append(submission)
    if len(matches) == 1:
        return matches[0]
    return None


def fetch_unseen_and_process(dry_run: bool = False, max_messages: int = 20) -> dict:
    """
    Connect to IMAP, fetch UNSEEN emails, log them as EmailEvent,
    and optionally auto-update submissions using rules-only detection.
    """
    config = PlatformConfig.load()
    if not config.email_ingest_enabled:
        return {"processed": 0, "reason": "disabled"}

    host = config.email_imap_host
    port = config.email_imap_port
    username = config.email_imap_username
    password = decrypt_value(config.email_imap_encrypted_password)

    if not (host and port and username and password):
        return {"processed": 0, "reason": "missing_credentials"}

    processed = 0
    auto_updated = 0
    needs_review = 0

    try:
        if config.email_imap_use_ssl:
            client = imaplib.IMAP4_SSL(host, port)
        else:
            client = imaplib.IMAP4(host, port)
    except (socket.gaierror, OSError) as exc:
        return {"processed": 0, "reason": f"connect_failed: {exc}"}

    try:
        try:
            client.login(username, password)
        except imaplib.IMAP4.error as exc:
            return {"processed": 0, "reason": f"auth_failed: {exc}"}

        client.select("INBOX")

        status, data = client.search(None, "UNSEEN")
        if status != "OK":
            return {"processed": 0, "reason": "search_failed"}

        ids = data[0].split()
        for msg_id in ids[:max_messages]:
            status, msg_data = client.fetch(msg_id, "(RFC822)")
            if status != "OK" or not msg_data:
                continue

            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)

            subject = _decode_header(msg.get("Subject", ""))
            from_addr = _decode_header(msg.get("From", ""))
            to_addr = _decode_header(msg.get("To", ""))
            message_id = msg.get("Message-Id", "") or msg.get("Message-ID", "")

            # Parse received date; fallback to now
            date_hdr = msg.get("Date")
            try:
                received_at = parsedate_to_datetime(date_hdr) if date_hdr else timezone.now()
                if timezone.is_naive(received_at):
                    received_at = timezone.make_aware(received_at, timezone=timezone.utc)
            except Exception:
                received_at = timezone.now()

            body_snippet = _get_body_snippet(msg)
            detected_status, confidence = _detect_status(subject, body_snippet)

            detected_candidate_name = ""
            detected_company = ""
            detected_job_title = ""

            # Optional AI fallback (token usage) when rules are unsure.
            if config.email_ai_fallback_enabled and (
                detected_status == EmailEvent.DetectedStatus.UNKNOWN or confidence < 70
            ):
                ai = _ai_fallback(subject, body_snippet)
                ai_status = ai.get("status", EmailEvent.DetectedStatus.UNKNOWN)
                ai_conf = int(ai.get("confidence") or 0)
                threshold = int(getattr(config, "email_ai_confidence_threshold", 80) or 80)
                if ai_status != EmailEvent.DetectedStatus.UNKNOWN and ai_conf >= threshold:
                    detected_status, confidence = ai_status, ai_conf
                    detected_candidate_name = ai.get("candidate_name") or ""
                    detected_company = ai.get("company") or ""
                    detected_job_title = ai.get("job_title") or ""

            matched_submission = _find_single_matching_submission(subject, body_snippet) if detected_status != EmailEvent.DetectedStatus.UNKNOWN else None

            applied_action = EmailEvent.AppliedAction.NONE

            if not dry_run and matched_submission and _is_transition_valid(matched_submission.status, detected_status):
                with transaction.atomic():
                    from_status = matched_submission.status
                    matched_submission.status = detected_status
                    matched_submission.save(update_fields=["status", "updated_at"])
                    note = f"Email parser (rules): {subject[:120]}"
                    record_submission_status_change(
                        matched_submission,
                        to_status=detected_status,
                        from_status=from_status,
                        note=note,
                    )

                # Audit log (best-effort)
                try:
                    from core.models import AuditLog
                    AuditLog.objects.create(
                        actor=None,
                        action="email_ingest_auto_update",
                        target_model="submissions.ApplicationSubmission",
                        target_id=str(matched_submission.pk),
                        details={
                            "detected_status": detected_status,
                            "confidence": confidence,
                            "subject": subject[:200],
                            "email_from": from_addr[:255],
                        },
                    )
                except Exception:
                    pass

                # Optional email notifications (best-effort)
                try:
                    from django.core.mail import send_mail
                    subject_line = f"CHENN: Application auto-updated → {detected_status}"
                    body_line = (
                        f"Submission #{matched_submission.pk} was auto-updated by email ingestion.\n"
                        f"New status: {detected_status}\n"
                        f"Job: {matched_submission.job.title} @ {matched_submission.job.company}\n"
                        f"Consultant: {matched_submission.consultant.user.get_full_name() or matched_submission.consultant.user.username}\n"
                        f"Email subject: {subject}\n"
                    )
                    if getattr(config, "email_notify_employee_on_auto_update", False) and matched_submission.submitted_by and matched_submission.submitted_by.email:
                        send_mail(subject_line, body_line, None, [matched_submission.submitted_by.email], fail_silently=True)
                    if getattr(config, "email_notify_consultant_on_auto_update", False) and matched_submission.consultant and matched_submission.consultant.user.email:
                        send_mail(subject_line, body_line, None, [matched_submission.consultant.user.email], fail_silently=True)
                except Exception:
                    pass

                applied_action = EmailEvent.AppliedAction.AUTO_UPDATED
                auto_updated += 1
            elif detected_status != EmailEvent.DetectedStatus.UNKNOWN and matched_submission is None:
                applied_action = EmailEvent.AppliedAction.NEEDS_REVIEW
                needs_review += 1

            if not dry_run:
                EmailEvent.objects.create(
                    received_at=received_at,
                    from_address=from_addr,
                    to_address=to_addr,
                    subject=subject[:500],
                    body_snippet=body_snippet,
                    raw_message_id=message_id[:255],
                    detected_status=detected_status,
                    detected_candidate_name=detected_candidate_name,
                    detected_company=detected_company,
                    detected_job_title=detected_job_title,
                    confidence=confidence,
                    matched_submission=matched_submission,
                    applied_action=applied_action,
                )

            # Mark as seen so we don't process again
            client.store(msg_id, "+FLAGS", "\\Seen")
            processed += 1

    finally:
        try:
            client.close()
        except Exception:
            pass
        try:
            client.logout()
        except Exception:
            pass

    return {
        "processed": processed,
        "auto_updated": auto_updated,
        "needs_review": needs_review,
        "dry_run": dry_run,
    }

