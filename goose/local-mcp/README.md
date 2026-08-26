# Google Workspace MCP (ADC)

Optional stdio MCP for Gmail, Drive, and Sheets using **gcloud Application Default Credentials**. Listed in [`listener/mcp-catalog.json`](../../listener/mcp-catalog.json) as extra `googleadc` (`enabled: false`). Do not turn it on by default on an e2-micro.

On the VM this file is `/opt/buzz/local-mcp/google_adc_mcp.py`. Spawn: `uv run --with mcp --with google-api-python-client --with google-auth --with google-auth-httplib2 python /opt/buzz/local-mcp/google_adc_mcp.py`.

## Credentials

`google.auth.default(scopes=…)` plus refresh. Listener deploy copies Secret Manager `gcloud-adc` to `/etc/buzz/_adc.json` when that secret exists (`GOOGLE_APPLICATION_CREDENTIALS`). `GOOGLE_CLOUD_PROJECT` is also required.

Scopes (must be on the ADC client — default ADC from `deploy.ps1` does **not** include Gmail / Drive / Sheets):

- `openid`, `userinfo.email`, `cloud-platform`
- Gmail: `gmail.readonly`, `gmail.compose`
- Drive: `drive.readonly`, `drive.file`
- Sheets: `spreadsheets.readonly`, `spreadsheets`

## Login (PC, then upload)

There is no browser on the micro. Authenticate on the Windows machine that runs `infra/create-secrets.ps1`, then redeploy the listener.

1. Re-login ADC **with every scope above** (`--scopes` replaces the default set; omitting any of them drops that API):

   ```powershell
   gcloud auth application-default login --scopes="openid,https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/gmail.readonly,https://www.googleapis.com/auth/gmail.compose,https://www.googleapis.com/auth/drive.readonly,https://www.googleapis.com/auth/drive.file,https://www.googleapis.com/auth/spreadsheets.readonly,https://www.googleapis.com/auth/spreadsheets"
   ```

2. Upload the user ADC file and redeploy:

   ```powershell
   .\infra\create-secrets.ps1
   .\infra\deploy-listener.ps1
   ```

   `create-secrets.ps1` upserts `%APPDATA%\gcloud\application_default_credentials.json` as secret `gcloud-adc` (skipped if the file is missing).

Keep `googleadc` disabled in the catalog until you explicitly enable that extra (RAM + Node/uv).
