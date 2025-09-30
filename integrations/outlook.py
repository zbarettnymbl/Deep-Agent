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


class OutlookClient:
    """Minimal Graph API client tailored for the Deep Agent example."""

    def __init__(self, credentials: OutlookCredentials, session: Optional[requests.Session] = None):
        self._credentials = credentials
        self._session = session or requests.Session()
        self._app = msal.ConfidentialClientApplication(
            client_id=credentials.client_id,
            authority=f"https://login.microsoftonline.com/{credentials.tenant_id}",
            client_credential=credentials.client_secret,
        )
        self._priority_sender_weights = self._load_priority_sender_weights()

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

    def fetch_previous_day_emails(self) -> List[Dict[str, Any]]:
        """Return structured metadata for emails received on the previous work day."""

        date_range = self._previous_workday_range()
        params = {
            "$top": "25",
            "$orderby": "receivedDateTime desc",
            "$select": (
                "subject,from,receivedDateTime,importance,flag"
            ),  # from is reserved but valid here
            "$filter": (
                "receivedDateTime ge {} and receivedDateTime lt {}"
            ).format(date_range["start"].isoformat(), date_range["end"].isoformat()),
        }
        data = self._authorized_get(f"{GRAPH_BASE_URL}/me/messages", params)
        emails = []
        for item in data.get("value", []):
            emails.append(self._build_email_record(item))
        return emails

    def _build_email_record(self, item: Dict[str, Any]) -> Dict[str, Any]:
        sender = item.get("from", {}).get("emailAddress", {})
        subject = item.get("subject", "(no subject)")
        sender_name = sender.get("name") or sender.get("address") or "Unknown"
        sender_address = (sender.get("address") or "").lower()
        received_iso = item.get("receivedDateTime")
        importance = (item.get("importance") or "normal").lower()
        flag = item.get("flag") or {}
        flag_status = (flag.get("flagStatus") or "notFlagged").lower()
        due_date_iso = (flag.get("dueDateTime") or {}).get("dateTime")

        score, reasons = self._score_email(
            importance=importance,
            sender_address=sender_address,
            due_date_iso=due_date_iso,
            flag_status=flag_status,
            received_iso=received_iso,
        )

        return {
            "subject": subject,
            "sender": sender_name,
            "sender_address": sender_address,
            "received": received_iso,
            "importance": importance,
            "flag_status": flag_status,
            "due_date": due_date_iso,
            "priority_score": score,
            "priority_reasons": reasons,
        }

    def prioritized_previous_day_emails(self, *, limit: int = 5) -> List[Dict[str, Any]]:
        """Return the highest priority emails from the previous work day."""

        emails = self.fetch_previous_day_emails()
        if not emails:
            return []

        def sort_key(email: Dict[str, Any]) -> Tuple[int, datetime]:
            received_dt = self._parse_datetime(email.get("received"))
            if received_dt is None:
                received_dt = datetime.min.replace(tzinfo=UTC)
            return email.get("priority_score", 0), received_dt

        ranked = sorted(emails, key=sort_key, reverse=True)
        return ranked[: max(1, limit)]

    def _score_email(
        self,
        *,
        importance: str,
        sender_address: str,
        due_date_iso: Optional[str],
        flag_status: str,
        received_iso: Optional[str],
    ) -> Tuple[int, List[str]]:
        score = 0
        reasons: List[str] = []

        importance_level = (importance or "normal").lower()
        if importance_level == "high":
            score += 3
            reasons.append("Marked as high importance")
        elif importance_level == "low":
            score -= 1
            reasons.append("Marked as low importance")

        sender_weight = 0
        normalized_sender = (sender_address or "").lower()
        if normalized_sender:
            sender_weight = self._match_sender_weight(normalized_sender)
        if sender_weight:
            score += sender_weight
            reasons.append(
                f"Sender matches priority rules (+{sender_weight})"
            )

        if flag_status == "flagged":
            score += 1
            reasons.append("Message is flagged for follow-up")

        due_dt = self._parse_datetime(due_date_iso)
        if due_dt is not None:
            now = datetime.now(tz=UTC)
            if due_dt < now:
                score += 3
                reasons.append("Flag due date has passed")
            elif due_dt <= now + timedelta(days=1):
                score += 2
                reasons.append("Flag due within 24 hours")
            elif due_dt <= now + timedelta(days=2):
                score += 1
                reasons.append("Flag due within 48 hours")

        received_dt = self._parse_datetime(received_iso)
        if received_dt is not None:
            age = datetime.now(tz=UTC) - received_dt
            if age <= timedelta(hours=4):
                score += 1
                reasons.append("Received within the last 4 hours")

        if not reasons:
            reasons.append("No priority signals detected")

        return score, reasons

    def _match_sender_weight(self, sender: str) -> int:
        if not self._priority_sender_weights:
            return 0

        weight = self._priority_sender_weights.get(sender)
        if weight:
            return weight

        for rule, rule_weight in self._priority_sender_weights.items():
            if rule.startswith("@") and sender.endswith(rule):
                return rule_weight
        return 0

    @staticmethod
    def _load_priority_sender_weights() -> Dict[str, int]:
        raw = os.getenv("OUTLOOK_PRIORITY_SENDERS", "")
        weights: Dict[str, int] = {}
        if not raw:
            return weights

        for entry in raw.split(","):
            cleaned = entry.strip()
            if not cleaned:
                continue
            if ":" in cleaned:
                sender, weight_str = cleaned.split(":", 1)
                sender = sender.strip().lower()
                try:
                    weight = int(weight_str.strip())
                except ValueError:
                    weight = 3
            else:
                sender = cleaned.lower()
                weight = 3
            if sender:
                weights[sender] = weight
        return weights

    @staticmethod
    def _parse_datetime(iso_ts: Optional[str]) -> Optional[datetime]:
        if not iso_ts:
            return None
        sanitized = iso_ts.replace("Z", "+00:00") if iso_ts.endswith("Z") else iso_ts
        try:
            dt = datetime.fromisoformat(sanitized)
        except ValueError:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)

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
            lines.append(
                f"- {email.get('subject')} from {email.get('sender')} at {timestamp}"
            )
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

    def previous_day_priority_digest(self, limit: int = 5) -> List[Dict[str, Any]]:
        """Return prioritized email metadata for downstream tools."""

        prioritized = self.prioritized_previous_day_emails(limit=limit)
        digest: List[Dict[str, Any]] = []
        for email in prioritized:
            digest.append(
                {
                    "subject": email.get("subject"),
                    "sender": email.get("sender"),
                    "sender_address": email.get("sender_address"),
                    "received": email.get("received"),
                    "received_display": self._format_time(
                        email.get("received"), default="Unknown time"
                    ),
                    "due_date": email.get("due_date"),
                    "due_display": self._format_time(
                        email.get("due_date"), default="No deadline"
                    ),
                    "importance": email.get("importance"),
                    "flag_status": email.get("flag_status"),
                    "priority_score": email.get("priority_score"),
                    "priority_reasons": email.get("priority_reasons", []),
                }
            )
        return digest

    def previous_day_calendar_summary(self) -> str:
        return self.summarize_events(self.fetch_previous_day_events())

    def previous_day_briefing(self) -> str:
        """Combine email and calendar summaries into a single briefing."""

        email_summary = self.previous_day_email_summary()
        calendar_summary = self.previous_day_calendar_summary()
        briefing_lines = [
            "Previous workday briefing:",
            "Here is a combined snapshot of your inbox activity and meetings.",
            "",
            email_summary,
            "",
            calendar_summary,
        ]
        return "\n".join(briefing_lines)

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

    def daily_briefing_tool(_: str = "") -> str:
        return client.previous_day_briefing()

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

    class TopEmailPrioritiesInput(BaseModel):
        """Schema for requesting prioritized Outlook emails."""

        limit: int = Field(
            default=3,
            ge=1,
            le=25,
            description="Maximum number of prioritized emails to return.",
        )

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

    def prioritized_email_tool(limit: int = 3) -> str:
        digest = client.previous_day_priority_digest(limit=limit)
        payload = {"top_priorities": digest, "limit": limit}
        return json.dumps(payload, indent=2)

    tools: List[Tool] = [
        Tool(
            name="outlook_email_summary",
            description=(
                "Summarize emails received during the previous work day, highlighting "
                "subjects, senders, and timestamps."
            ),
            func=email_summary_tool,
        ),
        Tool(
            name="outlook_calendar_summary",
            description=(
                "Summarize meetings from the previous work day, including start times "
                "and key attendees."
            ),
            func=calendar_summary_tool,
        ),
        Tool(
            name="outlook_daily_briefing",
            description=(
                "Provide a combined previous workday briefing that blends email "
                "highlights with the calendar overview."
            ),
            func=daily_briefing_tool,
        ),
        StructuredTool.from_function(
            name="outlook_top_email_priorities",
            description=(
                "Return the highest priority Outlook emails from the previous work day "
                "based on importance markers, sender rules, and follow-up deadlines."
            ),
            func=prioritized_email_tool,
            args_schema=TopEmailPrioritiesInput,
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
    "create_outlook_tools",
]
