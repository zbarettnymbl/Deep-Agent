"""Microsoft Outlook / Graph integrations for Deep Agent examples.

This module encapsulates authentication via MSAL along with helper utilities
for fetching Microsoft 365 mailbox and calendar data relevant to the "previous
work day".  The resulting summaries are exposed as LangChain ``Tool`` objects so
that they can be consumed by agents in the repository's examples.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Dict, Iterable, List, Optional

import msal
import requests
from langchain_core.tools import Tool

GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"


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

    def fetch_previous_day_emails(self) -> List[Dict[str, str]]:
        """Return structured metadata for emails received on the previous work day."""

        date_range = self._previous_workday_range()
        params = {
            "$top": "25",
            "$orderby": "receivedDateTime desc",
            "$select": "subject,from,receivedDateTime",  # from is reserved but valid here
            "$filter": (
                "receivedDateTime ge {} and receivedDateTime lt {}"
            ).format(date_range["start"].isoformat(), date_range["end"].isoformat()),
        }
        data = self._authorized_get(f"{GRAPH_BASE_URL}/me/messages", params)
        emails = []
        for item in data.get("value", []):
            sender = item.get("from", {}).get("emailAddress", {})
            emails.append(
                {
                    "subject": item.get("subject", "(no subject)"),
                    "sender": sender.get("name") or sender.get("address") or "Unknown",
                    "received": item.get("receivedDateTime"),
                }
            )
        return emails

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

    def summarize_emails(self, emails: Iterable[Dict[str, str]]) -> str:
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

    def previous_day_calendar_summary(self) -> str:
        return self.summarize_events(self.fetch_previous_day_events())


def create_outlook_tools(client: Optional[OutlookClient] = None) -> List[Tool]:
    """Create LangChain tools that expose Outlook email and calendar summaries."""

    client = client or OutlookClient(OutlookCredentials.from_env())

    def email_summary_tool(_: str = "") -> str:
        return client.previous_day_email_summary()

    def calendar_summary_tool(_: str = "") -> str:
        return client.previous_day_calendar_summary()

    return [
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
    ]


__all__ = [
    "OutlookClient",
    "OutlookCredentials",
    "OutlookIntegrationError",
    "create_outlook_tools",
]
