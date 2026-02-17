#!/usr/bin/env python3
"""
time.py - iLO5 scheduler + cron manager using same .env as ilo.py

Adds:
- Strong time sync preflight before scheduling:
  Prefer chrony -> timedatectl/systemd-timesyncd -> ntpq/ntpd
- Blocks cron job creation if not synced, unless user chooses override
"""

import os
import sys
import json
import time
import uuid
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.auth import HTTPBasicAuth
from requests.exceptions import (
    RequestException,
    SSLError,
    ConnectTimeout,
    ReadTimeout,
    ConnectionError as ReqConnectionError,
)

# ----------------------------
# .env loader (supports multi-line JSON)
# ----------------------------

def load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    i = 0
    while i < len(lines):
        raw = lines[i]
        i += 1
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()

        if not k or k in os.environ:
            continue

        if (v.startswith("[") and not v.rstrip().endswith("]")) or (v.startswith("{") and not v.rstrip().endswith("}")):
            buf = [v]
            open_brackets = v.count("[") - v.count("]")
            open_braces = v.count("{") - v.count("}")

            while i < len(lines) and (open_brackets > 0 or open_braces > 0):
                nxt = lines[i].rstrip("\n")
                i += 1
                buf.append(nxt)
                open_brackets += nxt.count("[") - nxt.count("]")
                open_braces += nxt.count("{") - nxt.count("}")

            v = "\n".join(buf).strip()

        os.environ[k] = v


def _str_to_bool(s: Optional[str], default: bool = True) -> bool:
    if s is None:
        return default
    s = s.strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return default


# ----------------------------
# Config
# ----------------------------

@dataclass
class IloHost:
    name: str
    host: str
    username: str
    password: str


def load_hosts_from_env() -> List[IloHost]:
    raw = os.environ.get("ILO_HOSTS", "").strip()
    if not raw:
        raise ValueError("ILO_HOSTS is missing/empty. Put it in your .env.")

    try:
        items = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"ILO_HOSTS is not valid JSON: {e}") from e

    if not isinstance(items, list):
        raise ValueError("ILO_HOSTS must be a JSON list of host objects.")

    hosts: List[IloHost] = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"ILO_HOSTS[{idx}] must be an object.")
        for key in ("name", "host", "username", "password"):
            if key not in item or not str(item[key]).strip():
                raise ValueError(f"ILO_HOSTS[{idx}] missing '{key}'.")
        hosts.append(IloHost(
            name=str(item["name"]).strip(),
            host=str(item["host"]).strip(),
            username=str(item["username"]).strip(),
            password=str(item["password"]).strip(),
        ))
    return hosts


# ----------------------------
# Redfish client (minimal, robust)
# ----------------------------

class RedfishError(Exception):
    pass


class IloRedfishClient:
    def __init__(self, host: IloHost, verify_tls: bool, timeout: int, retries: int = 1):
        self.host = host
        self.base = f"https://{host.host}"
        self.verify_tls = verify_tls
        self.timeout = timeout
        self.retries = retries

        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "ilo-scheduler/1.1",
            "Connection": "close",
        })

        self._token: Optional[str] = None
        self._systems_id: Optional[str] = None

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return self.base + path

    def _request(self, method: str, path: str, json_payload: Optional[Dict[str, Any]] = None) -> requests.Response:
        url = self._url(path)
        headers = {}
        auth = None
        if self._token:
            headers["X-Auth-Token"] = self._token
        else:
            auth = HTTPBasicAuth(self.host.username, self.host.password)

        for attempt in range(self.retries + 1):
            try:
                return self.session.request(
                    method=method,
                    url=url,
                    headers=headers,
                    auth=auth,
                    json=json_payload,
                    verify=self.verify_tls,
                    timeout=self.timeout,
                )
            except SSLError as e:
                raise RedfishError(f"{self.host.name}: TLS/SSL failed: {e}") from e
            except (ConnectTimeout, ReadTimeout) as e:
                raise RedfishError(f"{self.host.name}: timeout talking to iLO: {e}") from e
            except ReqConnectionError as e:
                if attempt < self.retries:
                    time.sleep(0.25)
                    continue
                raise RedfishError(f"{self.host.name}: connection dropped/reset: {e}") from e
            except RequestException as e:
                raise RedfishError(f"{self.host.name}: request error: {e}") from e

        raise RedfishError(f"{self.host.name}: request failed unexpectedly")

    def get_json(self, path: str) -> Dict[str, Any]:
        r = self._request("GET", path)
        if r.status_code in (401, 403):
            raise RedfishError(f"{self.host.name}: auth failed ({r.status_code}).")
        if r.status_code >= 400:
            raise RedfishError(f"{self.host.name}: GET {path} failed ({r.status_code}): {r.text[:250]}")
        try:
            return r.json()
        except ValueError as e:
            raise RedfishError(f"{self.host.name}: invalid JSON from {path}") from e

    def post_json(self, path: str, payload: Dict[str, Any]) -> None:
        r = self._request("POST", path, json_payload=payload)
        if r.status_code in (401, 403):
            raise RedfishError(f"{self.host.name}: auth failed ({r.status_code}).")
        if r.status_code >= 400 and r.status_code != 204:
            raise RedfishError(f"{self.host.name}: POST {path} failed ({r.status_code}): {r.text[:250]}")

    def login_session(self) -> None:
        path = "/redfish/v1/SessionService/Sessions/"
        payload = {"UserName": self.host.username, "Password": self.host.password}
        try:
            r = self.session.post(
                self._url(path),
                auth=HTTPBasicAuth(self.host.username, self.host.password),
                json=payload,
                verify=self.verify_tls,
                timeout=self.timeout,
                headers={"Connection": "close"},
            )
        except RequestException:
            return
        token = r.headers.get("X-Auth-Token")
        if token:
            self._token = token

    def systems_id(self) -> str:
        if self._systems_id:
            return self._systems_id
        try:
            self.get_json("/redfish/v1/Systems/1")
            self._systems_id = "1"
            return "1"
        except RedfishError:
            systems = self.get_json("/redfish/v1/Systems")
            members = systems.get("Members", []) or []
            if not members:
                raise RedfishError(f"{self.host.name}: no Systems members found.")
            odata = members[0].get("@odata.id")
            if not odata:
                raise RedfishError(f"{self.host.name}: malformed Systems member.")
            self._systems_id = odata.rstrip("/").split("/")[-1]
            return self._systems_id

    def power_state(self) -> str:
        sid = self.systems_id()
        sysj = self.get_json(f"/redfish/v1/Systems/{sid}")
        return sysj.get("PowerState", "Unknown")

    def reset(self, reset_type: str) -> None:
        sid = self.systems_id()
        action_path = f"/redfish/v1/Systems/{sid}/Actions/ComputerSystem.Reset"
        self.post_json(action_path, {"ResetType": reset_type})

    def power_on(self) -> None:
        self.reset("On")

    def graceful_shutdown(self) -> None:
        self.reset("GracefulShutdown")

    def force_off(self) -> None:
        self.reset("ForceOff")


# ----------------------------
# Shell helpers
# ----------------------------

def _run_cmd(cmd: List[str], input_text: Optional[str] = None) -> Tuple[int, str, str]:
    p = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    out, err = p.communicate(input=input_text)
    return p.returncode, out, err


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


# ----------------------------
# Time sync preflight (chrony first)
# ----------------------------

@dataclass
class TimeSyncStatus:
    backend: str
    ok: bool
    summary: str
    details: str


def check_time_sync_status() -> TimeSyncStatus:
    # 1) chrony
    if _have("chronyc"):
        rc, out, err = _run_cmd(["chronyc", "tracking"])
        if rc == 0 and out.strip():
            # Simple heuristic: "Leap status     : Normal" + "Stratum" present
            ok = ("Leap status" in out and "Normal" in out) or ("Reference ID" in out)
            summary = "chrony tracking looks OK" if ok else "chrony present but tracking looks off"
            details = out.strip()
            return TimeSyncStatus("chrony", ok, summary, details)
        return TimeSyncStatus("chrony", False, "chrony present but tracking failed", (err.strip() or out.strip()))

    # 2) systemd-timesyncd / timedatectl
    if _have("timedatectl"):
        rc, out, err = _run_cmd(["timedatectl", "show", "-p", "NTPSynchronized", "-p", "NTP", "-p", "Timezone", "-p", "TimeUSec"])
        if rc == 0 and out.strip():
            # Parse NTPSynchronized=yes
            ntp_sync = "NTPSynchronized=yes" in out
            ntp_enabled = "NTP=yes" in out
            ok = ntp_enabled and ntp_sync
            summary = "timedatectl reports NTP synced" if ok else "timedatectl reports NOT synced"
            return TimeSyncStatus("timedatectl", ok, summary, out.strip())
        return TimeSyncStatus("timedatectl", False, "timedatectl failed", (err.strip() or out.strip()))

    # 3) ntpq
    if _have("ntpq"):
        rc, out, err = _run_cmd(["ntpq", "-p"])
        if rc == 0 and out.strip():
            # crude: look for a line starting with '*' indicating current sync source
            ok = any(line.startswith("*") for line in out.splitlines())
            summary = "ntpq shows a selected sync source" if ok else "ntpq shows no selected sync source"
            return TimeSyncStatus("ntpq", ok, summary, out.strip())
        return TimeSyncStatus("ntpq", False, "ntpq present but failed", (err.strip() or out.strip()))

    return TimeSyncStatus("none", False, "No time-sync tooling found (chronyc/timedatectl/ntpq)", "")


def print_time_sync_report() -> TimeSyncStatus:
    st = check_time_sync_status()
    print("\nTime sync preflight:")
    print(f"  Backend: {st.backend}")
    print(f"  OK: {st.ok}")
    print(f"  Summary: {st.summary}")
    if st.details:
        print("\nDetails:\n" + st.details)
    else:
        print("\nDetails: (none)")
    return st


# ----------------------------
# Cron management (user crontab)
# ----------------------------

MARKER = "ILO_SCHEDULER"

def crontab_read() -> str:
    rc, out, err = _run_cmd(["crontab", "-l"])
    if rc != 0:
        return ""
    return out

def crontab_write(content: str) -> None:
    rc, out, err = _run_cmd(["crontab", "-"], input_text=content)
    if rc != 0:
        raise RuntimeError(f"Failed to write crontab: {err.strip() or out.strip()}")

def cron_list_jobs() -> List[Dict[str, str]]:
    tab = crontab_read().splitlines()
    jobs = []
    for line in tab:
        if line.strip().startswith(f"# {MARKER} "):
            parts = line.strip().split(maxsplit=3)
            if len(parts) >= 4:
                jobs.append({"job_id": parts[2], "job_name": parts[3]})
    return jobs

def cron_delete_job(job_id: str) -> int:
    tab = crontab_read().splitlines()
    new_lines = []
    removed = 0
    skip_next = False

    for line in tab:
        if skip_next:
            removed += 1
            skip_next = False
            continue

        if line.strip().startswith(f"# {MARKER} ") and job_id in line:
            removed += 1
            skip_next = True
            continue

        new_lines.append(line)

    crontab_write("\n".join(new_lines).rstrip() + ("\n" if new_lines else ""))
    return removed

def cron_add_job(job_id: str, job_name: str, cron_expr: str, command: str) -> None:
    tab = crontab_read().rstrip("\n")
    lines = tab.splitlines() if tab else []
    lines.append(f"# {MARKER} {job_id} {job_name}")
    lines.append(f"{cron_expr} {command}")
    crontab_write("\n".join(lines).rstrip() + "\n")


# ----------------------------
# Scheduling helpers
# ----------------------------

def pick_cron_expression_interactive() -> str:
    print("\nPick schedule type:")
    print("  1) Daily at HH:MM")
    print("  2) Weekly (day + HH:MM)")
    print("  3) Custom cron expression (min hour dom mon dow)")
    choice = input("> ").strip()

    if choice == "1":
        hhmm = input("Enter time (HH:MM, 24h): ").strip()
        hh, mm = hhmm.split(":")
        return f"{int(mm)} {int(hh)} * * *"
    if choice == "2":
        print("Day of week: 0=Sun 1=Mon 2=Tue 3=Wed 4=Thu 5=Fri 6=Sat")
        dow = int(input("Enter day number (0-6): ").strip())
        hhmm = input("Enter time (HH:MM, 24h): ").strip()
        hh, mm = hhmm.split(":")
        return f"{int(mm)} {int(hh)} * * {dow}"
    if choice == "3":
        expr = input("Enter cron expression (min hour dom mon dow): ").strip()
        if len(expr.split()) != 5:
            print("That doesn't look like 5 fields. Try again.")
            return pick_cron_expression_interactive()
        return expr

    print("Invalid choice.")
    return pick_cron_expression_interactive()


def choose_targets_interactive(hosts: List[IloHost]) -> List[str]:
    print("\nTargets:")
    for idx, h in enumerate(hosts, start=1):
        print(f"  {idx}) {h.name} ({h.host})")
    print("  a) all hosts")
    raw = input("> ").strip().lower()

    if raw == "a":
        return [h.name for h in hosts]

    parts = [p.strip() for p in raw.split(",") if p.strip()]
    selected: List[str] = []
    for p in parts:
        if p.isdigit():
            i = int(p)
            if 1 <= i <= len(hosts):
                selected.append(hosts[i - 1].name)
    if not selected:
        print("No valid targets selected.")
        return choose_targets_interactive(hosts)
    return selected


# ----------------------------
# Run actions (used by cron)
# ----------------------------

def run_start(hosts: List[IloHost], target_names: List[str], verify_tls: bool, timeout: int, retries: int) -> int:
    targets = [h for h in hosts if h.name in target_names]
    if not targets:
        print(f"No matching targets for: {target_names}")
        return 2

    if not verify_tls:
        requests.packages.urllib3.disable_warnings()  # type: ignore

    failures = 0
    for h in targets:
        client = IloRedfishClient(h, verify_tls=verify_tls, timeout=timeout, retries=retries)
        try:
            client.login_session()
            client.power_on()
            print(f"{h.name}: power on sent.")
        except RedfishError as e:
            failures += 1
            print(f"ERROR: {e}")
    return 1 if failures else 0


def run_shutdown(hosts: List[IloHost], target_names: List[str], verify_tls: bool, timeout: int, retries: int) -> int:
    targets = [h for h in hosts if h.name in target_names]
    if not targets:
        print(f"No matching targets for: {target_names}")
        return 2

    if not verify_tls:
        requests.packages.urllib3.disable_warnings()  # type: ignore

    failures = 0
    wait_s = int(os.environ.get("SHUTDOWN_GRACE_PERIOD_SEC", "600"))

    for h in targets:
        client = IloRedfishClient(h, verify_tls=verify_tls, timeout=timeout, retries=retries)
        try:
            client.login_session()
            state = client.power_state()
            print(f"{h.name}: power state before shutdown: {state}")
            if state.lower() != "on":
                print(f"{h.name}: already not On, skipping shutdown.")
                continue

            print(f"{h.name}: sending GracefulShutdown...")
            client.graceful_shutdown()

            print(f"{h.name}: waiting {wait_s}s before force-off check...")
            time.sleep(wait_s)

            state2 = client.power_state()
            print(f"{h.name}: power state after grace period: {state2}")
            if state2.lower() == "on":
                print(f"{h.name}: still On. sending ForceOff...")
                client.force_off()
            else:
                print(f"{h.name}: shutdown succeeded without force-off.")
        except RedfishError as e:
            failures += 1
            print(f"ERROR: {e}")
    return 1 if failures else 0


# ----------------------------
# Interactive menu
# ----------------------------

def _require_time_sync_or_override() -> bool:
    st = print_time_sync_report()
    if st.ok:
        return True
    print("\nTime sync does NOT look OK.")
    print("Creating cron schedules anyway is how you end up powering off servers at 3:00pm instead of 3:00am.")
    ans = input("Type OVERRIDE to proceed anyway, or anything else to cancel: ").strip()
    return ans == "OVERRIDE"


def interactive_menu() -> None:
    load_dotenv(".env")
    hosts = load_hosts_from_env()

    verify_tls = _str_to_bool(os.environ.get("VERIFY_TLS", "false"), default=False)
    timeout = int(os.environ.get("REQUEST_TIMEOUT", "6"))
    retries = int(os.environ.get("REQUEST_RETRIES", "1"))

    script_path = os.path.abspath(sys.argv[0])
    python_path = sys.executable

    while True:
        print("\nScheduler menu:")
        print("  1) Time sync preflight (chrony -> timedatectl -> ntpq)")
        print("  2) List scheduled jobs")
        print("  3) Create STARTUP schedule")
        print("  4) Create SHUTDOWN schedule (graceful then force after 10m)")
        print("  5) Delete a scheduled job")
        print("  6) Test-run STARTUP now")
        print("  7) Test-run SHUTDOWN now")
        print("  q) Quit")
        choice = input("> ").strip().lower()

        if choice == "q":
            return

        if choice == "1":
            print_time_sync_report()
            continue

        if choice == "2":
            jobs = cron_list_jobs()
            if not jobs:
                print("\nNo scheduler jobs found.")
            else:
                print("\nScheduler jobs:")
                for j in jobs:
                    print(f"  - {j['job_id']}  {j['job_name']}")
            continue

        if choice in ("3", "4"):
            if not _require_time_sync_or_override():
                print("Cancelled.")
                continue

            targets = choose_targets_interactive(hosts)
            cron_expr = pick_cron_expression_interactive()

            action = "start" if choice == "3" else "shutdown"
            job_id = uuid.uuid4().hex[:10]
            job_name = f"{action.upper()} targets={','.join(targets)}"

            targets_json = json.dumps(targets)
            cmd = f"{shlex.quote(python_path)} {shlex.quote(script_path)} --run {action} --targets {shlex.quote(targets_json)}"

            cron_add_job(job_id, job_name, cron_expr, cmd)
            print(f"\nAdded job {job_id}: {job_name}")
            print(f"Cron: {cron_expr} {cmd}")
            continue

        if choice == "5":
            jobs = cron_list_jobs()
            if not jobs:
                print("\nNo scheduler jobs to delete.")
                continue
            print("\nJobs:")
            for j in jobs:
                print(f"  - {j['job_id']}  {j['job_name']}")
            jid = input("Enter job_id to delete: ").strip()
            removed = cron_delete_job(jid)
            if removed:
                print(f"Deleted job {jid} (removed {removed} lines).")
            else:
                print("Job not found.")
            continue

        if choice in ("6", "7"):
            targets = choose_targets_interactive(hosts)
            if choice == "6":
                rc = run_start(hosts, targets, verify_tls, timeout, retries)
            else:
                rc = run_shutdown(hosts, targets, verify_tls, timeout, retries)
            print(f"\nTest run finished with exit code: {rc}")
            continue

        print("Invalid choice.")


# ----------------------------
# CLI entry for cron
# ----------------------------

def parse_args(argv: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"mode": "interactive", "action": None, "targets": None}
    if "--run" in argv:
        i = argv.index("--run")
        if i + 1 >= len(argv):
            raise ValueError("Missing action after --run (start|shutdown)")
        out["mode"] = "run"
        out["action"] = argv[i + 1].strip().lower()

    if "--targets" in argv:
        i = argv.index("--targets")
        if i + 1 >= len(argv):
            raise ValueError("Missing JSON after --targets")
        out["targets"] = argv[i + 1]

    return out


def main() -> int:
    args = parse_args(sys.argv[1:])
    load_dotenv(".env")

    try:
        hosts = load_hosts_from_env()
    except ValueError as e:
        print(f"Config error: {e}")
        return 2

    verify_tls = _str_to_bool(os.environ.get("VERIFY_TLS", "false"), default=False)
    timeout = int(os.environ.get("REQUEST_TIMEOUT", "6"))
    retries = int(os.environ.get("REQUEST_RETRIES", "1"))

    if args["mode"] == "interactive":
        interactive_menu()
        return 0

    action = args["action"]
    if action not in ("start", "shutdown"):
        print("Invalid --run action. Use start or shutdown.")
        return 2

    targets_json = args["targets"]
    if not targets_json:
        print("Missing --targets.")
        return 2

    try:
        targets = json.loads(targets_json)
        if not isinstance(targets, list) or not all(isinstance(x, str) for x in targets):
            raise ValueError()
    except Exception:
        print("Invalid --targets JSON. Expected: [\"server-1\",\"server-2\"]")
        return 2

    if action == "start":
        return run_start(hosts, targets, verify_tls, timeout, retries)
    return run_shutdown(hosts, targets, verify_tls, timeout, retries)


if __name__ == "__main__":
    raise SystemExit(main())
