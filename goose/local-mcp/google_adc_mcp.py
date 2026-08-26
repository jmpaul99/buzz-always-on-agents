"""Goose-local Gmail/Drive/Sheets MCP using gcloud Application Default Credentials."""

from __future__ import annotations

import json
from typing import Any

from google.auth import default
from google.auth.exceptions import DefaultCredentialsError, RefreshError
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from mcp.server import MCPServer

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]

LOGIN_HINT = (
    "Google login is missing or missing Gmail/Drive/Sheets scopes. "
    "On the PC run: powershell -File $env:APPDATA\\Block\\goose\\local-mcp\\login-google.ps1 "
    "then infra/create-secrets.ps1 (uploads ADC) and redeploy the Goose worker."
)

mcp = MCPServer("google-adc")


def _credentials():
    try:
        credentials, _project = default(scopes=SCOPES)
    except DefaultCredentialsError as exc:
        raise RuntimeError(LOGIN_HINT) from exc
    if not credentials.valid:
        try:
            credentials.refresh(Request())
        except RefreshError as exc:
            raise RuntimeError(f"{LOGIN_HINT} Refresh failed: {exc}") from exc
    return credentials


def _service(name: str, version: str):
    return build(name, version, credentials=_credentials(), cache_discovery=False)


def _err(exc: Exception) -> str:
    if isinstance(exc, HttpError):
        return f"Google API error {exc.status_code}: {exc.reason}"
    return str(exc)


@mcp.tool()
def google_whoami() -> str:
    """Show the signed-in Google account used by gcloud ADC."""
    try:
        info = _service("oauth2", "v2").userinfo().get().execute()
        return json.dumps(
            {"email": info.get("email"), "name": info.get("name"), "id": info.get("id")},
            indent=2,
        )
    except Exception as exc:
        return _err(exc) if isinstance(exc, HttpError) else str(exc)


@mcp.tool()
def gmail_list_labels() -> str:
    """List Gmail labels for the signed-in account."""
    try:
        result = _service("gmail", "v1").users().labels().list(userId="me").execute()
        labels = [
            {"id": item.get("id"), "name": item.get("name"), "type": item.get("type")}
            for item in result.get("labels", [])
        ]
        return json.dumps(labels, indent=2)
    except Exception as exc:
        return _err(exc) if isinstance(exc, HttpError) else str(exc)


@mcp.tool()
def gmail_search_threads(query: str, max_results: int = 10) -> str:
    """Search Gmail threads with a Gmail search query, e.g. is:unread newer_than:7d."""
    try:
        gmail = _service("gmail", "v1")
        listed = (
            gmail.users()
            .threads()
            .list(userId="me", q=query, maxResults=max(1, min(max_results, 25)))
            .execute()
        )
        threads = []
        for item in listed.get("threads", []):
            thread = (
                gmail.users()
                .threads()
                .get(userId="me", id=item["id"], format="metadata", metadataHeaders=["From", "To", "Subject", "Date"])
                .execute()
            )
            headers = {
                header["name"]: header["value"]
                for header in thread.get("messages", [{}])[0].get("payload", {}).get("headers", [])
            }
            threads.append(
                {
                    "id": item["id"],
                    "snippet": thread.get("messages", [{}])[0].get("snippet", item.get("snippet", "")),
                    "from": headers.get("From"),
                    "subject": headers.get("Subject"),
                    "date": headers.get("Date"),
                }
            )
        return json.dumps(threads, indent=2)
    except Exception as exc:
        return _err(exc) if isinstance(exc, HttpError) else str(exc)


@mcp.tool()
def gmail_get_thread(thread_id: str) -> str:
    """Get a Gmail thread's messages as plain text."""
    try:
        thread = (
            _service("gmail", "v1")
            .users()
            .threads()
            .get(userId="me", id=thread_id, format="full")
            .execute()
        )
        messages = []
        for message in thread.get("messages", []):
            headers = {
                header["name"]: header["value"]
                for header in message.get("payload", {}).get("headers", [])
            }
            messages.append(
                {
                    "id": message.get("id"),
                    "from": headers.get("From"),
                    "to": headers.get("To"),
                    "subject": headers.get("Subject"),
                    "date": headers.get("Date"),
                    "snippet": message.get("snippet"),
                }
            )
        return json.dumps({"id": thread_id, "messages": messages}, indent=2)
    except Exception as exc:
        return _err(exc) if isinstance(exc, HttpError) else str(exc)


@mcp.tool()
def gmail_list_drafts(max_results: int = 10) -> str:
    """List Gmail drafts."""
    try:
        result = (
            _service("gmail", "v1")
            .users()
            .drafts()
            .list(userId="me", maxResults=max(1, min(max_results, 25)))
            .execute()
        )
        return json.dumps(result.get("drafts", []), indent=2)
    except Exception as exc:
        return _err(exc) if isinstance(exc, HttpError) else str(exc)


@mcp.tool()
def gmail_create_draft(to: str, subject: str, body: str) -> str:
    """Create a Gmail draft. Does not send."""
    try:
        import base64
        from email.mime.text import MIMEText

        message = MIMEText(body)
        message["to"] = to
        message["subject"] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        draft = (
            _service("gmail", "v1")
            .users()
            .drafts()
            .create(userId="me", body={"message": {"raw": raw}})
            .execute()
        )
        return json.dumps({"id": draft.get("id"), "message": draft.get("message", {}).get("id")}, indent=2)
    except Exception as exc:
        return _err(exc) if isinstance(exc, HttpError) else str(exc)


@mcp.tool()
def drive_search(query: str, max_results: int = 10) -> str:
    """Search Google Drive files. Query uses Drive syntax, e.g. name contains 'budget'."""
    try:
        result = (
            _service("drive", "v3")
            .files()
            .list(
                q=query,
                pageSize=max(1, min(max_results, 25)),
                fields="files(id,name,mimeType,modifiedTime,webViewLink,owners/emailAddress)",
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
            )
            .execute()
        )
        return json.dumps(result.get("files", []), indent=2)
    except Exception as exc:
        return _err(exc) if isinstance(exc, HttpError) else str(exc)


@mcp.tool()
def sheets_get_values(spreadsheet_id: str, range_a1: str) -> str:
    """Read a Google Sheets range, e.g. Sheet1!A1:D20."""
    try:
        result = (
            _service("sheets", "v4")
            .spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=range_a1)
            .execute()
        )
        return json.dumps({"range": result.get("range"), "values": result.get("values", [])}, indent=2)
    except Exception as exc:
        return _err(exc) if isinstance(exc, HttpError) else str(exc)


@mcp.tool()
def sheets_update_values(spreadsheet_id: str, range_a1: str, values_json: str) -> str:
    """Write a Google Sheets range. values_json is a JSON array of rows, e.g. [["A","B"],["1","2"]]."""
    try:
        values: list[list[Any]] = json.loads(values_json)
        result = (
            _service("sheets", "v4")
            .spreadsheets()
            .values()
            .update(
                spreadsheetId=spreadsheet_id,
                range=range_a1,
                valueInputOption="USER_ENTERED",
                body={"values": values},
            )
            .execute()
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        return _err(exc) if isinstance(exc, HttpError) else str(exc)


if __name__ == "__main__":
    mcp.run()
