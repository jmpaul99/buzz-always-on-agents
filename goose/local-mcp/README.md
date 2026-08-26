# Google Workspace MCP (ADC)

Goose-local stdio MCP for Gmail, Drive, and Sheets using **gcloud Application Default Credentials**. Wired from [`goose/config.yaml`](../config.yaml) as extension `googleadc` (`enabled: false` until a recipe or Extension Manager turns it on).

In the worker image this file is `/opt/buzz/local-mcp/google_adc_mcp.py`. Goose launches it with `uv run --with mcp --with google-api-python-client --with google-auth --with google-auth-httplib2`.

## Credentials

`google.auth.default(scopes=…)` plus refresh. Cloud Run mounts Secret Manager `gcloud-adc` at `/secrets/adc.json` when that secret exists (`GOOGLE_APPLICATION_CREDENTIALS`). `GOOGLE_CLOUD_PROJECT` is also required.

Scopes (must be on the ADC client — default ADC from `deploy.ps1` does **not** include Gmail / Drive / Sheets):

- `openid`, `userinfo.email`, `cloud-platform`
- Gmail: `gmail.readonly`, `gmail.compose`
- Drive: `drive.readonly`, `drive.file`
- Sheets: `spreadsheets.readonly`, `spreadsheets`

## Login (PC, then upload)

Cloud Goose cannot open a Google consent screen. **Do not use Playwright for Google login.** Authenticate on the Windows machine that runs `infra/create-secrets.ps1`, then redeploy the worker.

1. Re-login ADC **with every scope above** (`--scopes` replaces the default set; omitting any of them drops that API):

   ```powershell
   gcloud auth application-default login --scopes="openid,https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/gmail.readonly,https://www.googleapis.com/auth/gmail.compose,https://www.googleapis.com/auth/drive.readonly,https://www.googleapis.com/auth/drive.file,https://www.googleapis.com/auth/spreadsheets.readonly,https://www.googleapis.com/auth/spreadsheets"
   ```

   If Desktop Goose is installed, `$env:APPDATA\Block\goose\local-mcp\login-google.ps1` is an equivalent (that is the path the MCP error hint names).

2. Upload the user ADC file and redeploy so Cloud Run remounts it:

   ```powershell
   .\infra\create-secrets.ps1
   .\infra\deploy-goose-job.ps1
   ```

   `create-secrets.ps1` upserts `%APPDATA%\gcloud\application_default_credentials.json` as secret `gcloud-adc` (skipped if the file is missing). Other extension keys in `.env` are unchanged.

3. Confirm from a mention that enables `googleadc` (or a Google recipe): `google_whoami` should return the signed-in email.

Missing or expired ADC (or a login that lacked Workspace scopes) returns that same re-login hint instead of crashing Goose. After a password change, revoked consent, or `gcloud auth application-default revoke`, repeat steps 1–2.

## Tools

| Tool | What it does |
| --- | --- |
| `google_whoami` | Signed-in email / name / id |
| `gmail_list_labels` | Label id, name, type |
| `gmail_search_threads` | Gmail query (`is:unread newer_than:7d`); max 25 |
| `gmail_get_thread` | Thread messages as metadata (From/To/Subject/Date/snippet) |
| `gmail_list_drafts` | Draft ids |
| `gmail_create_draft` | Create a draft only — does **not** send |
| `drive_search` | Drive `q` syntax; all-drives |
| `sheets_get_values` | A1 range read |
| `sheets_update_values` | A1 range write; `values_json` is a JSON array of rows |

Google API errors are returned as `Google API error {status}: {reason}` strings, not raised through Goose as crashes.

## Local run (debug)

```powershell
$env:GOOGLE_CLOUD_PROJECT = "your-gcp-project"
uv run --with "mcp>=2" --with google-api-python-client --with google-auth --with google-auth-httplib2 `
  python goose/local-mcp/google_adc_mcp.py
```

Parent overview (all extension logins): [`goose/README.md`](../README.md#extension-logins).
