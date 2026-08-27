# Google Workspace MCP (ADC)

Optional stdio MCP for Gmail, Drive, Calendar, and Sheets using **gcloud Application Default Credentials**. Listed in [`mcp-catalog.json`](../mcp-catalog.json) as extra `googleadc` (`enabled: false`). Cloud agents enable it with `mcp_enable` on the multiplexer ([`mcp_manager.py`](mcp_manager.py)); do not turn it on by default on an e2-micro.

Gmail, Drive, and Calendar tools match Google's remote Workspace MCP servers (the same endpoints the [Gmail](https://github.com/cursor/plugins/tree/main/third_party/gmail), [Drive](https://github.com/cursor/plugins/tree/main/third_party/google-drive), and [Calendar](https://github.com/cursor/plugins/tree/main/third_party/google-calendar) Cursor plugins wrap). This process uses ADC instead of those HTTP MCP URLs because the listener VM has no browser for Google's OAuth prompt.

`infra/deploy-listener.ps1` copies [`google_adc_mcp.py`](google_adc_mcp.py) to `/opt/buzz/local-mcp/google_adc_mcp.py`. Spawn: `uv run --with mcp --with google-api-python-client --with google-auth --with google-auth-httplib2 python /opt/buzz/local-mcp/google_adc_mcp.py`.

## Credentials

`google.auth.default(scopes=…)` plus refresh. Listener deploy copies Secret Manager `gcloud-adc` to `/etc/buzz/_adc.json` when that secret exists (`GOOGLE_APPLICATION_CREDENTIALS`). `GOOGLE_CLOUD_PROJECT` is also required.

Scopes (must be on the ADC client — default ADC from `deploy.ps1` does **not** include Gmail / Drive / Sheets / Calendar):

- `openid`, `userinfo.email`, `cloud-platform`
- Gmail: `gmail.readonly`, `gmail.compose`, `gmail.modify`
- Drive: `drive.readonly`, `drive.file`
- Sheets: `spreadsheets.readonly`, `spreadsheets`
- Calendar: `calendar.calendarlist.readonly`, `calendar.events`, `calendar.events.freebusy`

## Login (PC, then upload)

There is no browser on the micro. Authenticate on the Windows machine that runs `infra/create-secrets.ps1`, then redeploy the listener.

**Do not** use bare `gcloud auth application-default login --scopes=…gmail…`. Google's built-in gcloud OAuth client is not allowed to request Gmail / Drive / Calendar; the consent page returns **This app is blocked**. Use a Desktop OAuth client from **this** GCP project instead ([ADC + extra scopes](https://docs.cloud.google.com/docs/authentication/troubleshoot-adc#access-blocked-when-using-scopes)).

1. OAuth consent screen (one-time) in [Google Auth Platform](https://console.cloud.google.com/auth/overview):

   - User type **External** if the account is personal `@gmail.com` (Internal is Workspace-only).
   - Publishing status **Testing**.
   - Add your Google account under **Audience → Test users**.
   - **Data Access → Add or remove scopes**, then add every scope listed above.

2. Create a **Desktop app** OAuth client under [Credentials](https://console.cloud.google.com/auth/clients). Download the JSON outside the repo (for example `%USERPROFILE%\buzz-adc-client.json`). Do not commit it.

3. Re-login ADC with **that** client and **every** scope (`--scopes` replaces the default set):

   ```powershell
   gcloud auth application-default login --client-id-file="$env:USERPROFILE\buzz-adc-client.json" --scopes="openid,https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/gmail.readonly,https://www.googleapis.com/auth/gmail.compose,https://www.googleapis.com/auth/gmail.modify,https://www.googleapis.com/auth/drive.readonly,https://www.googleapis.com/auth/drive.file,https://www.googleapis.com/auth/spreadsheets.readonly,https://www.googleapis.com/auth/spreadsheets,https://www.googleapis.com/auth/calendar.calendarlist.readonly,https://www.googleapis.com/auth/calendar.events,https://www.googleapis.com/auth/calendar.events.freebusy"
   ```

   If you see **Google hasn't verified this app**, that is expected in Testing: **Advanced → Go to \<app\> (unsafe)**. Leave every extra checkbox checked.

4. Upload the user ADC file and redeploy:

   ```powershell
   .\infra\create-secrets.ps1
   .\infra\deploy-listener.ps1
   ```

   `create-secrets.ps1` upserts `%APPDATA%\gcloud\application_default_credentials.json` as secret `gcloud-adc` (skipped if the file is missing).

## Gmail tools

Match [Gmail MCP](https://developers.google.com/workspace/gmail/api/guides/configure-mcp-server): `search_threads`, `get_thread`, `list_labels`, `list_drafts`, `create_draft`, `label_message`, `label_thread`, `unlabel_message`, `unlabel_thread`.

| Tool | What it does |
|---|---|
| `gmail_list_labels` | Label id, name, type |
| `gmail_search_threads` | Gmail query (`is:unread newer_than:7d`); max 25 |
| `gmail_get_thread` | Thread messages including plain-text body |
| `gmail_list_drafts` | Draft ids |
| `gmail_create_draft` | Create a draft only — does **not** send. Optional `cc`, `bcc`, `thread_id` |
| `gmail_label_message` / `gmail_unlabel_message` | Add or remove labels on one message |
| `gmail_label_thread` / `gmail_unlabel_thread` | Add or remove labels on a thread |

## Drive tools

Match [Drive MCP](https://developers.google.com/workspace/drive/api/guides/configure-mcp-server): `search_files`, `list_recent_files`, `get_file_metadata`, `get_file_permissions`, `read_file_content`, `download_file_content`, `create_file`, `copy_file`.

| Tool | What it does |
|---|---|
| `drive_search` / `drive_search_files` | Drive `q` syntax; all-drives |
| `drive_list_recent_files` | Recently modified files |
| `drive_get_file_metadata` | Name, mime, times, link |
| `drive_get_file_permissions` | Sharing ACLs |
| `drive_read_file_content` | Text / exported Docs-Sheets-Slides |
| `drive_download_file_content` | UTF-8 or base64 body |
| `drive_create_file` | Create a file or Google Doc/Sheet/Slide |
| `drive_copy_file` | Copy a file |

## Calendar tools

Match [Calendar MCP](https://developers.google.com/workspace/calendar/api/guides/configure-mcp-server): `list_calendars`, `list_events`, `get_event`, `create_event`, `update_event`, `delete_event`, `respond_to_event`, `suggest_time`.

| Tool | What it does |
|---|---|
| `calendar_list_calendars` | Calendars on the account |
| `calendar_list_events` | Events in a window; optional free-text `query` |
| `calendar_get_event` | One event |
| `calendar_create_event` | Create; `attendees_json` is `["a@b.com"]` |
| `calendar_update_event` | Patch summary/times/location/description |
| `calendar_delete_event` | Delete |
| `calendar_respond_to_event` | `accepted` / `declined` / `tentative` |
| `calendar_suggest_time` | Free/busy gaps between `time_min` and `time_max` |

## Sheets tools

| Tool | What it does |
|---|---|
| `sheets_list_spreadsheets` | Drive list of spreadsheet files |
| `sheets_create_spreadsheet` | Create a workbook by title |
| `sheets_copy_sheet` | Copy a tab into another spreadsheet (first tab if `source_sheet_name` is empty) |
| `sheets_get_values` | Read an A1 range |
| `sheets_update_values` | Write one A1 range; `values_json` is a JSON array of rows |
| `sheets_batch_update_values` | Write several ranges in one call |
| `sheets_fill_colors` | Set cell background colors from hex |

Keep `googleadc` disabled in the committed catalog. Enable it per agent with `mcp_enable` (RAM + Node/uv).
