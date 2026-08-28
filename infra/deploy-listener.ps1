#Requires -Version 5.1
$ErrorActionPreference = "Continue"
. (Join-Path $PSScriptRoot "_common.ps1")
$listenerDir = Join-Path $Root "listener"
$sa = "$($C.LISTENER_SA)@$Project.iam.gserviceaccount.com"
$inst = $C.LISTENER_INSTANCE

Invoke-Gcloud config set project $Project --quiet

& gcloud compute instances describe $inst --zone $Zone --project $Project 1>$null 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "creating $inst e2-micro"
    Invoke-Gcloud compute instances create $inst `
        --project $Project `
        --zone $Zone `
        --machine-type=e2-micro `
        "--network-interface=network-tier=PREMIUM,stack-type=IPV4_ONLY,subnet=default" `
        --maintenance-policy=MIGRATE `
        --provisioning-model=STANDARD `
        --service-account $sa `
        --scopes=https://www.googleapis.com/auth/cloud-platform `
        --tags=$($C.IAP_TAG) `
        --create-disk="auto-delete=yes,boot=yes,device-name=$inst,image-family=ubuntu-2404-lts-amd64,image-project=ubuntu-os-cloud,mode=rw,size=30,type=pd-standard" `
        --metadata=enable-oslogin=FALSE
} else {
    Write-Host "instance $inst already exists"
}

& gcloud compute firewall-rules describe allow-iap-8743 --project $Project 1>$null 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "creating allow-iap-8743 (IAP range only)"
    Invoke-Gcloud compute firewall-rules create allow-iap-8743 --project $Project `
        --allow=tcp:8743 --source-ranges=35.235.240.0/20 --target-tags=$($C.IAP_TAG) `
        --description="IAP TCP tunnel to listener control API"
}

$tmp = "/tmp/buzz-listener-src"
& gcloud compute ssh $inst --project $Project --zone $Zone --tunnel-through-iap --command "rm -rf $tmp && mkdir -p $tmp"
Invoke-Gcloud compute scp --project $Project --zone $Zone --tunnel-through-iap `
    (Join-Path $listenerDir "listener.py") `
    (Join-Path $listenerDir "agentutil.py") `
    (Join-Path $listenerDir "mcp_catalog.py") `
    (Join-Path $listenerDir "mcp-catalog.json") `
    (Join-Path $listenerDir "litellm_proxy.py") `
    (Join-Path $listenerDir "cloud_agents.py") `
    (Join-Path $listenerDir "nostrutil.py") `
    (Join-Path $listenerDir "requirements.txt") `
    (Join-Path $listenerDir "add-agent.sh") `
    (Join-Path $listenerDir "remove-agent.sh") `
    (Join-Path $listenerDir "run-acp.sh") `
    (Join-Path $listenerDir "run-mcp.sh") `
    (Join-Path $listenerDir "keepalive.sh") `
    (Join-Path $listenerDir "buzz-listener.service") `
    (Join-Path $listenerDir "buzz-acp@.service") `
    (Join-Path $listenerDir "buzz-litellm-proxy.service") `
    (Join-Path $listenerDir "buzz-keepalive.service") `
    (Join-Path $listenerDir "buzz-keepalive.timer") `
    "${inst}:${tmp}/"

$adcSrc = Join-Path $listenerDir "local-mcp\google_adc_mcp.py"
if (Test-Path -LiteralPath $adcSrc) {
    Invoke-Gcloud compute scp --project $Project --zone $Zone --tunnel-through-iap $adcSrc "${inst}:${tmp}/google_adc_mcp.py"
}
$mgrSrc = Join-Path $listenerDir "local-mcp\mcp_manager.py"
if (Test-Path -LiteralPath $mgrSrc) {
    Invoke-Gcloud compute scp --project $Project --zone $Zone --tunnel-through-iap $mgrSrc "${inst}:${tmp}/mcp_manager.py"
}
$skillSrc = Join-Path $listenerDir "skills\mcp-manager\SKILL.md"
if (Test-Path -LiteralPath $skillSrc) {
    Invoke-Gcloud compute scp --project $Project --zone $Zone --tunnel-through-iap $skillSrc "${inst}:${tmp}/mcp-manager-skill.md"
}

$remote = @'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl ca-certificates gnupg
if ! node -v 2>/dev/null | grep -q '^v24\.'; then
  curl -fsSL https://deb.nodesource.com/setup_24.x | bash -
fi
apt-get install -y -qq python3 python3-venv python3-pip tar nodejs
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
fi
curl -fsSL -o /tmp/github-mcp-server.tgz \
  https://github.com/github/github-mcp-server/releases/latest/download/github-mcp-server_Linux_x86_64.tar.gz
rm -rf /tmp/github-mcp-unpack && mkdir -p /tmp/github-mcp-unpack
tar -xzf /tmp/github-mcp-server.tgz -C /tmp/github-mcp-unpack
GHBIN=$(find /tmp/github-mcp-unpack -type f -name github-mcp-server | head -n1)
install -m 755 "$GHBIN" /usr/local/bin/github-mcp-server
rm -rf /tmp/github-mcp-server.tgz /tmp/github-mcp-unpack
install -d -m 755 /opt/buzz-listener /var/lib/buzz-listener /opt/sprig /opt/buzz/local-mcp /opt/buzz-listener/skills/mcp-manager
install -d -m 700 /etc/buzz
cp /tmp/buzz-listener-src/*.py /tmp/buzz-listener-src/requirements.txt /tmp/buzz-listener-src/mcp-catalog.json /opt/buzz-listener/
if [ -f /tmp/buzz-listener-src/google_adc_mcp.py ]; then
  install -m 644 /tmp/buzz-listener-src/google_adc_mcp.py /opt/buzz/local-mcp/google_adc_mcp.py
fi
if [ -f /tmp/buzz-listener-src/mcp_manager.py ]; then
  install -m 644 /tmp/buzz-listener-src/mcp_manager.py /opt/buzz/local-mcp/mcp_manager.py
fi
if [ -f /tmp/buzz-listener-src/mcp-manager-skill.md ]; then
  install -m 644 /tmp/buzz-listener-src/mcp-manager-skill.md /opt/buzz-listener/skills/mcp-manager/SKILL.md
fi
install -m 755 /tmp/buzz-listener-src/add-agent.sh /opt/buzz-listener/add-agent.sh
install -m 755 /tmp/buzz-listener-src/remove-agent.sh /opt/buzz-listener/remove-agent.sh
install -m 755 /tmp/buzz-listener-src/run-acp.sh /opt/buzz-listener/run-acp.sh
rm -f /opt/buzz-listener/run-agent.sh /opt/buzz-listener/acp_user_echo.py
install -m 755 /tmp/buzz-listener-src/run-mcp.sh /opt/buzz-listener/run-mcp.sh
install -m 755 /tmp/buzz-listener-src/keepalive.sh /opt/buzz-listener/keepalive.sh
sed -i 's/\r$//' /opt/buzz-listener/*.sh /opt/buzz-listener/cloud_agents.py
chmod +x /opt/buzz-listener/cloud_agents.py
ln -sfn /opt/buzz-listener/cloud_agents.py /usr/local/bin/buzz-cloud-agents
install -m 644 /tmp/buzz-listener-src/buzz-listener.service /etc/systemd/system/buzz-listener.service
install -m 644 /tmp/buzz-listener-src/buzz-acp@.service /etc/systemd/system/buzz-acp@.service
install -m 644 /tmp/buzz-listener-src/buzz-litellm-proxy.service /etc/systemd/system/buzz-litellm-proxy.service
install -m 644 /tmp/buzz-listener-src/buzz-keepalive.service /etc/systemd/system/buzz-keepalive.service
install -m 644 /tmp/buzz-listener-src/buzz-keepalive.timer /etc/systemd/system/buzz-keepalive.timer
SPRIG_URL="https://github.com/block/buzz/releases/download/sprig-latest/sprig-x86_64-unknown-linux-musl.tar.gz"
rm -rf /tmp/sprig && mkdir -p /tmp/sprig && cd /tmp/sprig
curl -fsSL "$SPRIG_URL" -o sprig.tar.gz
tar -xzf sprig.tar.gz
SRC=$(find . -type f \( -name sprig -o -name buzz-acp -o -name buzz \) | head -n1)
install -m 755 "$SRC" /opt/sprig/sprig
ln -sfn /opt/sprig/sprig /opt/sprig/buzz
ln -sfn /opt/sprig/sprig /opt/sprig/buzz-acp
ln -sfn /opt/sprig/sprig /opt/sprig/buzz-agent
ln -sfn /opt/sprig/sprig /opt/sprig/buzz-dev-mcp
ln -sfn /opt/sprig/sprig /usr/local/bin/buzz
ln -sfn /opt/sprig/sprig /usr/local/bin/buzz-acp
ln -sfn /opt/sprig/sprig /usr/local/bin/buzz-agent
ln -sfn /opt/sprig/sprig /usr/local/bin/buzz-dev-mcp
ln -sfn /opt/sprig/sprig /usr/local/bin/sprig
cd /
rm -rf /tmp/sprig
python3 -m venv --clear /opt/buzz-listener/.venv
/opt/buzz-listener/.venv/bin/python -m pip install -q --upgrade pip
/opt/buzz-listener/.venv/bin/python -m pip install -q -r /opt/buzz-listener/requirements.txt
systemctl daemon-reload
systemctl enable --now buzz-keepalive.timer
systemctl enable --now buzz-litellm-proxy.service
systemctl enable --now buzz-listener.service
for envf in /etc/buzz/*.env; do
  [ -f "$envf" ] || continue
  slug=$(basename "$envf" .env)
  case "$slug" in _*) continue ;; esac
  systemctl enable --now "buzz-acp@${slug}.service" || true
done
'@
$remotePath = Join-Path $env:TEMP "buzz-listener-install.sh"
$unix = (($remote -replace "`r`n", "`n") -replace "`r", "`n").TrimEnd() + "`n"
[System.IO.File]::WriteAllText($remotePath, $unix)
Invoke-Gcloud compute scp --project $Project --zone $Zone --tunnel-through-iap $remotePath "${inst}:/tmp/install-listener.sh"
Invoke-Gcloud compute ssh $inst --project $Project --zone $Zone --tunnel-through-iap --command "sudo bash /tmp/install-listener.sh"

$litellmUrl = (& gcloud run services describe $C.LITELLM_SERVICE --project $Project --region $Region --format="value(status.url)" 2>$null).Trim()
if (-not $litellmUrl) { throw "deploy LiteLLM first" }
$master = (& gcloud secrets versions access latest --secret=litellm-master-key --project $Project 2>$null)
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($master)) {
    throw "litellm-master-key secret is missing"
}
$runtime = @(
    "LITELLM_URL=$litellmUrl",
    "LITELLM_AUDIENCE=$litellmUrl",
    "LITELLM_MASTER_KEY=$master",
    "OPENAI_COMPAT_API_KEY=$master",
    "BUZZ_AGENT_PROVIDER=openai",
    "OPENAI_COMPAT_BASE_URL=http://127.0.0.1:4000/v1",
    "OPENAI_COMPAT_MODEL=goose",
    "OPENAI_COMPAT_API=chat",
    "BUZZ_AGENT_REQUIRE_REPLY=1",
    "MCP_HOOK_SERVERS=*",
    "BUZZ_ACP_AGENT_COMMAND=buzz-agent",
    "BUZZ_ACP_AGENT_ARGS=",
    "BUZZ_ACP_MCP_COMMAND=/opt/buzz-listener/run-mcp.sh",
    "BUZZ_ACP_RELAY_OBSERVER=true",
    "BUZZ_ACP_LAZY_POOL=true",
    "BUZZ_ACP_IDLE_POOL_SLEEP=900",
    "BUZZ_ACP_AGENTS=1",
    "RUST_LOG=info,buzz_acp=info",
    "LITELLM_PROXY_TIMEOUT_SECS=300",
    "LISTENER_CONTROL_URL=http://127.0.0.1:8743",
    "APPLY_SA=$sa",
    "BUZZ_WORKSPACE=/var/lib/buzz-listener",
    "GOOGLE_CLOUD_PROJECT=$Project"
)
function Test-Secret([string]$Name) {
    & gcloud secrets describe $Name --project $Project 1>$null 2>$null
    return ($LASTEXITCODE -eq 0)
}
function Get-Secret([string]$Name) {
    $v = & gcloud secrets versions access latest --secret=$Name --project $Project 2>$null
    if ($LASTEXITCODE -ne 0) { return "" }
    return [string]$v
}
if (Test-Secret "github-pat") {
    $tok = Get-Secret "github-pat"
    if ($tok) { $runtime += "GITHUB_PERSONAL_ACCESS_TOKEN=$tok" }
}
if (Test-Secret "tavily-api-key") {
    $tok = Get-Secret "tavily-api-key"
    if ($tok) { $runtime += "TAVILY_API_KEY=$tok" }
}
if (Test-Secret "stripe-api-key") {
    $tok = Get-Secret "stripe-api-key"
    if ($tok) { $runtime += "STRIPE_API_KEY=$tok" }
}
$runtimePath = Join-Path $env:TEMP "buzz-runtime.env"
[System.IO.File]::WriteAllText($runtimePath, (($runtime -join "`n") + "`n"))
Invoke-Gcloud compute scp --project $Project --zone $Zone --tunnel-through-iap $runtimePath "${inst}:/tmp/_runtime.env"
$applyRuntime = @'
set -euo pipefail
install -m 600 /tmp/_runtime.env /etc/buzz/_runtime.env
rm -f /tmp/_runtime.env
systemctl daemon-reload
systemctl restart buzz-litellm-proxy.service
systemctl restart buzz-listener.service
systemctl list-units --type=service --all 'buzz-acp@*' --no-legend | awk '{print $1}' | while read -r u; do
  [ -n "$u" ] || continue
  systemctl restart "$u" || true
done
'@
$applyPath = Join-Path $env:TEMP "buzz-apply-runtime.sh"
[System.IO.File]::WriteAllText($applyPath, (($applyRuntime -replace "`r`n", "`n") -replace "`r", "`n").TrimEnd() + "`n")
Invoke-Gcloud compute scp --project $Project --zone $Zone --tunnel-through-iap $applyPath "${inst}:/tmp/apply-runtime.sh"
Invoke-Gcloud compute ssh $inst --project $Project --zone $Zone --tunnel-through-iap --command "sudo bash /tmp/apply-runtime.sh"
Remove-Item -Force $runtimePath, $applyPath -ErrorAction SilentlyContinue

if (Test-Secret "gcloud-adc") {
    $adcTmp = Join-Path $env:TEMP "buzz-adc.json"
    & gcloud secrets versions access latest --secret=gcloud-adc --project $Project --out-file=$adcTmp 1>$null
    if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $adcTmp)) {
        Invoke-Gcloud compute scp --project $Project --zone $Zone --tunnel-through-iap $adcTmp "${inst}:/tmp/_adc.json"
        $afterAdc = @'
set -euo pipefail
install -m 600 /tmp/_adc.json /etc/buzz/_adc.json
rm -f /tmp/_adc.json
grep -q '^GOOGLE_APPLICATION_CREDENTIALS=' /etc/buzz/_runtime.env || printf '\nGOOGLE_APPLICATION_CREDENTIALS=/etc/buzz/_adc.json\n' >> /etc/buzz/_runtime.env
systemctl restart buzz-litellm-proxy.service
systemctl restart buzz-listener.service
systemctl list-units --type=service --all 'buzz-acp@*' --no-legend | awk '{print $1}' | while read -r u; do
  [ -n "$u" ] || continue
  systemctl restart "$u" || true
done
'@
        $afterAdcPath = Join-Path $env:TEMP "buzz-after-adc.sh"
        [System.IO.File]::WriteAllText($afterAdcPath, (($afterAdc -replace "`r`n", "`n") -replace "`r", "`n").TrimEnd() + "`n")
        Invoke-Gcloud compute scp --project $Project --zone $Zone --tunnel-through-iap $afterAdcPath "${inst}:/tmp/after-adc.sh"
        Invoke-Gcloud compute ssh $inst --project $Project --zone $Zone --tunnel-through-iap --command "sudo bash /tmp/after-adc.sh"
        Remove-Item -Force $adcTmp, $afterAdcPath -ErrorAction SilentlyContinue
    }
}

$ip = (& gcloud compute instances describe $inst --project $Project --zone $Zone --format="value(networkInterfaces[0].accessConfigs[0].natIP)").Trim()
Write-Host "listener VM ready ip=$ip (IPv4 billed ~`$3.65/mo). SSH: gcloud compute ssh $inst --zone $Zone --tunnel-through-iap"
Write-Host "LiteLLM proxy -> $litellmUrl"
