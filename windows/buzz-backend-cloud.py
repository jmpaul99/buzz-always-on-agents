# Buzz Desktop PATH plugin: executable name `buzz-backend-cloud`
# Wire: one JSON object on stdin, one JSON object on stdout, exit 0.
# protocol_version: 1. Never log nsecs.
# Deploys nsecs to the GCP e2-micro listener (control API or IAP SSH).
import json
import os
import pathlib
import shutil
import subprocess
import sys
import traceback
import urllib.error
import urllib.request

PROTOCOL_VERSION = 1
VERSION = "0.3.0"
NAME = "cloud"

DEFAULT_PROJECT = os.environ.get("BUZZ_GCP_PROJECT", "your-gcp-project")
DEFAULT_ZONE = os.environ.get("BUZZ_GCP_ZONE", "us-central1-a")
DEFAULT_INSTANCE = os.environ.get("BUZZ_GCP_INSTANCE", "buzz-listener")
DEFAULT_REMOTE_SCRIPT = "/opt/buzz-listener/add-agent.sh"
DEFAULT_REMOVE_SCRIPT = "/opt/buzz-listener/remove-agent.sh"
DEFAULT_SYNC_URL = os.environ.get("BUZZ_SYNC_URL", "http://127.0.0.1:8743")
RELAY_DEFAULT = os.environ.get("BUZZ_RELAY_URL", "wss://your-community.communities.buzz.xyz")


def _out(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, separators=(",", ":")))
    sys.stdout.flush()


def _fail(request_id: str, error: str, code: int = 1) -> int:
    _out({"ok": False, "request_id": request_id, "error": error})
    return code


def _info(_req: dict) -> dict:
    return {
        "ok": True,
        "name": NAME,
        "version": VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "description": "GCP e2-micro Buzz listener. Goose runs on Cloud Run, not on this PC.",
        "config_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "default": DEFAULT_PROJECT},
                "zone": {"type": "string", "default": DEFAULT_ZONE},
                "instance": {"type": "string", "default": DEFAULT_INSTANCE},
                "remote_script": {"type": "string", "default": DEFAULT_REMOTE_SCRIPT},
            },
        },
    }


def _gcloud() -> str:
    exe = shutil.which("gcloud") or shutil.which("gcloud.cmd")
    if not exe:
        raise ValueError("gcloud not on PATH")
    return exe


def _run_gcloud(args: list[str], timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
        stdin=subprocess.DEVNULL,
    )


def _slug(name: str) -> str:
    s = "".join(ch.lower() if ch.isalnum() else "-" for ch in (name or "agent"))
    s = "-".join(p for p in s.split("-") if p)
    return s[:32] or "agent"


def _teams_path() -> pathlib.Path:
    appdata = os.environ.get("APPDATA") or ""
    return pathlib.Path(appdata) / "xyz.block.buzz.app" / "agents" / "teams.json"


def _team_instructions(agent: dict) -> str:
    tid = str(agent.get("team_id") or "").strip()
    if not tid:
        return ""
    path = _teams_path()
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    records = data if isinstance(data, list) else (data.get("teams") if isinstance(data, dict) else [])
    if not isinstance(records, list):
        return ""
    for item in records:
        if isinstance(item, dict) and str(item.get("id") or "") == tid:
            return str(item.get("instructions") or "").strip()[:8000]
    return ""


def _allowlist(raw) -> str:
    if isinstance(raw, list):
        items = [str(x).strip().lower() for x in raw]
    else:
        items = [p.strip().lower() for p in str(raw or "").split(",")]
    out = []
    for item in items:
        if len(item) == 64 and item.isalnum() and item not in out:
            out.append(item)
    return ",".join(out)


def _cfg(req: dict) -> dict:
    return req.get("provider_config") or {}


def _project_zone_instance(cfg: dict) -> tuple[str, str, str]:
    project = (cfg.get("project") or os.environ.get("BUZZ_GCP_PROJECT") or DEFAULT_PROJECT).strip()
    zone = (cfg.get("zone") or os.environ.get("BUZZ_GCP_ZONE") or DEFAULT_ZONE).strip()
    instance = (cfg.get("instance") or os.environ.get("BUZZ_GCP_INSTANCE") or DEFAULT_INSTANCE).strip()
    return project, zone, instance


def _sync_state_path() -> pathlib.Path:
    appdata = os.environ.get("APPDATA") or ""
    return pathlib.Path(appdata) / "xyz.block.buzz.app" / "agents" / "cloud-sync-state.json"


def _sync_token() -> str:
    env = (os.environ.get("BUZZ_SYNC_TOKEN") or "").strip()
    if env:
        return env
    path = _sync_state_path()
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(data.get("token") or "").strip()


def _control_api(method: str, path: str, body: dict | None = None) -> dict | None:
    token = _sync_token()
    if not token:
        return None
    url = DEFAULT_SYNC_URL.rstrip("/") + path
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        parsed = json.loads(raw) if raw else {}
        if isinstance(parsed, dict) and parsed.get("ok"):
            return parsed
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    return None


def _iap_bash(cfg: dict, script: str, name: str) -> None:
    project, zone, instance = _project_zone_instance(cfg)
    gcloud = _gcloud()
    local = pathlib.Path(os.environ.get("TEMP") or "/tmp") / f"buzz-sync-{name}.sh"
    remote_tmp = f"/tmp/buzz-sync-{name}.sh"
    local.write_bytes(script.encode("utf-8"))
    try:
        scp = _run_gcloud(
            [
                gcloud,
                "compute",
                "scp",
                f"--project={project}",
                f"--zone={zone}",
                "--tunnel-through-iap",
                "--quiet",
                str(local),
                f"{instance}:{remote_tmp}",
            ]
        )
        if scp.returncode != 0:
            err = (scp.stderr or scp.stdout or "iap scp failed").strip()
            raise RuntimeError(err[:500])
        ssh = _run_gcloud(
            [
                gcloud,
                "compute",
                "ssh",
                instance,
                f"--project={project}",
                f"--zone={zone}",
                "--tunnel-through-iap",
                "--quiet",
                f"--command=sudo bash {remote_tmp}; st=$?; sudo rm -f {remote_tmp}; exit $st",
            ]
        )
        if ssh.returncode != 0:
            err = (ssh.stderr or ssh.stdout or "iap ssh failed").strip()
            raise RuntimeError(err[:500])
    finally:
        try:
            local.unlink()
        except OSError:
            pass


def _agent_fields(agent: dict) -> dict:
    name = _slug(str(agent.get("name") or "agent"))
    allow = _allowlist(agent.get("respond_to_allowlist"))
    return {
        "name": name,
        "display": str(agent.get("name") or name),
        "nsec": agent.get("private_key_nsec") or agent.get("nsec") or "",
        "auth_tag": agent.get("auth_tag") or "",
        "relay": agent.get("relay_url") or RELAY_DEFAULT,
        "prompt": agent.get("system_prompt") or "",
        "respond_to": str(agent.get("respond_to") or "owner-only").lower(),
        "allowlist": allow,
        "team_id": str(agent.get("team_id") or ""),
        "team_instructions": _team_instructions(agent),
        "updated_at": str(agent.get("updated_at") or ""),
        "pubkey": str(agent.get("pubkey") or "").lower(),
    }


def _deploy(req: dict) -> dict:
    agent = req.get("agent") or {}
    cfg = _cfg(req)
    fields = _agent_fields(agent)
    if not fields["nsec"]:
        raise ValueError("agent.private_key_nsec is required")
    pubkey = fields["pubkey"]
    api_body = {
        "nsec": fields["nsec"],
        "name": fields["display"],
        "slug": fields["name"],
        "system_prompt": fields["prompt"],
        "respond_to": fields["respond_to"],
        "respond_to_allowlist": [p for p in fields["allowlist"].split(",") if p],
        "team_id": fields["team_id"],
        "team_instructions": fields["team_instructions"],
        "auth_tag": fields["auth_tag"],
        "relay_url": fields["relay"],
        "updated_at": fields["updated_at"],
    }
    if pubkey and len(pubkey) == 64:
        got = _control_api("PUT", f"/agents/{pubkey}", api_body)
        if got:
            return {"ok": True, "agent_id": got.get("agent_id") or fields["name"]}
    remote = cfg.get("remote_script") or DEFAULT_REMOTE_SCRIPT
    payload = "\n".join(
        [
            "set -euo pipefail",
            f"export BUZZ_PRIVATE_KEY={json.dumps(fields['nsec'])}",
            f"export BUZZ_AUTH_TAG={json.dumps(fields['auth_tag'])}",
            f"export BUZZ_RELAY_URL={json.dumps(fields['relay'])}",
            f"export BUZZ_ACP_DISPLAY_NAME={json.dumps(fields['display'])}",
            f"export BUZZ_ACP_SYSTEM_PROMPT={json.dumps(fields['prompt'])}",
            f"export BUZZ_PUBKEY={json.dumps(pubkey)}",
            f"export BUZZ_ACP_RESPOND_TO={json.dumps(fields['respond_to'])}",
            f"export BUZZ_ACP_RESPOND_TO_ALLOWLIST={json.dumps(fields['allowlist'])}",
            f"export BUZZ_TEAM_ID={json.dumps(fields['team_id'])}",
            f"export BUZZ_ACP_TEAM_INSTRUCTIONS={json.dumps(fields['team_instructions'])}",
            f"export BUZZ_UPDATED_AT={json.dumps(fields['updated_at'])}",
            f"sudo -E {remote} {fields['name']}",
            "",
        ]
    )
    _iap_bash(cfg, payload, fields["name"])
    return {"ok": True, "agent_id": fields["name"]}


def _delete(req: dict) -> dict:
    agent = req.get("agent") or {}
    cfg = _cfg(req)
    fields = _agent_fields(agent)
    pubkey = fields["pubkey"]
    if pubkey and len(pubkey) == 64:
        got = _control_api("DELETE", f"/agents/{pubkey}")
        if got:
            return {"ok": True, "agent_id": got.get("agent_id") or fields["name"]}
    remote = cfg.get("remove_script") or DEFAULT_REMOVE_SCRIPT
    payload = "\n".join(
        [
            "set -euo pipefail",
            f"sudo {remote} {fields['name']}",
            "",
        ]
    )
    _iap_bash(cfg, payload, "rm-" + fields["name"])
    return {"ok": True, "agent_id": fields["name"]}


def main() -> int:
    if len(sys.argv) > 1:
        raw = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
    else:
        raw = sys.stdin.read()
    try:
        req = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return _fail("", "stdin is not JSON")
    request_id = str(req.get("request_id") or "")
    op = req.get("op")
    try:
        if op == "info":
            _out(_info(req))
            return 0
        if op == "deploy":
            _out(_deploy(req))
            return 0
        if op in {"delete", "undeploy"}:
            _out(_delete(req))
            return 0
        return _fail(request_id, f"unknown op: {op}")
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        return _fail(request_id, str(exc))


if __name__ == "__main__":
    sys.exit(main())
