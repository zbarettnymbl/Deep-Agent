"""Microsoft Outlook / Graph integrations for Deep Agent examples.

This module encapsulates authentication via MSAL along with helper utilities
for fetching Microsoft 365 mailbox and calendar data relevant to the "previous
work day".  The resulting summaries are exposed as LangChain ``Tool`` objects so
that they can be consumed by agents in the repository's examples.
"""

from __future__ import annotations

import json
import os
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import msal
import requests
from langchain_core.tools import StructuredTool, Tool
from pydantic import BaseModel, Field, validator

GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"


logger = logging.getLogger(__name__)


class OutlookIntegrationError(RuntimeError):
    """Raised when the Outlook integration is not correctly configured."""


@dataclass
class OutlookCredentials:
    """Configuration required to authenticate with Microsoft Graph."""

    client_id: str
    tenant_id: str
    client_secret: str

    @classmethod
    def from_env(cls) -> "OutlookCredentials":
        """Load credentials from environment variables.

        Expected environment variables are:
        ``AZURE_CLIENT_ID``, ``AZURE_TENANT_ID``, ``AZURE_CLIENT_SECRET``.
        """

        client_id = os.getenv("AZURE_CLIENT_ID")
        tenant_id = os.getenv("AZURE_TENANT_ID")
        client_secret = os.getenv("AZURE_CLIENT_SECRET")

        if not all([client_id, tenant_id, client_secret]):
            raise OutlookIntegrationError(
                "Missing Azure AD credentials. Ensure AZURE_CLIENT_ID, "
                "AZURE_TENANT_ID, and AZURE_CLIENT_SECRET are set."
            )

        return cls(
            client_id=client_id,
            tenant_id=tenant_id,
            client_secret=client_secret,
        )


@dataclass
class EmailPriorityRecommendation:
    """Structured recommendation describing a prioritized email."""

    subject: str
    sender: str
    received: Optional[str]
    importance: str
    score: float
    reasons: List[str]
    due_by: Optional[str]
    sender_email: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "subject": self.subject,
            "sender": self.sender,
            "sender_email": self.sender_email,
            "received": self.received,
            "importance": self.importance,
            "score": round(self.score, 2),
            "reasons": self.reasons,
            "due_by": self.due_by,
        }


class EmailPrioritizer:
    """Apply heuristics to score Outlook messages based on urgency."""

    DEFAULT_IMPORTANCE_WEIGHTS: Dict[str, float] = {
        "high": 40.0,
        "normal": 10.0,
        "low": -5.0,
    }

    def __init__(
        self,
        *,
        sender_weights: Optional[Dict[str, float]] = None,
        domain_weights: Optional[Dict[str, float]] = None,
        importance_weights: Optional[Dict[str, float]] = None,
        flagged_weight: float = 15.0,
        overdue_weight: float = 35.0,
        due_soon_weight: float = 20.0,
        due_soon_hours: int = 24,
    ) -> None:
        self.sender_weights = {
            (email or "").lower(): weight
            for email, weight in (sender_weights or {}).items()
            if email
        }
        self.domain_weights = {
            (domain or "").lstrip("@").lower(): weight
            for domain, weight in (domain_weights or {}).items()
            if domain
        }
        self.importance_weights = (
            importance_weights or self.DEFAULT_IMPORTANCE_WEIGHTS
        )
        self.flagged_weight = flagged_weight
        self.overdue_weight = overdue_weight
        self.due_soon_weight = due_soon_weight
        self.due_soon_window = timedelta(hours=max(due_soon_hours, 1))

    @classmethod
    def from_env(cls) -> "EmailPrioritizer":
        """Build a prioritizer using optional environment configuration."""

        def _parse_weight_mapping(raw: str, default_weight: float) -> Dict[str, float]:
            mapping: Dict[str, float] = {}
            for token in raw.split(","):
                token = token.strip()
                if not token:
                    continue
                if "=" in token:
                    key, weight_str = token.split("=", 1)
                    try:
                        weight = float(weight_str.strip())
                    except ValueError:
                        weight = default_weight
                else:
                    key, weight = token, default_weight
                mapping[key.strip()] = weight
            return mapping

        sender_weights = _parse_weight_mapping(
            os.getenv("OUTLOOK_PRIORITY_SENDERS", ""), default_weight=30.0
        )
        domain_weights = _parse_weight_mapping(
            os.getenv("OUTLOOK_PRIORITY_DOMAINS", ""), default_weight=20.0
        )

        importance_weights: Dict[str, float] = {}
        importance_raw = os.getenv("OUTLOOK_IMPORTANCE_WEIGHTS")
        if importance_raw:
            try:
                data = json.loads(importance_raw)
                if isinstance(data, dict):
                    for key, value in data.items():
                        try:
                            importance_weights[str(key).lower()] = float(value)
                        except (TypeError, ValueError):
                            continue
            except json.JSONDecodeError:
                logger.warning(
                    "Invalid JSON provided in OUTLOOK_IMPORTANCE_WEIGHTS; falling back to defaults."
                )

        try:
            due_soon_hours = int(os.getenv("OUTLOOK_DEADLINE_SOON_HOURS", "24"))
        except ValueError:
            due_soon_hours = 24

        try:
            flagged_weight = float(os.getenv("OUTLOOK_FLAGGED_WEIGHT", "15"))
        except ValueError:
            flagged_weight = 15.0

        try:
            overdue_weight = float(os.getenv("OUTLOOK_OVERDUE_WEIGHT", "35"))
        except ValueError:
            overdue_weight = 35.0

        try:
            due_soon_weight = float(os.getenv("OUTLOOK_DUE_SOON_WEIGHT", "20"))
        except ValueError:
            due_soon_weight = 20.0

        return cls(
            sender_weights=sender_weights,
            domain_weights=domain_weights,
            importance_weights=importance_weights or None,
            flagged_weight=flagged_weight,
            overdue_weight=overdue_weight,
            due_soon_weight=due_soon_weight,
            due_soon_hours=due_soon_hours,
        )

    @staticmethod
    def parse_due_datetime(
        date_str: Optional[str], time_zone: Optional[str]
    ) -> Optional[datetime]:
        if not date_str:
            return None
        sanitized = date_str.replace("Z", "+00:00") if date_str.endswith("Z") else date_str
        try:
            parsed = datetime.fromisoformat(sanitized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            if time_zone:
                try:
                    parsed = parsed.replace(tzinfo=ZoneInfo(time_zone))
                except Exception:
                    parsed = parsed.replace(tzinfo=UTC)
            else:
                parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def score_email(self, email: Dict[str, Any]) -> Tuple[float, List[str]]:
        """Return a score and rationale for the provided email record."""

        score = 0.0
        reasons: List[str] = []

        importance = str(email.get("importance") or "normal").lower()
        if importance in self.importance_weights:
            importance_score = self.importance_weights[importance]
            score += importance_score
            reasons.append(f"Marked as {importance} importance")

        sender_email = str(email.get("sender_email") or email.get("sender") or "").lower()
        if sender_email in self.sender_weights:
            weight = self.sender_weights[sender_email]
            score += weight
            reasons.append("Matches high-priority sender rule")
        else:
            domain = sender_email.split("@", 1)[-1] if "@" in sender_email else ""
            if domain in self.domain_weights:
                weight = self.domain_weights[domain]
                score += weight
                reasons.append("Matches high-priority domain rule")

        if email.get("is_flagged"):
            score += self.flagged_weight
            reasons.append("Flagged for follow-up")

        due_info = email.get("due_info", {})
        due_dt = None
        if isinstance(due_info, dict):
            due_dt = self.parse_due_datetime(
                due_info.get("dateTime"), due_info.get("timeZone")
            )
        elif isinstance(due_info, str):
            due_dt = self.parse_due_datetime(due_info, None)

        if due_dt:
            now = datetime.now(tz=UTC)
            if due_dt <= now:
                score += self.overdue_weight
                reasons.append("Deadline has passed")
            elif due_dt - now <= self.due_soon_window:
                score += self.due_soon_weight
                reasons.append(
                    f"Due within {int(self.due_soon_window.total_seconds() // 3600)}h"
                )

        return score, reasons

class OutlookClient:
    """Minimal Graph API client tailored for the Deep Agent example."""

    def __init__(
        self,
        credentials: OutlookCredentials,
        session: Optional[requests.Session] = None,
        prioritizer: Optional[EmailPrioritizer] = None,
    ):
        self._credentials = credentials
        self._session = session or requests.Session()
        self._prioritizer = prioritizer or EmailPrioritizer.from_env()
        self._app = msal.ConfidentialClientApplication(
            client_id=credentials.client_id,
            authority=f"https://login.microsoftonline.com/{credentials.tenant_id}",
            client_credential=credentials.client_secret,
        )

    def _get_access_token(self) -> str:
        result = self._app.acquire_token_silent(scopes=GRAPH_SCOPE, account=None)
        if not result:
            result = self._app.acquire_token_for_client(scopes=GRAPH_SCOPE)
        if "access_token" not in result:
            raise OutlookIntegrationError(
                f"Unable to acquire access token: {result.get('error_description', result)}"
            )
        return str(result["access_token"])

    def _authorized_get(self, url: str, params: Optional[Dict[str, str]] = None) -> Dict:
        token = self._get_access_token()
        response = self._session.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        if not response.ok:
            raise OutlookIntegrationError(
                f"Graph API request failed ({response.status_code}): {response.text}"
            )
        return response.json()

    def _authorized_post(self, url: str, payload: Optional[Dict] = None) -> Dict:
        token = self._get_access_token()
        response = self._session.post(
            url,
            json=payload or {},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        if response.status_code not in {200, 201, 202, 204}:
            raise OutlookIntegrationError(
                f"Graph API POST failed ({response.status_code}): {response.text}"
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    @staticmethod
    def _format_recipients(addresses: Sequence[str]) -> List[Dict[str, Dict[str, str]]]:
        recipients: List[Dict[str, Dict[str, str]]] = []
        for address in addresses:
            if not address:
                continue
            trimmed = address.strip()
            if not trimmed:
                continue
            recipients.append({"emailAddress": {"address": trimmed}})
        if not recipients:
            raise OutlookIntegrationError("At least one recipient email address is required.")
        return recipients

    @staticmethod
    def _previous_workday_range(now: Optional[datetime] = None) -> Dict[str, datetime]:
        now = now or datetime.now(tz=UTC)
        date = now.date()
        # Move back one day; if today is Monday, go back to Friday.
        previous_day = date - timedelta(days=1)
        if previous_day.weekday() == 6:  # Sunday -> go back to Friday
            previous_day -= timedelta(days=2)
        elif previous_day.weekday() == 5:  # Saturday -> go back to Friday
            previous_day -= timedelta(days=1)

        start = datetime.combine(previous_day, time.min, tzinfo=UTC)
        end = start + timedelta(days=1)
        return {"start": start, "end": end}

    def _normalize_due(self, flag_payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not flag_payload:
            return None
        due = flag_payload.get("dueDateTime")
        if isinstance(due, dict) and due.get("dateTime"):
            return due
        return None

    def fetch_previous_day_emails(self) -> List[Dict[str, Any]]:
        """Return structured metadata for emails received on the previous work day."""

        date_range = self._previous_workday_range()
        params = {
            "$top": "25",
            "$orderby": "receivedDateTime desc",
            "$select": "subject,from,receivedDateTime,importance,flag",
            "$filter": (
                "receivedDateTime ge {} and receivedDateTime lt {}"
            ).format(date_range["start"].isoformat(), date_range["end"].isoformat()),
        }
        data = self._authorized_get(f"{GRAPH_BASE_URL}/me/messages", params)
        emails: List[Dict[str, Any]] = []
        for item in data.get("value", []):
            sender = item.get("from", {}).get("emailAddress", {})
            flag_payload = item.get("flag") or {}
            flag_status = str(flag_payload.get("flagStatus") or "").lower()
            due_info = self._normalize_due(flag_payload)
            normalized: Dict[str, Any] = {
                "subject": item.get("subject", "(no subject)"),
                "sender": sender.get("name") or sender.get("address") or "Unknown",
                "sender_email": sender.get("address"),
                "received": item.get("receivedDateTime"),
                "importance": item.get("importance", "normal"),
                "is_flagged": flag_status == "flagged",
                "due_info": due_info,
            }
            score, reasons = self._prioritizer.score_email(normalized)
            normalized["score"] = score
            normalized["reasons"] = reasons
            normalized["due_by"] = None
            if due_info and isinstance(due_info, dict):
                due_dt = self._prioritizer.parse_due_datetime(
                    due_info.get("dateTime"), due_info.get("timeZone")
                )
                if due_dt:
                    normalized["due_by"] = due_dt.isoformat()
            emails.append(normalized)
        return emails

    def prioritized_email_recommendations(
        self, limit: int = 3
    ) -> List[EmailPriorityRecommendation]:
        """Return top-N scored emails based on prioritization heuristics."""

        emails = self.fetch_previous_day_emails()
        if not emails:
            return []

        ranked = sorted(
            emails,
            key=lambda payload: float(payload.get("score", 0.0)),
            reverse=True,
        )

        recommendations: List[EmailPriorityRecommendation] = []
        for email in ranked[: max(limit, 0)]:
            recommendations.append(
                EmailPriorityRecommendation(
                    subject=str(email.get("subject")),
                    sender=str(email.get("sender")),
                    sender_email=email.get("sender_email"),
                    received=email.get("received"),
                    importance=str(email.get("importance", "normal")),
                    score=float(email.get("score", 0.0)),
                    reasons=list(email.get("reasons", [])),
                    due_by=email.get("due_by"),
                )
            )
        return recommendations

    def fetch_previous_day_events(self) -> List[Dict[str, str]]:
        """Return structured metadata for calendar events scheduled on the previous work day."""

        date_range = self._previous_workday_range()
        params = {
            "$top": "25",
            "$orderby": "start/dateTime",
            "$select": "subject,start,end,attendees,organizer",
            "$filter": (
                "start/dateTime ge {} and start/dateTime lt {}"
            ).format(date_range["start"].isoformat(), date_range["end"].isoformat()),
        }
        data = self._authorized_get(f"{GRAPH_BASE_URL}/me/events", params)
        events = []
        for item in data.get("value", []):
            attendees = [
                attendee.get("emailAddress", {}).get("name")
                or attendee.get("emailAddress", {}).get("address")
                for attendee in item.get("attendees", [])
            ]
            attendees = [name for name in attendees if name]
            events.append(
                {
                    "subject": item.get("subject", "(no subject)"),
                    "start": item.get("start", {}).get("dateTime"),
                    "end": item.get("end", {}).get("dateTime"),
                    "organizer": item.get("organizer", {})
                    .get("emailAddress", {})
                    .get("name"),
                    "attendees": attendees,
                }
            )
        return events

    def summarize_emails(self, emails: Iterable[Dict[str, Any]]) -> str:
        lines = ["Previous workday email highlights:"]
        for email in emails:
            received = email.get("received")
            timestamp = self._format_time(received, default="Unknown time")
            description = f"- {email.get('subject')} from {email.get('sender')} at {timestamp}"
            if email.get("due_by"):
                due_display = self._format_time(email.get("due_by"), default="Unknown deadline")
                description += f" (due by {due_display})"
            if email.get("score") is not None:
                description += f" [score: {float(email.get('score', 0.0)):.1f}]"
            lines.append(description)
        if len(lines) == 1:
            lines.append("- No email activity detected during the previous work day.")
        return "\n".join(lines)

    def summarize_events(self, events: Iterable[Dict[str, str]]) -> str:
        lines = ["Previous workday calendar overview:"]
        for event in events:
            start = event.get("start")
            start_display = self._format_time(start, default="Unknown start")
            attendees = ", ".join(event.get("attendees") or []) or "No attendees listed"
            lines.append(
                f"- {event.get('subject')} starting {start_display}; attendees: {attendees}"
            )
        if len(lines) == 1:
            lines.append("- No meetings were scheduled on the previous work day.")
        return "\n".join(lines)

    @staticmethod
    def _format_time(iso_ts: Optional[str], *, default: str) -> str:
        if not iso_ts:
            return default
        sanitized = iso_ts.replace("Z", "+00:00") if iso_ts.endswith("Z") else iso_ts
        try:
            dt = datetime.fromisoformat(sanitized)
        except ValueError:
            return default
        return dt.astimezone(UTC).strftime("%H:%M UTC")

    def previous_day_email_summary(self) -> str:
        return self.summarize_emails(self.fetch_previous_day_emails())

    def previous_day_calendar_summary(self) -> str:
        return self.summarize_events(self.fetch_previous_day_events())

    def send_mail(
        self,
        *,
        subject: str,
        body: str,
        to_recipients: Sequence[str],
        cc_recipients: Optional[Sequence[str]] = None,
        bcc_recipients: Optional[Sequence[str]] = None,
        save_to_sent_items: bool = True,
    ) -> str:
        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": body},
                "toRecipients": self._format_recipients(to_recipients),
            },
            "saveToSentItems": save_to_sent_items,
        }
        if cc_recipients:
            payload["message"]["ccRecipients"] = self._format_recipients(cc_recipients)
        if bcc_recipients:
            payload["message"]["bccRecipients"] = self._format_recipients(bcc_recipients)

        logger.info("Sending Outlook email with subject '%s' to %s", subject, to_recipients)
        self._authorized_post(f"{GRAPH_BASE_URL}/me/sendMail", payload)
        return "Email sent successfully."

    def reply_to_message(
        self,
        *,
        message_id: str,
        comment: str,
        reply_all: bool = False,
    ) -> str:
        endpoint = "replyAll" if reply_all else "reply"
        payload = {"comment": comment}
        logger.info("Replying to Outlook message %s (reply_all=%s)", message_id, reply_all)
        self._authorized_post(f"{GRAPH_BASE_URL}/me/messages/{message_id}/{endpoint}", payload)
        return "Reply sent successfully."

    def forward_message(
        self,
        *,
        message_id: str,
        comment: str,
        to_recipients: Sequence[str],
    ) -> str:
        payload = {
            "comment": comment,
            "toRecipients": self._format_recipients(to_recipients),
        }
        logger.info("Forwarding Outlook message %s to %s", message_id, to_recipients)
        self._authorized_post(f"{GRAPH_BASE_URL}/me/messages/{message_id}/forward", payload)
        return "Forward sent successfully."

    def create_meeting(
        self,
        *,
        subject: str,
        start: str,
        end: str,
        attendees: Sequence[str],
        body: str = "",
        location: Optional[str] = None,
    ) -> str:
        event_payload = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "start": {"dateTime": start, "timeZone": "UTC"},
            "end": {"dateTime": end, "timeZone": "UTC"},
            "attendees": [
                {
                    "emailAddress": {"address": addr.strip()},
                    "type": "required",
                }
                for addr in attendees
                if addr.strip()
            ],
        }
        if not event_payload["attendees"]:
            raise OutlookIntegrationError("At least one attendee is required to create a meeting.")
        if location:
            event_payload["location"] = {"displayName": location}

        logger.info(
            "Creating Outlook meeting '%s' from %s to %s for attendees %s",
            subject,
            start,
            end,
            attendees,
        )
        self._authorized_post(f"{GRAPH_BASE_URL}/me/events", event_payload)
        return "Meeting created successfully."

    def respond_to_invite(
        self,
        *,
        event_id: str,
        response: str,
        comment: str = "",
        send_response: bool = True,
    ) -> str:
        normalized = response.lower()
        endpoint_map = {
            "accept": "accept",
            "decline": "decline",
            "tentative": "tentativelyAccept",
        }
        if normalized not in endpoint_map:
            raise OutlookIntegrationError(
                "Response must be one of 'accept', 'decline', or 'tentative'."
            )
        payload = {"comment": comment, "sendResponse": send_response}
        endpoint = endpoint_map[normalized]
        logger.info(
            "Responding to Outlook invite %s with '%s' (send_response=%s)",
            event_id,
            normalized,
            send_response,
        )
        self._authorized_post(f"{GRAPH_BASE_URL}/me/events/{event_id}/{endpoint}", payload)
        return f"Invite response '{normalized}' sent successfully."


def create_outlook_tools(client: Optional[OutlookClient] = None) -> List[Tool]:
    """Create LangChain tools that expose Outlook email and calendar summaries."""

    client = client or OutlookClient(OutlookCredentials.from_env())

    def email_summary_tool(_: str = "") -> str:
        return client.previous_day_email_summary()

    def calendar_summary_tool(_: str = "") -> str:
        return client.previous_day_calendar_summary()

    def prioritized_email_tool(*, limit: int = 3) -> str:
        recommendations = client.prioritized_email_recommendations(limit=limit)
        payload = {
            "recommendations": [recommendation.as_dict() for recommendation in recommendations]
        }
        return json.dumps(payload, indent=2)

    class SendMailInput(BaseModel):
        """Schema for composing and sending a new Outlook email."""

        subject: str = Field(..., description="Email subject line.")
        body: str = Field(..., description="Plain text body of the email message.")
        to_recipients: List[str] = Field(
            ..., description="List of primary recipient email addresses."
        )
        cc_recipients: List[str] = Field(
            default_factory=list,
            description="Optional list of CC recipient email addresses.",
        )
        bcc_recipients: List[str] = Field(
            default_factory=list,
            description="Optional list of BCC recipient email addresses.",
        )
        save_to_sent_items: bool = Field(
            default=True,
            description="Whether to save the message to the Sent Items folder.",
        )

        @validator("to_recipients", "cc_recipients", "bcc_recipients", pre=True)
        def _ensure_list(cls, value):  # type: ignore[override]
            if value is None:
                return []
            if isinstance(value, str):
                return [value]
            return list(value)

    class ReplyToMessageInput(BaseModel):
        """Schema for replying to an existing Outlook email."""

        message_id: str = Field(..., description="Graph message ID to reply to.")
        comment: str = Field(
            default="",
            description="Optional note included in the reply body.",
        )
        reply_all: bool = Field(
            default=False,
            description="Whether to send the reply to all original recipients.",
        )

    class ForwardMessageInput(BaseModel):
        """Schema for forwarding an Outlook email."""

        message_id: str = Field(..., description="Graph message ID to forward.")
        comment: str = Field(
            default="",
            description="Optional text added to the forwarded message.",
        )
        to_recipients: List[str] = Field(
            ..., description="Email addresses that should receive the forward."
        )

        @validator("to_recipients", pre=True)
        def _ensure_forward_list(cls, value):  # type: ignore[override]
            if isinstance(value, str):
                return [value]
            return list(value)

    class CreateMeetingInput(BaseModel):
        """Schema for creating a new Outlook meeting."""

        subject: str = Field(..., description="Meeting subject line.")
        start: str = Field(
            ..., description="Meeting start timestamp in ISO 8601 format (UTC)."
        )
        end: str = Field(
            ..., description="Meeting end timestamp in ISO 8601 format (UTC)."
        )
        attendees: List[str] = Field(
            ..., description="Email addresses of required attendees."
        )
        body: str = Field(
            default="",
            description="Optional agenda or notes included in the invite.",
        )
        location: Optional[str] = Field(
            default=None,
            description="Optional display name for the meeting location.",
        )

        @validator("attendees", pre=True)
        def _ensure_attendees(cls, value):  # type: ignore[override]
            if isinstance(value, str):
                return [value]
            return list(value)

    class RespondToInviteInput(BaseModel):
        """Schema for responding to an Outlook meeting invitation."""

        event_id: str = Field(..., description="Graph event ID for the invitation.")
        response: str = Field(
            ..., description="Response type: accept, decline, or tentative."
        )
        comment: str = Field(
            default="",
            description="Optional note sent with the response.",
        )
        send_response: bool = Field(
            default=True,
            description="Whether Outlook should send a response email.",
        )

    class PrioritizedEmailsInput(BaseModel):
        """Schema for requesting prioritized email recommendations."""

        limit: int = Field(
            default=3,
            ge=1,
            le=25,
            description="Number of top priority emails to include in the response.",
        )

    tools: List[Tool] = [
        Tool(
            name="outlook_email_summary",
            description=(
                "Summarize emails received during the previous work day, highlighting "
                "subjects, senders, and timestamps."
            ),
            func=email_summary_tool,
        ),
        StructuredTool.from_function(
            name="outlook_top_priority_emails",
            description=(
                "Return the highest priority emails from the previous work day using "
                "importance flags, sender rules, and deadlines."
            ),
            func=prioritized_email_tool,
            args_schema=PrioritizedEmailsInput,
        ),
        Tool(
            name="outlook_calendar_summary",
            description=(
                "Summarize meetings from the previous work day, including start times "
                "and key attendees."
            ),
            func=calendar_summary_tool,
        ),
        StructuredTool.from_function(
            name="outlook_send_mail",
            description=(
                "Send a new Outlook email. Use only after the user confirms the "
                "recipients, subject, and body are correct."
            ),
            func=lambda **kwargs: client.send_mail(**kwargs),
            args_schema=SendMailInput,
        ),
        StructuredTool.from_function(
            name="outlook_reply_to_message",
            description=(
                "Reply to an existing Outlook message by ID. Confirm the user wants "
                "to reply and whether to include all recipients before calling."
            ),
            func=lambda **kwargs: client.reply_to_message(**kwargs),
            args_schema=ReplyToMessageInput,
        ),
        StructuredTool.from_function(
            name="outlook_forward_message",
            description=(
                "Forward an Outlook message to new recipients. Ensure forwarding is "
                "explicitly requested and the destination addresses are confirmed."
            ),
            func=lambda **kwargs: client.forward_message(**kwargs),
            args_schema=ForwardMessageInput,
        ),
        StructuredTool.from_function(
            name="outlook_create_meeting",
            description=(
                "Schedule a new Outlook meeting. Require user confirmation of "
                "timing, attendees, and agenda before executing."
            ),
            func=lambda **kwargs: client.create_meeting(**kwargs),
            args_schema=CreateMeetingInput,
        ),
        StructuredTool.from_function(
            name="outlook_respond_to_invite",
            description=(
                "Respond to an Outlook meeting invitation. Only call after the user "
                "specifies the desired response."
            ),
            func=lambda **kwargs: client.respond_to_invite(**kwargs),
            args_schema=RespondToInviteInput,
        ),
    ]

    return tools


__all__ = [
    "OutlookClient",
    "OutlookCredentials",
    "OutlookIntegrationError",
    "EmailPriorityRecommendation",
    "EmailPrioritizer",
    "create_outlook_tools",
]
