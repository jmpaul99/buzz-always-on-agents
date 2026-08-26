"""Watch Buzz Desktop agent files and sync them with the GCP listener.

Never logs nsecs, auth tags, or allowlist dumps. Desktop is the identity store.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from typing import Any

_HERE = pathlib.Path(__file__).resolve().parent
for extra in (_HERE, _HERE.parent / "listener"):
    if (extra / "agentutil.py").is_file() and str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import agentutil as au  # noqa: E402

DEFAULT_PROJECT = os.environ.get("BUZZ_GCP_PROJECT", "your-gcp-project")
DEFAULT_ZONE = os.environ.get("BUZZ_GCP_ZONE", "us-central1-a")
DEFAULT_INSTANCE = os.environ.get("BUZZ_GCP_INSTANCE", "buzz-listener")
SYNC_URL = os.environ.get("BUZZ_SYNC_URL", "http://127.0.0.1:8743").rstrip("/")
LOCAL_RUNTIME = au.LOCAL_RUNTIME_FIELDS
DEBOUNCE_SECS = 0.4
PULL_SECS = 5.0
TUNNEL_WAIT_SECS = 45


def _agents_dir() -> pathlib.Path:
    appdata = os.environ.get("APPDATA") or ""
    return pathlib.Path(appdata) / "xyz.block.buzz.app" / "agents"


def _log_path() -> pathlib.Path:
    return _agents_dir() / "cloud-sync.log"


def _state_path() -> pathlib.Path:
    return _agents_dir() / "cloud-sync-state.json"


def log(msg: str) -> None:
    line = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + " " + msg
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _gcloud() -> str:
    exe = shutil.which("gcloud") or shutil.which("gcloud.cmd")
    if not exe:
        raise RuntimeError("gcloud not on PATH")
    return exe


def _gcloud_argv() -> list[str]:
    """Call gcloud's python entrypoint so Windows never opens a cmd.exe console."""
    exe = pathlib.Path(_gcloud())
    if exe.suffix.lower() in {".cmd", ".bat"}:
        root = exe.resolve().parent.parent
        script = root / "lib" / "gcloud.py"
        bundled = root / "platform" / "bundledpython" / "python.exe"
        if script.is_file() and bundled.is_file():
            return [str(bundled), "-S", str(script)]
    return [str(exe)]


CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
MUTEX_NAME = "Local\\BuzzOracleCloudSync"


def _startupinfo_hidden():
    if sys.platform != "win32":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    return si


def _run(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    kw: dict[str, Any] = {
        "args": args,
        "text": True,
        "capture_output": True,
        "timeout": timeout,
        "check": False,
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kw["creationflags"] = CREATE_NO_WINDOW
        kw["startupinfo"] = _startupinfo_hidden()
    return subprocess.run(**kw)


def _popen(args: list[str]) -> subprocess.Popen[str]:
    kw: dict[str, Any] = {
        "args": args,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "text": True,
    }
    if sys.platform == "win32":
        kw["creationflags"] = CREATE_NO_WINDOW
        kw["startupinfo"] = _startupinfo_hidden()
    return subprocess.Popen(**kw)


def _kill_tree(pid: int) -> None:
    if pid <= 0:
        return
    if sys.platform == "win32":
        _run(["taskkill", "/PID", str(pid), "/T", "/F"], timeout=20)
        return
    try:
        os.kill(pid, 15)
    except OSError:
        pass


def _win_processes() -> list[dict[str, Any]]:
    if sys.platform != "win32":
        return []
    result = _run(
        [
            "powershell.exe",
            "-NoProfile",
            "-WindowStyle",
            "Hidden",
            "-Command",
            "Get-CimInstance Win32_Process | "
            "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress",
        ],
        timeout=40,
    )
    if result.returncode != 0 or not (result.stdout or "").strip():
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    return []


def _is_sync_orphan(name: str, cmdline: str, pid: int) -> bool:
    if pid in {os.getpid(), os.getppid()}:
        return False
    cl = (cmdline or "").lower()
    nm = (name or "").lower()
    if "buzz_cloud_sync" in cl or "buzz-cloud-sync" in cl:
        return True
    if "start-iap-tunnel" in cl and "buzz-listener" in cl:
        return True
    if "compute ssh" in cl and "buzz-listener" in cl and "8743" in cl:
        return True
    if nm in {"putty.exe", "plink.exe"} and "8743" in cl:
        return True
    if "8743" in cl and "buzz-listener" in cl:
        return True
    return False


def reap_orphans() -> int:
    killed = 0
    for row in _win_processes():
        try:
            pid = int(row.get("ProcessId") or 0)
        except (TypeError, ValueError):
            continue
        name = str(row.get("Name") or "")
        cmdline = str(row.get("CommandLine") or "")
        if not _is_sync_orphan(name, cmdline, pid):
            continue
        _kill_tree(pid)
        killed += 1
    if killed:
        log(f"reaped {killed} leftover sync/tunnel processes")
    return killed


def acquire_single_instance() -> Any:
    if sys.platform != "win32":
        return None
    import ctypes

    kernel32 = ctypes.windll.kernel32
    for _ in range(15):
        handle = kernel32.CreateMutexW(None, True, MUTEX_NAME)
        if kernel32.GetLastError() != 183:
            return handle
        kernel32.CloseHandle(handle)
        time.sleep(0.3)
    return None


_CRED_TARGETS = (
    "secrets.buzz-desktop",
    "LegacyGeneric:target=secrets.buzz-desktop",
    "buzz-desktop",
    "LegacyGeneric:target=buzz-desktop",
    "buzz-desktop:secrets",
)
_cred_target = "secrets.buzz-desktop"


def _cred_struct():
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.c_void_p),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    return ctypes, wintypes, CREDENTIAL


def read_cred_blob() -> dict[str, Any]:
    global _cred_target
    if sys.platform != "win32":
        return {}
    ctypes, wintypes, CREDENTIAL = _cred_struct()

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    cred_read = advapi.CredReadW
    cred_read.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    cred_read.restype = wintypes.BOOL
    cred_free = advapi.CredFree
    cred_free.argtypes = [ctypes.c_void_p]
    for target in _CRED_TARGETS:
        ptr = ctypes.c_void_p()
        if not cred_read(target, 1, 0, ctypes.byref(ptr)):
            continue
        try:
            cred = ctypes.cast(ptr, ctypes.POINTER(CREDENTIAL)).contents
            size = int(cred.CredentialBlobSize)
            if not cred.CredentialBlob or size <= 0:
                continue
            buf = ctypes.string_at(cred.CredentialBlob, size)
            if len(buf) >= 2 and buf[1] == 0:
                text = buf.decode("utf-16-le").rstrip("\x00")
            else:
                text = buf.decode("utf-8", errors="replace").rstrip("\x00")
            data = json.loads(text)
            if isinstance(data, dict):
                _cred_target = target
                return data
        except (json.JSONDecodeError, OSError, ValueError):
            continue
        finally:
            cred_free(ptr)
    _cred_target = "secrets.buzz-desktop"
    return {}


def write_cred_blob(blob: dict[str, Any]) -> None:
    """Merge-write the Desktop secrets JSON. Never logs the payload."""
    if sys.platform != "win32":
        return
    ctypes, wintypes, CREDENTIAL = _cred_struct()
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    cred_write = advapi.CredWriteW
    cred_write.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
    cred_write.restype = wintypes.BOOL
    payload = json.dumps(blob, separators=(",", ":")).encode("utf-16-le")
    buf = ctypes.create_string_buffer(payload, len(payload))
    cred = CREDENTIAL()
    cred.Flags = 0
    cred.Type = 1
    cred.TargetName = _cred_target
    cred.CredentialBlobSize = len(payload)
    cred.CredentialBlob = ctypes.cast(buf, ctypes.c_void_p)
    cred.Persist = 2
    cred.UserName = ""
    if not cred_write(ctypes.byref(cred), 0):
        err = ctypes.get_last_error()
        raise RuntimeError(f"failed to write credential manager secrets ({err})")


def apply_secret_keys(blob: dict[str, Any], imported: list, removed: list[str]) -> bool:
    changed = False
    for item in imported:
        if not isinstance(item, dict):
            continue
        pk = str(item.get("pubkey") or "").lower()
        nsec = str(item.get("nsec") or "").strip()
        if not pk or not nsec:
            continue
        key = f"agent:{pk}"
        if blob.get(key) != nsec:
            blob[key] = nsec
            changed = True
    for pk in removed:
        key = f"agent:{str(pk or '').lower()}"
        if key in blob:
            blob.pop(key, None)
            changed = True
    return changed


def nsec_for(pubkey: str, blob: dict[str, Any]) -> str:
    key = f"agent:{pubkey}"
    val = blob.get(key) or blob.get(key.lower()) or ""
    return str(val).strip()


def load_json(path: pathlib.Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path: pathlib.Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def live_rows(records: list) -> dict[str, dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    if not isinstance(records, list):
        return best
    for row in records:
        if not isinstance(row, dict):
            continue
        pk = str(row.get("pubkey") or "").lower()
        if len(pk) != 64:
            continue
        prev = best.get(pk)
        if prev is None or str(row.get("updated_at") or "") >= str(prev.get("updated_at") or ""):
            best[pk] = row
    return best


def load_state() -> dict[str, Any]:
    data = load_json(_state_path(), {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("token", "")
    data.setdefault("agents", {})
    return data


def save_state(state: dict[str, Any]) -> None:
    save_json(_state_path(), {"token": state.get("token") or "", "agents": state.get("agents") or {}})


class Tunnel:
    def __init__(self) -> None:
        self.proc: subprocess.Popen[str] | None = None
        self.mode = ""

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if self.proc is None:
            return
        pid = self.proc.pid
        self.proc = None
        self.mode = ""
        _kill_tree(pid)

    def start(self) -> None:
        self.stop()
        self.proc = _popen(
            _gcloud_argv()
            + [
                "compute",
                "start-iap-tunnel",
                DEFAULT_INSTANCE,
                "8743",
                "--local-host-port=127.0.0.1:8743",
                f"--zone={DEFAULT_ZONE}",
                f"--project={DEFAULT_PROJECT}",
                "--quiet",
            ]
        )
        self.mode = "iap"
        log("iap tcp tunnel started")

    def start_ssh_fallback(self) -> None:
        self.stop()
        self.proc = _popen(
            _gcloud_argv()
            + [
                "compute",
                "ssh",
                DEFAULT_INSTANCE,
                f"--project={DEFAULT_PROJECT}",
                f"--zone={DEFAULT_ZONE}",
                "--tunnel-through-iap",
                "--quiet",
                "--ssh-flag=-N",
                "--ssh-flag=-n",
                "--ssh-flag=-L127.0.0.1:8743:127.0.0.1:8743",
            ]
        )
        self.mode = "ssh"
        log("iap ssh local-forward started (hidden)")


def health_ok() -> bool:
    try:
        with urllib.request.urlopen(SYNC_URL + "/health", timeout=3) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def fetch_token() -> str:
    result = _run(
        _gcloud_argv() + [
            "compute",
            "ssh",
            DEFAULT_INSTANCE,
            f"--project={DEFAULT_PROJECT}",
            f"--zone={DEFAULT_ZONE}",
            "--tunnel-through-iap",
            "--quiet",
            "--command=sudo cat /etc/buzz/_sync.token",
        ],
        timeout=90,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "token fetch failed").strip()
        raise RuntimeError(err[:300])
    token = (result.stdout or "").strip().splitlines()
    token = token[-1].strip() if token else ""
    if not token:
        raise RuntimeError("empty sync token")
    return token


def api(method: str, path: str, token: str, body: dict | None = None) -> dict[str, Any]:
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        SYNC_URL + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    parsed = json.loads(raw) if raw else {}
    if not isinstance(parsed, dict):
        raise RuntimeError("invalid api response")
    return parsed


def wait_for_api(tunnel: Tunnel) -> None:
    deadline = time.monotonic() + TUNNEL_WAIT_SECS
    while time.monotonic() < deadline:
        if health_ok():
            return
        if not tunnel.alive():
            tunnel.start()
        time.sleep(1)
    raise RuntimeError("control api not reachable on 127.0.0.1:8743")


def put_agent(token: str, row: dict[str, Any], nsec: str) -> str:
    pk = str(row.get("pubkey") or "").lower()
    slug = au.slug_name(str(row.get("name") or "agent"))
    body = {
        "nsec": nsec,
        "name": row.get("name") or slug,
        "slug": slug,
        "system_prompt": row.get("system_prompt") or "",
        "respond_to": row.get("respond_to") or "owner-only",
        "respond_to_allowlist": au.parse_allowlist(row.get("respond_to_allowlist")),
        "team_id": row.get("team_id") or "",
        "auth_tag": row.get("auth_tag") or "",
        "relay_url": row.get("relay_url") or au.DEFAULT_RELAY,
        "updated_at": row.get("updated_at") or au.utc_now(),
        "channel_allowlist": row.get("channel_allowlist") or [],
    }
    got = api("PUT", f"/agents/{pk}", token, body)
    return str(got.get("agent_id") or slug)


def provider_backend(existing: dict[str, Any] | None = None) -> dict[str, Any]:
    existing = existing if isinstance(existing, dict) else {}
    cfg = existing.get("config") if isinstance(existing.get("config"), dict) else {}
    if not cfg:
        cfg = {
            "project": DEFAULT_PROJECT,
            "zone": DEFAULT_ZONE,
            "instance": DEFAULT_INSTANCE,
            "remote_script": "/opt/buzz-listener/add-agent.sh",
        }
    pid = str(existing.get("id") or "").strip() or "cloud"
    return {"type": "provider", "id": pid, "config": cfg}


def sanitize_backend(row: dict[str, Any]) -> bool:
    """Desktop BackendKind::Provider requires { type, id, config }."""
    backend = row.get("backend")
    if not isinstance(backend, dict):
        return False
    kind = str(backend.get("type") or "")
    if kind == "local":
        return False
    if (
        kind == "provider"
        and str(backend.get("id") or "").strip()
        and isinstance(backend.get("config"), dict)
    ):
        return False
    row["backend"] = provider_backend(backend)
    return True


def mark_cloud_backend(row: dict[str, Any], slug: str) -> None:
    row["backend"] = provider_backend(row.get("backend") if isinstance(row.get("backend"), dict) else None)
    row["backend_agent_id"] = slug


def push(state: dict[str, Any], records: list, blob: dict[str, Any]) -> bool:
    token = str(state.get("token") or "")
    tracked: dict[str, Any] = dict(state.get("agents") or {})
    live = live_rows(records)
    wrote_desktop = False
    for pk, row in live.items():
        nsec = nsec_for(pk, blob)
        if not nsec:
            log(f"skip push {pk[:12]}: no nsec in credential manager")
            continue
        fp = au.settings_fingerprint(
            {
                "name": row.get("name"),
                "system_prompt": row.get("system_prompt"),
                "respond_to": row.get("respond_to"),
                "respond_to_allowlist": row.get("respond_to_allowlist"),
                "team_id": row.get("team_id"),
                "relay_url": row.get("relay_url"),
                "channel_allowlist": row.get("channel_allowlist"),
            }
        )
        prev = tracked.get(pk) or {}
        if prev.get("fingerprint") == fp:
            continue
        slug = put_agent(token, row, nsec)
        tracked[pk] = {"fingerprint": fp, "slug": slug, "updated_at": row.get("updated_at") or ""}
        mark_cloud_backend(row, slug)
        wrote_desktop = True
        log(f"pushed {pk[:12]} as {slug}")
    vanished = [pk for pk in list(tracked) if pk not in live]
    for pk in vanished:
        api("DELETE", f"/agents/{pk}", token)
        tracked.pop(pk, None)
        log(f"undeployed {pk[:12]}")
    state["agents"] = tracked
    return wrote_desktop


def pull(state: dict[str, Any], records: list) -> bool:
    token = str(state.get("token") or "")
    got = api("GET", "/agents", token)
    cloud_agents = got.get("agents") if isinstance(got.get("agents"), list) else []
    blob = read_cred_blob()
    identity = str(blob.get("identity") or "").strip()
    users = au.desktop_user_pubkeys(records, identity)
    tracked = dict(state.get("agents") or {})
    new_records, tracked, imported, removed, updated = au.apply_cloud_roster(
        records, cloud_agents, tracked, user_pubkeys=users
    )
    records[:] = new_records
    state["agents"] = tracked
    for item in imported:
        pk = str(item.get("pubkey") or "").lower()
        row = next((r for r in records if str(r.get("pubkey") or "").lower() == pk), None)
        if isinstance(row, dict):
            mark_cloud_backend(row, str(item.get("slug") or row.get("slug") or ""))
        log(f"imported {pk[:12]}")
    for pk in removed:
        log(f"dropped local card {pk[:12]}")
    for pk in updated:
        log(f"pulled settings for {pk[:12]}")
    live = live_rows(records)
    imported_pks = {str(item.get("pubkey") or "").lower() for item in imported}
    for cloud in cloud_agents:
        if not isinstance(cloud, dict):
            continue
        pk = str(cloud.get("pubkey") or "").lower()
        if not au.PUBKEY_RE.match(pk):
            continue
        if pk in live or pk in removed or pk in imported_pks:
            continue
        if not au.user_can_access_agent(cloud, users):
            log(f"skip import {pk[:12]}: no access")
            continue
        log(f"skip import {pk[:12]}: no nsec from cloud")
    blob_changed = apply_secret_keys(blob, imported, removed)
    for cloud in cloud_agents:
        if not isinstance(cloud, dict):
            continue
        pk = str(cloud.get("pubkey") or "").lower()
        nsec = str(cloud.get("nsec") or cloud.get("private_key_nsec") or "").strip()
        if not nsec or pk not in live:
            continue
        key = f"agent:{pk}"
        if blob.get(key) != nsec:
            blob[key] = nsec
            blob_changed = True
    if blob_changed:
        write_cred_blob(blob)
    return bool(imported or removed or updated or blob_changed)


def persist_records(path: pathlib.Path, records: list) -> None:
    if isinstance(records, list):
        records, _dropped = au.compact_desktop_records(records)
        for row in records:
            if isinstance(row, dict):
                sanitize_backend(row)
        save_json(path, records)
        return
    save_json(path, records)


def sync_once(state: dict[str, Any]) -> dict[str, Any]:
    agents_path = _agents_dir() / "managed-agents.json"
    records = load_json(agents_path, [])
    if not isinstance(records, list):
        records = []
    records, dropped = au.compact_desktop_records(records)
    if dropped:
        tracked = dict(state.get("agents") or {})
        drop_names = {au.display_name_key(row) for row in dropped if isinstance(row, dict)}
        drop_pks = {
            str(row.get("pubkey") or "").lower()
            for row in dropped
            if isinstance(row, dict) and au.PUBKEY_RE.match(str(row.get("pubkey") or "").lower())
        }
        for pk, row in live_rows(records).items():
            if au.display_name_key(row) in drop_names:
                tracked.pop(pk, None)
        for pk in drop_pks:
            if pk not in live_rows(records):
                tracked.setdefault(pk, {"fingerprint": "dropped-duplicate", "slug": "", "updated_at": ""})
        state["agents"] = tracked
        persist_records(agents_path, records)
        log(f"compacted desktop store; dropped {len(dropped)} duplicate cards")
    pulled = pull(state, records)
    blob = read_cred_blob()
    wrote = push(state, records, blob)
    if wrote or pulled or dropped:
        persist_records(agents_path, records)
    save_state(state)
    return state


def watch_loop(once: bool) -> None:
    agents_dir = _agents_dir()
    agents_path = agents_dir / "managed-agents.json"
    teams_path = agents_dir / "teams.json"
    tunnel = Tunnel()
    state = load_state()
    try:
        tunnel.start()
        wait_for_api(tunnel)
        log("control api reachable")
        if not state.get("token"):
            state["token"] = fetch_token()
            save_state(state)
        if once:
            sync_once(state)
            return
        last_mtime = 0.0
        pending_since = 0.0
        last_pull = 0.0
        last_healthy = time.monotonic()
        while True:
            if health_ok():
                last_healthy = time.monotonic()
            elif not tunnel.alive() or time.monotonic() - last_healthy > 30:
                log("tunnel down; restarting")
                tunnel.start()
                wait_for_api(tunnel)
                last_healthy = time.monotonic()
            mtime = 0.0
            for path in (agents_path, teams_path):
                try:
                    mtime = max(mtime, path.stat().st_mtime)
                except OSError:
                    pass
            now = time.monotonic()
            if mtime > last_mtime:
                last_mtime = mtime
                pending_since = now
            if pending_since and now - pending_since >= DEBOUNCE_SECS:
                pending_since = 0.0
                try:
                    state = load_state()
                    sync_once(state)
                except Exception:
                    log("push failed")
                    traceback.print_exc()
            if now - last_pull >= PULL_SECS:
                last_pull = now
                try:
                    state = load_state()
                    records = load_json(agents_path, [])
                    if isinstance(records, list) and pull(state, records):
                        persist_records(agents_path, records)
                    save_state(state)
                except Exception:
                    log("pull failed")
            time.sleep(0.4)
    finally:
        tunnel.stop()


def attach_log_stdio() -> None:
    if sys.platform != "win32":
        return
    if not (sys.executable or "").lower().endswith("pythonw.exe"):
        return
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a", encoding="utf-8")
    sys.stdout = stream
    sys.stderr = stream


def spawn_silent_daemon() -> bool:
    """Scheduled tasks may still launch python.exe. Hand off to pythonw and close the console."""
    if sys.platform != "win32" or "--once" in sys.argv:
        return False
    exe = pathlib.Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        return False
    pythonw = exe.with_name("pythonw.exe")
    script = str(pathlib.Path(__file__).resolve())
    if pythonw.is_file():
        flags = CREATE_NO_WINDOW | 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        kw: dict[str, Any] = {
            "args": [str(pythonw), script, *sys.argv[1:]],
            "cwd": str(pathlib.Path(script).parent),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
            "creationflags": flags,
            "startupinfo": _startupinfo_hidden(),
        }
        subprocess.Popen(**kw)
        return True
    try:
        import ctypes

        ctypes.windll.kernel32.FreeConsole()
    except Exception:
        pass
    return False


def main() -> int:
    if spawn_silent_daemon():
        return 0
    once = "--once" in sys.argv
    attach_log_stdio()
    if not once:
        reap_orphans()
        lock = acquire_single_instance()
        if lock is None:
            log("another BuzzCloudSync is already running; exiting")
            return 0
    try:
        watch_loop(once)
        return 0
    except Exception as exc:
        log(f"fatal {type(exc).__name__}")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
