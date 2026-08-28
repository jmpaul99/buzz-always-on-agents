"""Optional Gmail/Drive/Sheets/Calendar MCP using gcloud Application Default Credentials."""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from typing import Any, Iterator

from google.auth import default
from google.auth.exceptions import DefaultCredentialsError, RefreshError
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaInMemoryUpload
from mcp.server import MCPServer

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.events.freebusy",
]

LOGIN_HINT = (
    "Google login is missing or missing Gmail/Drive/Sheets/Calendar scopes. "
    "On the PC run gcloud auth application-default login with the scopes in "
    "listener/local-mcp/README.md, then infra/create-secrets.ps1 and "
    "infra/deploy-listener.ps1."
)

MAX_TEXT = 200_000
DRIVE_EXPORT = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}

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


_CELL_RE = re.compile(r"^([A-Za-z]+)(\d+)$")


def _json_list(raw: str, label: str) -> list[Any]:
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise ValueError(f"{label} must be a JSON array")
    return parsed


def _col_letters_to_index(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        if ch < "A" or ch > "Z":
            raise ValueError(f"Invalid column letters: {letters}")
        n = n * 26 + (ord(ch) - ord("A") + 1)
    if n == 0:
        raise ValueError("Missing column letters")
    return n - 1


def _parse_a1_cell(cell: str) -> tuple[int, int]:
    match = _CELL_RE.fullmatch(cell.strip())
    if not match:
        raise ValueError(f"Invalid A1 cell: {cell}")
    return int(match.group(2)) - 1, _col_letters_to_index(match.group(1))


def _split_a1_sheet(range_a1: str) -> tuple[str | None, str]:
    raw = range_a1.strip()
    if not raw:
        raise ValueError("Empty A1 range")
    if raw.startswith("'"):
        name_chars: list[str] = []
        i = 1
        while i < len(raw):
            if raw[i] == "'" and i + 1 < len(raw) and raw[i + 1] == "'":
                name_chars.append("'")
                i += 2
                continue
            if raw[i] == "'" and i + 1 < len(raw) and raw[i + 1] == "!":
                return "".join(name_chars), raw[i + 2 :]
            if raw[i] == "'":
                break
            name_chars.append(raw[i])
            i += 1
        raise ValueError(f"Invalid quoted sheet in A1 range: {range_a1}")
    if "!" in raw:
        sheet, cells = raw.split("!", 1)
        return sheet, cells
    return None, raw


def _parse_a1_range(range_a1: str) -> tuple[str | None, int, int, int, int]:
    sheet_name, cells = _split_a1_sheet(range_a1)
    start_raw, sep, end_raw = cells.partition(":")
    start_row, start_col = _parse_a1_cell(start_raw)
    if not sep:
        return sheet_name, start_row, start_row + 1, start_col, start_col + 1
    end_row, end_col = _parse_a1_cell(end_raw)
    if end_row < start_row or end_col < start_col:
        raise ValueError(f"Inverted A1 range: {range_a1}")
    return sheet_name, start_row, end_row + 1, start_col, end_col + 1


def _hex_to_rgb(hex_color: str) -> dict[str, float]:
    raw = hex_color.strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    if len(raw) != 6:
        raise ValueError(f"Invalid hex color: {hex_color}")
    try:
        red, green, blue = bytes.fromhex(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid hex color: {hex_color}") from exc
    return {"red": red / 255, "green": green / 255, "blue": blue / 255}


def _resolve_sheet_id(sheets: list[dict[str, Any]], sheet_name: str | None) -> int:
    if not sheets:
        raise ValueError("Spreadsheet has no sheets")
    if not sheet_name:
        return int(sheets[0]["properties"]["sheetId"])
    for item in sheets:
        props = item.get("properties") or {}
        if props.get("title") == sheet_name:
            return int(props["sheetId"])
    titles = ", ".join(str((item.get("properties") or {}).get("title") or "") for item in sheets)
    raise ValueError(f"Sheet {sheet_name!r} not found. Available: {titles}")


def _spreadsheet_sheets(spreadsheet_id: str) -> list[dict[str, Any]]:
    meta = (
        _service("sheets", "v4")
        .spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets.properties(sheetId,title)")
        .execute()
    )
    return list(meta.get("sheets") or [])


def _split_ids(raw: str) -> list[str]:
    return [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]


def _clamp_text(text: str, limit: int = MAX_TEXT) -> dict[str, Any]:
    if len(text) <= limit:
        return {"content": text, "truncated": False}
    return {"content": text[:limit], "truncated": True, "chars": len(text)}


def _b64url_decode(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _walk_parts(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield payload
    for part in payload.get("parts") or []:
        if isinstance(part, dict):
            yield from _walk_parts(part)


def _gmail_plain(payload: dict[str, Any]) -> str:
    plain: list[str] = []
    html: list[str] = []
    for part in _walk_parts(payload):
        mime = str(part.get("mimeType") or "").lower()
        data = (part.get("body") or {}).get("data")
        if not data:
            continue
        text = _b64url_decode(str(data))
        if mime == "text/plain":
            plain.append(text)
        elif mime == "text/html":
            html.append(text)
    return "\n".join(plain) if plain else "\n".join(html)


def _gmail_headers(payload: dict[str, Any]) -> dict[str, str]:
    return {
        str(header.get("name")): str(header.get("value") or "")
        for header in payload.get("headers") or []
        if isinstance(header, dict)
    }


def _gmail_modify(kind: str, item_id: str, add: list[str], remove: list[str]) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if add:
        body["addLabelIds"] = add
    if remove:
        body["removeLabelIds"] = remove
    if not body:
        raise ValueError("Provide at least one label id")
    resource = _service("gmail", "v1").users()
    target = resource.messages() if kind == "messages" else resource.threads()
    return target.modify(userId="me", id=item_id, body=body).execute()


def _bytes_as_text(raw: bytes) -> tuple[str, bool]:
    try:
        return raw.decode("utf-8"), False
    except UnicodeDecodeError:
        return base64.b64encode(raw).decode("ascii"), True


def _parse_rfc3339(value: str) -> datetime:
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _merge_busy(windows: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    if not windows:
        return []
    ordered = sorted(windows, key=lambda item: item[0])
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _suggest_gaps(
    time_min: datetime,
    time_max: datetime,
    duration: timedelta,
    busy: list[tuple[datetime, datetime]],
    max_suggestions: int = 5,
) -> list[dict[str, str]]:
    suggestions: list[dict[str, str]] = []
    cursor = time_min
    for start, end in _merge_busy(busy) + [(time_max, time_max)]:
        if start - cursor >= duration:
            slot_end = cursor + duration
            if slot_end <= time_max:
                suggestions.append({"start": cursor.isoformat(), "end": slot_end.isoformat()})
                if len(suggestions) >= max_suggestions:
                    return suggestions
        cursor = max(cursor, end)
    return suggestions


def _event_time(value: str, tz_name: str) -> dict[str, str]:
    if "T" not in value:
        return {"date": value}
    payload = {"dateTime": value}
    if tz_name:
        payload["timeZone"] = tz_name
    return payload


def _user_email() -> str:
    info = _service("oauth2", "v2").userinfo().get().execute()
    email = str(info.get("email") or "").strip()
    if not email:
        raise ValueError("Signed-in account has no email")
    return email


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
            payload = message.get("payload") or {}
            headers = _gmail_headers(payload)
            body = _clamp_text(_gmail_plain(payload))
            messages.append(
                {
                    "id": message.get("id"),
                    "from": headers.get("From"),
                    "to": headers.get("To"),
                    "subject": headers.get("Subject"),
                    "date": headers.get("Date"),
                    "snippet": message.get("snippet"),
                    "body": body["content"],
                    "truncated": body["truncated"],
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
def gmail_create_draft(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
    thread_id: str = "",
) -> str:
    """Create a Gmail draft. Does not send. Optional cc, bcc, and thread_id for replies."""
    try:
        message = MIMEText(body)
        message["to"] = to
        message["subject"] = subject
        if cc:
            message["cc"] = cc
        if bcc:
            message["bcc"] = bcc
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        payload: dict[str, Any] = {"message": {"raw": raw}}
        if thread_id:
            payload["message"]["threadId"] = thread_id
        draft = _service("gmail", "v1").users().drafts().create(userId="me", body=payload).execute()
        return json.dumps({"id": draft.get("id"), "message": draft.get("message", {}).get("id")}, indent=2)
    except Exception as exc:
        return _err(exc) if isinstance(exc, HttpError) else str(exc)


@mcp.tool()
def gmail_label_message(message_id: str, label_ids: str) -> str:
    """Add Gmail labels to a message. label_ids is a comma-separated list of label ids."""
    try:
        result = _gmail_modify("messages", message_id, _split_ids(label_ids), [])
        return json.dumps({"id": result.get("id"), "labelIds": result.get("labelIds", [])}, indent=2)
    except Exception as exc:
        return _err(exc) if isinstance(exc, HttpError) else str(exc)


@mcp.tool()
def gmail_unlabel_message(message_id: str, label_ids: str) -> str:
    """Remove Gmail labels from a message. label_ids is a comma-separated list of label ids."""
    try:
        result = _gmail_modify("messages", message_id, [], _split_ids(label_ids))
        return json.dumps({"id": result.get("id"), "labelIds": result.get("labelIds", [])}, indent=2)
    except Exception as exc:
        return _err(exc) if isinstance(exc, HttpError) else str(exc)


@mcp.tool()
def gmail_label_thread(thread_id: str, label_ids: str) -> str:
    """Add Gmail labels to every message in a thread."""
    try:
        result = _gmail_modify("threads", thread_id, _split_ids(label_ids), [])
        return json.dumps({"id": result.get("id")}, indent=2)
    except Exception as exc:
        return _err(exc) if isinstance(exc, HttpError) else str(exc)


@mcp.tool()
def gmail_unlabel_thread(thread_id: str, label_ids: str) -> str:
    """Remove Gmail labels from every message in a thread."""
    try:
        result = _gmail_modify("threads", thread_id, [], _split_ids(label_ids))
        return json.dumps({"id": result.get("id")}, indent=2)
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
def drive_search_files(query: str, max_results: int = 10) -> str:
    """Search Drive files (same as drive_search). Matches Google Drive MCP search_files."""
    return drive_search(query, max_results)


@mcp.tool()
def drive_list_recent_files(max_results: int = 10) -> str:
    """List recently modified Drive files."""
    try:
        result = (
            _service("drive", "v3")
            .files()
            .list(
                q="trashed=false",
                pageSize=max(1, min(max_results, 25)),
                orderBy="modifiedTime desc",
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
def drive_get_file_metadata(file_id: str) -> str:
    """Get Drive file metadata."""
    try:
        result = (
            _service("drive", "v3")
            .files()
            .get(
                fileId=file_id,
                fields="id,name,mimeType,size,createdTime,modifiedTime,webViewLink,parents,owners/emailAddress,md5Checksum",
                supportsAllDrives=True,
            )
            .execute()
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        return _err(exc) if isinstance(exc, HttpError) else str(exc)


@mcp.tool()
def drive_get_file_permissions(file_id: str) -> str:
    """List sharing permissions on a Drive file."""
    try:
        result = (
            _service("drive", "v3")
            .permissions()
            .list(
                fileId=file_id,
                fields="permissions(id,type,role,emailAddress,domain,displayName)",
                supportsAllDrives=True,
            )
            .execute()
        )
        return json.dumps(result.get("permissions", []), indent=2)
    except Exception as exc:
        return _err(exc) if isinstance(exc, HttpError) else str(exc)


@mcp.tool()
def drive_read_file_content(file_id: str) -> str:
    """Read Drive file text. Google Docs/Sheets/Slides are exported; other files are UTF-8 or skipped if binary."""
    try:
        drive = _service("drive", "v3")
        meta = drive.files().get(fileId=file_id, fields="id,name,mimeType", supportsAllDrives=True).execute()
        mime = str(meta.get("mimeType") or "")
        export_mime = DRIVE_EXPORT.get(mime)
        if export_mime:
            raw = drive.files().export(fileId=file_id, mimeType=export_mime).execute()
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
        else:
            raw = drive.files().get_media(fileId=file_id, supportsAllDrives=True).execute()
        text, binary = _bytes_as_text(raw if isinstance(raw, bytes) else bytes(raw))
        if binary:
            return json.dumps(
                {
                    "id": file_id,
                    "name": meta.get("name"),
                    "mimeType": mime,
                    "binary": True,
                    "hint": "Use drive_download_file_content for a base64 body.",
                },
                indent=2,
            )
        clipped = _clamp_text(text)
        return json.dumps(
            {
                "id": file_id,
                "name": meta.get("name"),
                "mimeType": mime,
                "content": clipped["content"],
                "truncated": clipped["truncated"],
            },
            indent=2,
        )
    except Exception as exc:
        return _err(exc) if isinstance(exc, HttpError) else str(exc)


@mcp.tool()
def drive_download_file_content(file_id: str) -> str:
    """Download Drive file bytes as UTF-8 text or base64."""
    try:
        drive = _service("drive", "v3")
        meta = drive.files().get(fileId=file_id, fields="id,name,mimeType", supportsAllDrives=True).execute()
        mime = str(meta.get("mimeType") or "")
        if mime in DRIVE_EXPORT:
            raw = drive.files().export(fileId=file_id, mimeType=DRIVE_EXPORT[mime]).execute()
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
        else:
            raw = drive.files().get_media(fileId=file_id, supportsAllDrives=True).execute()
        blob = raw if isinstance(raw, bytes) else bytes(raw)
        truncated = False
        if len(blob) > MAX_TEXT:
            blob = blob[:MAX_TEXT]
            truncated = True
        text, binary = _bytes_as_text(blob)
        return json.dumps(
            {
                "id": file_id,
                "name": meta.get("name"),
                "mimeType": mime,
                "encoding": "base64" if binary else "utf-8",
                "content": text,
                "truncated": truncated,
            },
            indent=2,
        )
    except Exception as exc:
        return _err(exc) if isinstance(exc, HttpError) else str(exc)


@mcp.tool()
def drive_create_file(name: str, mime_type: str = "text/plain", content: str = "", parent_id: str = "") -> str:
    """Create a Drive file. For Google Docs/Sheets/Slides, omit content and pass the Google mime type."""
    try:
        body: dict[str, Any] = {"name": name, "mimeType": mime_type}
        if parent_id:
            body["parents"] = [parent_id]
        drive = _service("drive", "v3")
        if content and mime_type not in DRIVE_EXPORT:
            media = MediaInMemoryUpload(content.encode("utf-8"), mimetype=mime_type, resumable=False)
            created = drive.files().create(
                body=body,
                media_body=media,
                fields="id,name,mimeType,webViewLink",
                supportsAllDrives=True,
            ).execute()
        else:
            created = drive.files().create(
                body=body,
                fields="id,name,mimeType,webViewLink",
                supportsAllDrives=True,
            ).execute()
        return json.dumps(created, indent=2)
    except Exception as exc:
        return _err(exc) if isinstance(exc, HttpError) else str(exc)


@mcp.tool()
def drive_copy_file(file_id: str, name: str = "", parent_id: str = "") -> str:
    """Copy a Drive file. Optional new name and destination folder id."""
    try:
        body: dict[str, Any] = {}
        if name:
            body["name"] = name
        if parent_id:
            body["parents"] = [parent_id]
        copied = (
            _service("drive", "v3")
            .files()
            .copy(
                fileId=file_id,
                body=body,
                fields="id,name,mimeType,webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )
        return json.dumps(copied, indent=2)
    except Exception as exc:
        return _err(exc) if isinstance(exc, HttpError) else str(exc)


@mcp.tool()
def calendar_list_calendars(max_results: int = 25) -> str:
    """List calendars on the signed-in account."""
    try:
        result = (
            _service("calendar", "v3")
            .calendarList()
            .list(maxResults=max(1, min(max_results, 100)))
            .execute()
        )
        calendars = [
            {
                "id": item.get("id"),
                "summary": item.get("summary"),
                "primary": item.get("primary", False),
                "accessRole": item.get("accessRole"),
                "timeZone": item.get("timeZone"),
            }
            for item in result.get("items", [])
        ]
        return json.dumps(calendars, indent=2)
    except Exception as exc:
        return _err(exc) if isinstance(exc, HttpError) else str(exc)


@mcp.tool()
def calendar_list_events(
    calendar_id: str = "primary",
    time_min: str = "",
    time_max: str = "",
    query: str = "",
    max_results: int = 10,
) -> str:
    """List events. time_min/time_max are RFC3339. query is a free-text search."""
    try:
        kwargs: dict[str, Any] = {
            "calendarId": calendar_id or "primary",
            "maxResults": max(1, min(max_results, 50)),
            "singleEvents": True,
            "orderBy": "startTime",
        }
        if time_min:
            kwargs["timeMin"] = time_min
        if time_max:
            kwargs["timeMax"] = time_max
        if query:
            kwargs["q"] = query
        result = _service("calendar", "v3").events().list(**kwargs).execute()
        events = []
        for item in result.get("items", []):
            events.append(
                {
                    "id": item.get("id"),
                    "summary": item.get("summary"),
                    "status": item.get("status"),
                    "start": item.get("start"),
                    "end": item.get("end"),
                    "location": item.get("location"),
                    "htmlLink": item.get("htmlLink"),
                    "attendees": item.get("attendees", []),
                }
            )
        return json.dumps(events, indent=2)
    except Exception as exc:
        return _err(exc) if isinstance(exc, HttpError) else str(exc)


@mcp.tool()
def calendar_get_event(event_id: str, calendar_id: str = "primary") -> str:
    """Get one calendar event."""
    try:
        event = (
            _service("calendar", "v3")
            .events()
            .get(calendarId=calendar_id or "primary", eventId=event_id)
            .execute()
        )
        return json.dumps(event, indent=2)
    except Exception as exc:
        return _err(exc) if isinstance(exc, HttpError) else str(exc)


@mcp.tool()
def calendar_create_event(
    summary: str,
    start: str,
    end: str,
    calendar_id: str = "primary",
    description: str = "",
    location: str = "",
    attendees_json: str = "",
    timezone: str = "",
) -> str:
    """Create an event. start/end are RFC3339 dateTimes or YYYY-MM-DD all-day dates. attendees_json is ["a@b.com"]."""
    try:
        body: dict[str, Any] = {
            "summary": summary,
            "start": _event_time(start, timezone),
            "end": _event_time(end, timezone),
        }
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if attendees_json:
            emails = json.loads(attendees_json)
            body["attendees"] = [{"email": item} if isinstance(item, str) else item for item in emails]
        created = (
            _service("calendar", "v3")
            .events()
            .insert(calendarId=calendar_id or "primary", body=body, sendUpdates="none")
            .execute()
        )
        return json.dumps(
            {"id": created.get("id"), "htmlLink": created.get("htmlLink"), "status": created.get("status")},
            indent=2,
        )
    except Exception as exc:
        return _err(exc) if isinstance(exc, HttpError) else str(exc)


@mcp.tool()
def calendar_update_event(
    event_id: str,
    calendar_id: str = "primary",
    summary: str = "",
    start: str = "",
    end: str = "",
    description: str = "",
    location: str = "",
    timezone: str = "",
) -> str:
    """Patch event fields. Empty strings are left unchanged."""
    try:
        body: dict[str, Any] = {}
        if summary:
            body["summary"] = summary
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if start:
            body["start"] = _event_time(start, timezone)
        if end:
            body["end"] = _event_time(end, timezone)
        if not body:
            raise ValueError("Provide at least one field to update")
        updated = (
            _service("calendar", "v3")
            .events()
            .patch(calendarId=calendar_id or "primary", eventId=event_id, body=body, sendUpdates="none")
            .execute()
        )
        return json.dumps(
            {"id": updated.get("id"), "htmlLink": updated.get("htmlLink"), "status": updated.get("status")},
            indent=2,
        )
    except Exception as exc:
        return _err(exc) if isinstance(exc, HttpError) else str(exc)


@mcp.tool()
def calendar_delete_event(event_id: str, calendar_id: str = "primary") -> str:
    """Delete a calendar event."""
    try:
        _service("calendar", "v3").events().delete(
            calendarId=calendar_id or "primary", eventId=event_id, sendUpdates="none"
        ).execute()
        return json.dumps({"deleted": True, "id": event_id, "calendarId": calendar_id or "primary"}, indent=2)
    except Exception as exc:
        return _err(exc) if isinstance(exc, HttpError) else str(exc)


@mcp.tool()
def calendar_respond_to_event(event_id: str, response: str, calendar_id: str = "primary") -> str:
    """Respond to an invitation: accepted, declined, or tentative."""
    try:
        status = response.strip().lower()
        if status not in {"accepted", "declined", "tentative"}:
            raise ValueError("response must be accepted, declined, or tentative")
        calendar = _service("calendar", "v3")
        cal_id = calendar_id or "primary"
        event = calendar.events().get(calendarId=cal_id, eventId=event_id).execute()
        email = _user_email().lower()
        attendees = list(event.get("attendees") or [])
        found = False
        for attendee in attendees:
            if str(attendee.get("email") or "").lower() == email:
                attendee["responseStatus"] = status
                found = True
                break
        if not found:
            attendees.append({"email": email, "responseStatus": status})
        updated = calendar.events().patch(
            calendarId=cal_id, eventId=event_id, body={"attendees": attendees}, sendUpdates="none"
        ).execute()
        return json.dumps({"id": updated.get("id"), "response": status}, indent=2)
    except Exception as exc:
        return _err(exc) if isinstance(exc, HttpError) else str(exc)


@mcp.tool()
def calendar_suggest_time(
    time_min: str,
    time_max: str,
    duration_minutes: int = 30,
    calendar_ids: str = "primary",
    max_suggestions: int = 5,
) -> str:
    """Suggest free slots between time_min and time_max (RFC3339). calendar_ids is comma-separated."""
    try:
        start = _parse_rfc3339(time_min)
        end = _parse_rfc3339(time_max)
        if end <= start:
            raise ValueError("time_max must be after time_min")
        duration = timedelta(minutes=max(1, duration_minutes))
        ids = _split_ids(calendar_ids) or ["primary"]
        result = (
            _service("calendar", "v3")
            .freebusy()
            .query(
                body={
                    "timeMin": start.isoformat(),
                    "timeMax": end.isoformat(),
                    "items": [{"id": cal_id} for cal_id in ids],
                }
            )
            .execute()
        )
        busy: list[tuple[datetime, datetime]] = []
        for cal in (result.get("calendars") or {}).values():
            for window in cal.get("busy") or []:
                busy.append((_parse_rfc3339(str(window["start"])), _parse_rfc3339(str(window["end"]))))
        suggestions = _suggest_gaps(start, end, duration, busy, max(1, min(max_suggestions, 10)))
        return json.dumps({"durationMinutes": duration_minutes, "suggestions": suggestions}, indent=2)
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


@mcp.tool()
def sheets_list_spreadsheets(max_results: int = 25) -> str:
    """List Google Sheets files the signed-in account can see."""
    try:
        result = (
            _service("drive", "v3")
            .files()
            .list(
                q="mimeType='application/vnd.google-apps.spreadsheet' and trashed=false",
                pageSize=max(1, min(max_results, 100)),
                fields="files(id,name,modifiedTime,webViewLink)",
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
            )
            .execute()
        )
        return json.dumps(result.get("files", []), indent=2)
    except Exception as exc:
        return _err(exc) if isinstance(exc, HttpError) else str(exc)


@mcp.tool()
def sheets_create_spreadsheet(title: str) -> str:
    """Create a new Google spreadsheet with the given title."""
    try:
        created = (
            _service("sheets", "v4")
            .spreadsheets()
            .create(
                body={"properties": {"title": title}},
                fields="spreadsheetId,spreadsheetUrl,properties.title",
            )
            .execute()
        )
        return json.dumps(
            {
                "title": (created.get("properties") or {}).get("title", title),
                "spreadsheetId": created.get("spreadsheetId"),
                "spreadsheetUrl": created.get("spreadsheetUrl"),
            },
            indent=2,
        )
    except Exception as exc:
        return _err(exc) if isinstance(exc, HttpError) else str(exc)


@mcp.tool()
def sheets_copy_sheet(
    source_spreadsheet_id: str,
    destination_spreadsheet_id: str,
    source_sheet_name: str = "",
) -> str:
    """Copy a tab into another spreadsheet. Empty source_sheet_name copies the first tab."""
    try:
        sheets = _spreadsheet_sheets(source_spreadsheet_id)
        sheet_id = _resolve_sheet_id(sheets, source_sheet_name or None)
        copied = (
            _service("sheets", "v4")
            .spreadsheets()
            .sheets()
            .copyTo(
                spreadsheetId=source_spreadsheet_id,
                sheetId=sheet_id,
                body={"destinationSpreadsheetId": destination_spreadsheet_id},
            )
            .execute()
        )
        return json.dumps(
            {
                "sourceSpreadsheetId": source_spreadsheet_id,
                "destinationSpreadsheetId": destination_spreadsheet_id,
                "sourceSheetId": sheet_id,
                "copiedTitle": copied.get("title"),
                "copiedSheetId": copied.get("sheetId"),
            },
            indent=2,
        )
    except Exception as exc:
        return _err(exc) if isinstance(exc, HttpError) else str(exc)


@mcp.tool()
def sheets_batch_update_values(spreadsheet_id: str, ranges_json: str) -> str:
    """Write multiple ranges in one call. ranges_json is [{"range":"Sheet1!A1:B2","values":[["A","B"],["1","2"]]}, ...]."""
    try:
        data = []
        for item in _json_list(ranges_json, "ranges_json"):
            if not isinstance(item, dict) or "range" not in item or "values" not in item:
                raise ValueError('Each ranges_json item must have "range" and "values"')
            data.append({"range": item["range"], "values": item["values"]})
        result = (
            _service("sheets", "v4")
            .spreadsheets()
            .values()
            .batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"valueInputOption": "USER_ENTERED", "data": data},
            )
            .execute()
        )
        return json.dumps(result, indent=2)
    except Exception as exc:
        return _err(exc) if isinstance(exc, HttpError) else str(exc)


@mcp.tool()
def sheets_fill_colors(spreadsheet_id: str, ranges_json: str) -> str:
    """Fill cell backgrounds. ranges_json is [{"range":"Sheet1!A1:B2","color":"#FF5733"}, ...]."""
    try:
        sheets = _spreadsheet_sheets(spreadsheet_id)
        requests: list[dict[str, Any]] = []
        for item in _json_list(ranges_json, "ranges_json"):
            if not isinstance(item, dict) or "range" not in item or "color" not in item:
                raise ValueError('Each ranges_json item must have "range" and "color" (hex)')
            sheet_name, start_row, end_row, start_col, end_col = _parse_a1_range(str(item["range"]))
            requests.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": _resolve_sheet_id(sheets, sheet_name),
                            "startRowIndex": start_row,
                            "endRowIndex": end_row,
                            "startColumnIndex": start_col,
                            "endColumnIndex": end_col,
                        },
                        "cell": {"userEnteredFormat": {"backgroundColor": _hex_to_rgb(str(item["color"]))}},
                        "fields": "userEnteredFormat.backgroundColor",
                    }
                }
            )
        result = (
            _service("sheets", "v4")
            .spreadsheets()
            .batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests})
            .execute()
        )
        return json.dumps({"updatedRanges": len(requests), "replies": result.get("replies", [])}, indent=2)
    except Exception as exc:
        return _err(exc) if isinstance(exc, HttpError) else str(exc)


SUITES = ("gmail", "drive", "calendar", "sheets")


def _tool_bags(server: Any) -> list[dict[str, Any]]:
    bags: list[dict[str, Any]] = []
    for attr in ("_tools", "tools"):
        val = getattr(server, attr, None)
        if isinstance(val, dict):
            bags.append(val)
    manager = getattr(server, "_tool_manager", None)
    if manager is not None:
        val = getattr(manager, "_tools", None)
        if isinstance(val, dict):
            bags.append(val)
    return bags


def apply_suite(suite: str, server: Any | None = None) -> int:
    """Keep one product suite of tools; return how many were removed."""
    name = (suite or "gmail").strip().lower()
    if name not in SUITES:
        raise ValueError(f"suite must be one of {SUITES}")
    prefix = name + "_"
    removed = 0
    for bag in _tool_bags(server if server is not None else mcp):
        for tool_name in list(bag):
            if not str(tool_name).startswith(prefix):
                bag.pop(tool_name, None)
                removed += 1
    return removed


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", default="gmail", choices=SUITES)
    ns = parser.parse_args()
    apply_suite(ns.suite)
    mcp.run()
