"""Google Drive integrations for Deep Agent examples.

This module handles authentication via OAuth 2.0 credentials or Google Cloud
service accounts and exposes helpers that fetch Drive metadata for a given time
window.  The helpers are wrapped as LangChain ``Tool`` objects so that agents
can reason over recent Drive activity.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Dict, Iterable, List, Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.credentials import Credentials
from google.auth.transport.requests import Request
from google.oauth2 import credentials as oauth_credentials
from google.oauth2 import service_account
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, validator

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.metadata.readonly"]


logger = logging.getLogger(__name__)


class GoogleDriveIntegrationError(RuntimeError):
    """Raised when the Google Drive integration is not correctly configured."""


@dataclass
class DriveCredentials:
    """Wrapper class around google-auth credentials."""

    credentials: Credentials

    @classmethod
    def from_env(cls) -> "DriveCredentials":
        """Load credentials from environment variables.

        The loader supports two strategies:

        1. **Service account impersonation** – Provide a JSON file path in the
           ``GOOGLE_APPLICATION_CREDENTIALS`` environment variable. Optional
           domain-wide delegation is enabled by setting
           ``GOOGLE_DRIVE_DELEGATED_USER`` to the primary email address that the
           service account should impersonate.
        2. **OAuth user credentials** – Provide an authorized user JSON payload
           via ``GOOGLE_DRIVE_TOKEN_JSON`` or a file path via
           ``GOOGLE_DRIVE_TOKEN_PATH``. The payload must include a refresh token
           so it can be refreshed automatically.
        """

        service_account_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        delegated_user = os.getenv("GOOGLE_DRIVE_DELEGATED_USER")
        token_json = os.getenv("GOOGLE_DRIVE_TOKEN_JSON")
        token_path = os.getenv("GOOGLE_DRIVE_TOKEN_PATH")

        if service_account_path:
            if not os.path.exists(service_account_path):
                raise GoogleDriveIntegrationError(
                    "GOOGLE_APPLICATION_CREDENTIALS points to a missing file."
                )
            logger.debug("Loading Google service account credentials from %s", service_account_path)
            credentials = service_account.Credentials.from_service_account_file(
                service_account_path,
                scopes=DRIVE_SCOPES,
            )
            if delegated_user:
                credentials = credentials.with_subject(delegated_user)
            return cls(credentials=credentials)

        info: Optional[Dict[str, str]] = None
        if token_json:
            try:
                info = json.loads(token_json)
            except json.JSONDecodeError as exc:  # pragma: no cover - configuration guardrail
                raise GoogleDriveIntegrationError(
                    "GOOGLE_DRIVE_TOKEN_JSON contains invalid JSON."
                ) from exc
        elif token_path:
            if not os.path.exists(token_path):
                raise GoogleDriveIntegrationError(
                    "GOOGLE_DRIVE_TOKEN_PATH points to a missing file."
                )
            with open(token_path, "r", encoding="utf-8") as handle:
                info = json.load(handle)

        if info:
            logger.debug("Loading Google OAuth user credentials from provided token info")
            credentials = oauth_credentials.Credentials.from_authorized_user_info(
                info,
                scopes=DRIVE_SCOPES,
            )
            return cls(credentials=credentials)

        raise GoogleDriveIntegrationError(
            "Google Drive credentials not configured. Set GOOGLE_APPLICATION_CREDENTIALS "
            "or provide OAuth token info via GOOGLE_DRIVE_TOKEN_JSON/GOOGLE_DRIVE_TOKEN_PATH."
        )


class GoogleDriveClient:
    """Small wrapper around the Google Drive API used by the example tools."""

    def __init__(self, credentials: DriveCredentials):
        self._credentials = credentials.credentials
        self._service = None

    def _ensure_credentials(self) -> Credentials:
        if not self._credentials.valid:
            if self._credentials.expired and getattr(self._credentials, "refresh_token", None):
                logger.debug("Refreshing Google credentials")
                self._credentials.refresh(Request())
            else:
                raise GoogleDriveIntegrationError(
                    "Google credentials are invalid and cannot be refreshed automatically."
                )
        return self._credentials

    def _service_client(self):
        if self._service is None:
            credentials = self._ensure_credentials()
            self._service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        return self._service

    @staticmethod
    def _to_utc_string(dt: datetime) -> str:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def list_files_modified_between(
        self,
        *,
        start_time: datetime,
        end_time: datetime,
        page_size: int = 25,
    ) -> List[Dict[str, str]]:
        """Return Drive files whose ``modifiedTime`` falls within the window."""

        if start_time > end_time:
            raise GoogleDriveIntegrationError("start_time must be before end_time")

        query = [
            f"modifiedTime >= '{self._to_utc_string(start_time)}'",
            f"modifiedTime <= '{self._to_utc_string(end_time)}'",
        ]
        params = {
            "q": " and ".join(query),
            "orderBy": "modifiedTime desc",
            "pageSize": min(max(page_size, 1), 100),
            "fields": "files(id, name, mimeType, owners(displayName, emailAddress), modifiedTime, webViewLink)",
        }
        logger.info(
            "Listing Google Drive files modified between %s and %s (limit=%s)",
            params["q"],
            end_time.isoformat(),
            params["pageSize"],
        )
        try:
            service = self._service_client()
            response = service.files().list(**params).execute()
        except HttpError as exc:  # pragma: no cover - network guardrail
            raise GoogleDriveIntegrationError(f"Drive API request failed: {exc}") from exc
        return response.get("files", [])

    def summarize_files(self, files: Iterable[Dict[str, str]]) -> str:
        lines = ["Recent Google Drive activity:"]
        for item in files:
            modified = item.get("modifiedTime")
            timestamp = "unknown"
            if modified:
                timestamp = modified
            owner_info = item.get("owners") or []
            owner_names = ", ".join(
                owner.get("displayName") or owner.get("emailAddress") or "Unknown"
                for owner in owner_info
            ) or "Unknown owner"
            lines.append(
                f"- {item.get('name', 'Untitled file')} (ID: {item.get('id')}) "
                f"[{item.get('mimeType', 'unknown type')}] modified {timestamp} by {owner_names}."
            )
        if len(lines) == 1:
            lines.append("- No file changes detected in the selected window.")
        return "\n".join(lines)

    def get_file_metadata(self, file_id: str) -> Dict[str, str]:
        """Fetch metadata for a single Drive file."""

        if not file_id:
            raise GoogleDriveIntegrationError("file_id is required to fetch metadata")
        try:
            service = self._service_client()
            response = (
                service.files()
                .get(
                    fileId=file_id,
                    fields=(
                        "id, name, mimeType, modifiedTime, createdTime, size, "
                        "owners(displayName, emailAddress), webViewLink, iconLink, description"
                    ),
                )
                .execute()
            )
        except HttpError as exc:  # pragma: no cover - network guardrail
            raise GoogleDriveIntegrationError(f"Drive API metadata request failed: {exc}") from exc
        return response

    @staticmethod
    def format_metadata(metadata: Dict[str, str]) -> str:
        lines = ["Google Drive file metadata:"]
        for key in [
            "name",
            "id",
            "mimeType",
            "modifiedTime",
            "createdTime",
            "size",
            "webViewLink",
            "iconLink",
            "description",
        ]:
            value = metadata.get(key)
            if value:
                lines.append(f"- {key}: {value}")
        owners = metadata.get("owners")
        if owners:
            owner_lines = []
            for owner in owners:
                display = owner.get("displayName") or owner.get("emailAddress")
                email = owner.get("emailAddress")
                if display and email:
                    owner_lines.append(f"{display} <{email}>")
                elif display:
                    owner_lines.append(display)
                elif email:
                    owner_lines.append(email)
            if owner_lines:
                lines.append("- owners: " + ", ".join(owner_lines))
        if len(lines) == 1:
            lines.append("- No metadata available.")
        return "\n".join(lines)


def create_google_drive_tools(client: Optional[GoogleDriveClient] = None) -> List[StructuredTool]:
    """Create LangChain tools that expose Google Drive metadata."""

    client = client or GoogleDriveClient(DriveCredentials.from_env())

    class ListDriveFilesInput(BaseModel):
        """Schema for listing Drive files within a time window."""

        start_time: datetime = Field(..., description="Window start timestamp in ISO 8601 format.")
        end_time: datetime = Field(..., description="Window end timestamp in ISO 8601 format.")
        page_size: int = Field(
            default=25,
            ge=1,
            le=100,
            description="Maximum number of files to return (1-100).",
        )

        @validator("end_time")
        def _ensure_timezone(cls, value: datetime):  # type: ignore[override]
            if value.tzinfo is None:
                return value.replace(tzinfo=UTC)
            return value

        @validator("start_time")
        def _ensure_start_timezone(cls, value: datetime):  # type: ignore[override]
            if value.tzinfo is None:
                return value.replace(tzinfo=UTC)
            return value

    class DriveFileMetadataInput(BaseModel):
        """Schema for fetching metadata about a Drive file."""

        file_id: str = Field(..., description="Google Drive file identifier.")

    def list_recent_files(start_time: datetime, end_time: datetime, page_size: int = 25) -> str:
        files = client.list_files_modified_between(
            start_time=start_time,
            end_time=end_time,
            page_size=page_size,
        )
        return client.summarize_files(files)

    def get_file_metadata(file_id: str) -> str:
        metadata = client.get_file_metadata(file_id)
        return client.format_metadata(metadata)

    return [
        StructuredTool.from_function(
            list_recent_files,
            name="list_recent_drive_files",
            description=(
                "List Google Drive files modified within a specific time window. "
                "Provide ISO 8601 timestamps to bound the query."
            ),
            args_schema=ListDriveFilesInput,
        ),
        StructuredTool.from_function(
            get_file_metadata,
            name="get_drive_file_metadata",
            description="Fetch detailed metadata for a Google Drive file using its ID.",
            args_schema=DriveFileMetadataInput,
        ),
    ]
