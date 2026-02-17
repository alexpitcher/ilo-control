#!/usr/bin/env python3
"""
Interactive iLO 5 Redfish controller (fixed/robust):
- Reads power state, system health, fan speeds
- Power On
- Graceful shutdown (ResetType=GracefulShutdown)
- Hard shutdown (ResetType=ForceOff)

Fixes included:
- .env supports multi-line JSON values
- Better error messages for TLS/timeout/reset/disconnect
- Avoid keep-alive pooling issues with iLO (Connection: close)
- Prefer Redfish session token auth (fallback to Basic)
- Simple retry on transient disconnects
"""

import os
import sys
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

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

        # Multi-line JSON support for values that start with [ or {
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
# Redfish client
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

        # iLO sometimes gets weird with keep-alive reuse. Force close.
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "ilo-ctl/1.1",
            "Connection": "close",
        })

        self._token: Optional[str] = None
        self._systems_id: Optional[str] = None
        self._chassis_id: Optional[str] = None

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return self.base + path

    def _request(self, method: str, path: str, json_payload: Optional[Dict[str, Any]] = None) -> requests.Response:
        url = self._url(path)
        last_exc: Optional[Exception] = None

        # Per-request session header set (token vs basic)
        headers = {}
        auth = None
        if self._token:
            headers["X-Auth-Token"] = self._token
        else:
            auth = HTTPBasicAuth(self.host.username, self.host.password)

        for attempt in range(self.retries + 1):
            try:
                r = self.session.request(
                    method=method,
                    url=url,
                    headers=headers,
                    auth=auth,
                    json=json_payload,
                    verify=self.verify_tls,
                    timeout=self.timeout,
                )
                return r
            except SSLError as e:
                raise RedfishError(f"{self.host.name}: TLS/SSL failed: {e}") from e
            except (ConnectTimeout, ReadTimeout) as e:
                raise RedfishError(f"{self.host.name}: timeout talking to iLO: {e}") from e
            except ReqConnectionError as e:
                # This is where RemoteDisconnected usually lands.
                last_exc = e
                if attempt < self.retries:
                    time.sleep(0.25)
                    continue
                raise RedfishError(f"{self.host.name}: connection dropped/reset: {e}") from e
            except RequestException as e:
                raise RedfishError(f"{self.host.name}: request error: {e}") from e

        # Should never hit
        raise RedfishError(f"{self.host.name}: request failed: {last_exc}")

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

    def post_json(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        r = self._request("POST", path, json_payload=payload)

        if r.status_code in (401, 403):
            raise RedfishError(f"{self.host.name}: auth failed ({r.status_code}).")
        if r.status_code == 204 or not r.text.strip():
            return {}
        if r.status_code >= 400:
            raise RedfishError(f"{self.host.name}: POST {path} failed ({r.status_code}): {r.text[:250]}")

        try:
            return r.json()
        except ValueError:
            return {}

    def login_session(self) -> None:
        """
        Create a Redfish session and store X-Auth-Token.
        Some iLOs are happier with sessions than basic auth on every request.
        """
        # ServiceRoot shows session hint; iLO usually supports this endpoint:
        # POST /redfish/v1/SessionService/Sessions/ {"UserName":"...","Password":"..."}
        path = "/redfish/v1/SessionService/Sessions/"
        payload = {"UserName": self.host.username, "Password": self.host.password}

        # Session create requires no existing token; use basic auth for POST or no auth (iLO accepts either).
        # We'll use basic auth (harmless) and then switch to token.
        url = self._url(path)
        try:
            r = self.session.post(
                url,
                headers={"Accept": "application/json", "Content-Type": "application/json", "Connection": "close"},
                auth=HTTPBasicAuth(self.host.username, self.host.password),
                json=payload,
                verify=self.verify_tls,
                timeout=self.timeout,
            )
        except RequestException:
            # If session creation fails, we just keep basic auth fallback.
            return

        token = r.headers.get("X-Auth-Token")
        if token:
            self._token = token

    def discover_ids(self) -> None:
        # Discover Systems
        if self._systems_id is None:
            try:
                self.get_json("/redfish/v1/Systems/1")
                self._systems_id = "1"
            except RedfishError:
                systems = self.get_json("/redfish/v1/Systems")
                members = systems.get("Members", []) or []
                if not members:
                    raise RedfishError(f"{self.host.name}: no Systems members found.")
                odata = members[0].get("@odata.id")
                if not odata:
                    raise RedfishError(f"{self.host.name}: malformed Systems member.")
                self._systems_id = odata.rstrip("/").split("/")[-1]

        # Discover Chassis
        if self._chassis_id is None:
            try:
                self.get_json("/redfish/v1/Chassis/1")
                self._chassis_id = "1"
            except RedfishError:
                ch = self.get_json("/redfish/v1/Chassis")
                members = ch.get("Members", []) or []
                if not members:
                    raise RedfishError(f"{self.host.name}: no Chassis members found.")
                odata = members[0].get("@odata.id")
                if not odata:
                    raise RedfishError(f"{self.host.name}: malformed Chassis member.")
                self._chassis_id = odata.rstrip("/").split("/")[-1]

    def get_power_and_health(self) -> Dict[str, Any]:
        self.discover_ids()
        sid = self._systems_id
        assert sid is not None

        sysj = self.get_json(f"/redfish/v1/Systems/{sid}")

        power_state = sysj.get("PowerState", "Unknown")
        status = sysj.get("Status", {}) or {}
        health = status.get("Health", "Unknown")
        state = status.get("State", "Unknown")

        model = sysj.get("Model") or sysj.get("SKU") or "Unknown"
        serial = sysj.get("SerialNumber") or sysj.get("UUID") or "Unknown"

        return {
            "power_state": power_state,
            "health": health,
            "state": state,
            "model": model,
            "serial": serial,
        }

    def get_fans(self) -> List[Dict[str, Any]]:
        self.discover_ids()
        cid = self._chassis_id
        assert cid is not None

        thermal = self.get_json(f"/redfish/v1/Chassis/{cid}/Thermal")
        fans = thermal.get("Fans", []) or []

        parsed: List[Dict[str, Any]] = []
        for f in fans:
            status = f.get("Status", {}) or {}
            entry = {
                "name": f.get("Name") or f.get("FanName") or "Fan",
                "health": status.get("Health", "Unknown"),
                "state": status.get("State", "Unknown"),
            }

            # Common readings:
            if "Reading" in f:
                entry["reading"] = f.get("Reading")
                entry["units"] = f.get("ReadingUnits") or "RPM"
            if "ReadingRPM" in f:
                entry["rpm"] = f.get("ReadingRPM")
            if "ReadingPercent" in f:
                entry["percent"] = f.get("ReadingPercent")

            # Some iLO OEM fields exist; we’ll just surface if present
            oem = f.get("Oem", {}) or {}
            if oem:
                entry["oem_keys"] = list(oem.keys())

            parsed.append(entry)

        return parsed

    def reset(self, reset_type: str) -> None:
        self.discover_ids()
        sid = self._systems_id
        assert sid is not None
        action_path = f"/redfish/v1/Systems/{sid}/Actions/ComputerSystem.Reset"
        self.post_json(action_path, {"ResetType": reset_type})

    def power_on(self) -> None:
        self.reset("On")

    def graceful_shutdown(self) -> None:
        self.reset("GracefulShutdown")

    def hard_shutdown(self) -> None:
        self.reset("ForceOff")


# ----------------------------
# UI
# ----------------------------

def print_host_summary(client: IloRedfishClient) -> None:
    info = client.get_power_and_health()
    fans = client.get_fans()

    print(f"\n[{client.host.name}] {info['model']}  Serial/UUID: {info['serial']}")
    print(f"  Power: {info['power_state']}   Health: {info['health']}   State: {info['state']}")

    if not fans:
        print("  Fans: (none returned)")
        return

    print("  Fans:")
    for f in fans:
        parts = []
        if "rpm" in f:
            parts.append(f"{f['rpm']} RPM")
        if "reading" in f:
            parts.append(f"{f['reading']} {f.get('units','')}".strip())
        if "percent" in f:
            parts.append(f"{f['percent']}%")

        reading = " | ".join(parts) if parts else "(no reading)"
        print(f"    - {f['name']}: {reading}   Health={f['health']} State={f['state']}")


def choose_host(hosts: List[IloHost]) -> Optional[IloHost]:
    print("\nSelect a host:")
    for idx, h in enumerate(hosts, start=1):
        print(f"  {idx}) {h.name} ({h.host})")
    print("  a) all hosts")
    print("  q) quit")

    choice = input("> ").strip().lower()
    if choice == "q":
        return None
    if choice == "a":
        return IloHost(name="__ALL__", host="__ALL__", username="", password="")
    if choice.isdigit():
        i = int(choice)
        if 1 <= i <= len(hosts):
            return hosts[i - 1]
    print("Invalid choice.")
    return choose_host(hosts)


def action_menu() -> str:
    print("\nActions:")
    print("  1) Status + Fans")
    print("  2) Power On")
    print("  3) Graceful Shutdown")
    print("  4) Hard Shutdown (Force Off)")
    print("  r) refresh menu")
    print("  q) back")
    return input("> ").strip().lower()


def confirm_danger(action: str) -> bool:
    if action not in ("3", "4"):
        return True
    if action == "3":
        typed = input("Type SHUTDOWN to send graceful shutdown: ").strip()
        return typed == "SHUTDOWN"
    typed = input("Type FORCEOFF to kill power (yes, really): ").strip()
    return typed == "FORCEOFF"


def run_for_client(client: IloRedfishClient, action: str) -> None:
    if action == "1":
        print_host_summary(client)
        return
    if action == "2":
        client.power_on()
        print(f"{client.host.name}: power on sent.")
        return
    if action == "3":
        client.graceful_shutdown()
        print(f"{client.host.name}: graceful shutdown sent.")
        return
    if action == "4":
        client.hard_shutdown()
        print(f"{client.host.name}: HARD shutdown sent (ForceOff).")
        return
    print("Unknown action.")


# ----------------------------
# Main
# ----------------------------

def main() -> int:
    load_dotenv(".env")

    try:
        hosts = load_hosts_from_env()
    except ValueError as e:
        print(f"Config error: {e}")
        print("Fix your .env, then rerun.")
        return 2

    verify_tls = _str_to_bool(os.environ.get("VERIFY_TLS", "false"), default=False)
    timeout = int(os.environ.get("REQUEST_TIMEOUT", "6"))
    retries = int(os.environ.get("REQUEST_RETRIES", "1"))

    if not verify_tls:
        # silence urllib3 warnings about self-signed certs
        requests.packages.urllib3.disable_warnings()  # type: ignore

    while True:
        target = choose_host(hosts)
        if target is None:
            return 0

        selected = hosts if target.name == "__ALL__" else [target]

        while True:
            action = action_menu()
            if action == "q":
                break
            if action == "r":
                continue
            if not confirm_danger(action):
                print("Cancelled.")
                continue

            for h in selected:
                client = IloRedfishClient(h, verify_tls=verify_tls, timeout=timeout, retries=retries)
                try:
                    # Prefer token session (fallback to basic if it fails)
                    client.login_session()
                    run_for_client(client, action)
                except RedfishError as e:
                    print(f"ERROR: {e}")

            time.sleep(0.15)


if __name__ == "__main__":
    raise SystemExit(main())
