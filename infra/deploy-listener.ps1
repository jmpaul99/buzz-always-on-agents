#Requires -Version 5.1
$ErrorActionPreference = "Continue"
. (Join-Path $PSScriptRoot "_common.ps1")
$Root = Split-Path -Parent $PSScriptRoot
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
    (Join-Path $listenerDir "taskmcp.py") `
    (Join-Path $listenerDir "task-mcps.json") `
    (Join-Path $listenerDir "nostrutil.py") `
    (Join-Path $listenerDir "requirements.txt") `
    (Join-Path $listenerDir "add-agent.sh") `
    (Join-Path $listenerDir "remove-agent.sh") `
    (Join-Path $listenerDir "keepalive.sh") `
    (Join-Path $listenerDir "buzz-listener.service") `
    (Join-Path $listenerDir "buzz-keepalive.service") `
    (Join-Path $listenerDir "buzz-keepalive.timer") `
    "${inst}:${tmp}/"

$remote = @'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip curl
install -d -m 755 /opt/buzz-listener /var/lib/buzz-listener
install -d -m 700 /etc/buzz
cp /tmp/buzz-listener-src/*.py /tmp/buzz-listener-src/requirements.txt /tmp/buzz-listener-src/task-mcps.json /opt/buzz-listener/
install -m 755 /tmp/buzz-listener-src/add-agent.sh /opt/buzz-listener/add-agent.sh
install -m 755 /tmp/buzz-listener-src/remove-agent.sh /opt/buzz-listener/remove-agent.sh
install -m 755 /tmp/buzz-listener-src/keepalive.sh /opt/buzz-listener/keepalive.sh
install -m 644 /tmp/buzz-listener-src/buzz-listener.service /etc/systemd/system/buzz-listener.service
install -m 644 /tmp/buzz-listener-src/buzz-keepalive.service /etc/systemd/system/buzz-keepalive.service
install -m 644 /tmp/buzz-listener-src/buzz-keepalive.timer /etc/systemd/system/buzz-keepalive.timer
python3 -m venv /opt/buzz-listener/.venv
/opt/buzz-listener/.venv/bin/pip install -q --upgrade pip
/opt/buzz-listener/.venv/bin/pip install -q -r /opt/buzz-listener/requirements.txt
systemctl daemon-reload
systemctl enable --now buzz-keepalive.timer
# Listener stays inactive until at least one /etc/buzz/*.env exists
if ls /etc/buzz/*.env >/dev/null 2>&1; then
  systemctl enable --now buzz-listener.service
else
  systemctl enable buzz-listener.service
  echo "no agent env yet; start buzz-listener after add-agent.sh"
fi
'@
$remotePath = Join-Path $env:TEMP "buzz-listener-install.sh"
$unix = (($remote -replace "`r`n", "`n") -replace "`r", "`n").TrimEnd() + "`n"
[System.IO.File]::WriteAllText($remotePath, $unix)
Invoke-Gcloud compute scp --project $Project --zone $Zone --tunnel-through-iap $remotePath "${inst}:/tmp/install-listener.sh"
Invoke-Gcloud compute ssh $inst --project $Project --zone $Zone --tunnel-through-iap --command "sudo bash /tmp/install-listener.sh"

$workerUrl = (& gcloud run services describe $C.GOOSE_SERVICE --project $Project --region $Region --format="value(status.url)").Trim()
if ($workerUrl) {
    $dropin = "[Service]`nEnvironment=GOOSE_WORKER_URL=$workerUrl`nEnvironment=GOOSE_WORKER_TIMEOUT=1620`nEnvironment=BUZZ_CONTROL_HOST=0.0.0.0`nEnvironment=BUZZ_CONTROL_PORT=8743`n"
    $dropinPath = Join-Path $env:TEMP "buzz-listener-worker.conf"
    [System.IO.File]::WriteAllText($dropinPath, ($dropin -replace "`r`n", "`n"))
    Invoke-Gcloud compute ssh $inst --project $Project --zone $Zone --tunnel-through-iap --command "sudo mkdir -p /etc/systemd/system/buzz-listener.service.d"
    Invoke-Gcloud compute scp --project $Project --zone $Zone --tunnel-through-iap $dropinPath "${inst}:/tmp/buzz-listener-worker.conf"
    Invoke-Gcloud compute ssh $inst --project $Project --zone $Zone --tunnel-through-iap --command "sudo mv /tmp/buzz-listener-worker.conf /etc/systemd/system/buzz-listener.service.d/worker.conf && sudo systemctl daemon-reload && sudo systemctl restart buzz-listener.service || true"
    Write-Host "listener GOOSE_WORKER_URL=$workerUrl"
}

$ip = (& gcloud compute instances describe $inst --project $Project --zone $Zone --format="value(networkInterfaces[0].accessConfigs[0].natIP)").Trim()
Write-Host "listener VM ready ip=$ip (IPv4 billed ~`$3.65/mo). SSH: gcloud compute ssh $inst --zone $Zone --tunnel-through-iap"
