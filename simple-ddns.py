#!/usr/bin/env python3
"""IPv6 DDNS Monitor — watches for IPv6 changes and updates DDNS providers."""

import json
import logging
import sys
import threading
import time
import socket
import tempfile
from pathlib import Path

import requests
from pyroute2 import IPRoute
from pyroute2.netlink.rtnl import RTMGRP_IPV6_IFADDR, ifaddrmsg

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(threadName)s] %(levelname)s %(message)s")
log = logging.getLogger(__name__)


class DdnsUpdater:
    """Manages DDNS provider updates with retry logic and state persistence."""

    def __init__(self, providers: list[dict], retry_interval: int, request_timeout: int,
                 max_retries: int, state_dir: Path | None):
        self._providers = providers
        self._base_retry = retry_interval
        self._max_retries = max_retries
        self._timeout = request_timeout
        self._state_dir = state_dir
        self._lock = threading.Lock()
        # provider name -> (next_retry_time, failure_count)
        self._pending: dict[str, tuple[float, int]] = {}
        self._last_ip: dict[str, str] = {}  # provider name -> last successful IP
        if state_dir:
            state_dir.mkdir(parents=True, exist_ok=True)
            for p in providers:
                saved = self._read_state(p["name"])
                if saved:
                    self._last_ip[p["name"]] = saved

    def _state_path(self, name: str) -> Path | None:
        return self._state_dir / f"{name}.ip" if self._state_dir else None

    def _read_state(self, name: str) -> str | None:
        path = self._state_path(name)
        if path and path.exists():
            return path.read_text().strip()
        return None

    def _write_state(self, name: str, ipv6: str) -> None:
        path = self._state_path(name)
        if path:
            try:
                tmp = tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False, suffix=".tmp")
                tmp.write(ipv6)
                tmp.close()
                Path(tmp.name).replace(path)
            except OSError as e:
                log.error("Failed to write state for %s: %s", name, e)

    def update_all(self, ipv6: str, force: bool = False) -> None:
        for p in self._providers:
            with self._lock:
                if not force and self._last_ip.get(p["name"]) == ipv6:
                    continue
                # Reset failure count when IP changes
                old = self._pending.get(p["name"])
                if old and self._last_ip.get(p["name"]) != ipv6:
                    self._pending.pop(p["name"], None)
            self._try_update(p, ipv6)

    def process_retries(self, ipv6: str) -> None:
        now = time.time()
        with self._lock:
            due = [name for name, (ts, _) in self._pending.items() if ts <= now]
        for name in due:
            p = next((p for p in self._providers if p["name"] == name), None)
            if p:
                self._try_update(p, ipv6)

    def _try_update(self, provider: dict, ipv6: str) -> None:
        name = provider["name"]
        if self._send_update(provider, ipv6):
            with self._lock:
                self._pending.pop(name, None)
                self._last_ip[name] = ipv6
            self._write_state(name, ipv6)
        else:
            with self._lock:
                _, fails = self._pending.get(name, (0, 0))
                fails += 1
                if fails <= self._max_retries:
                    delay = min(self._base_retry * (2 ** (fails - 1)), 86400)
                    self._pending[name] = (time.time() + delay, fails)
                    log.info("Retry %s in %ds (attempt %d/%d)", name, delay, fails, self._max_retries)
                else:
                    self._pending.pop(name, None)
                    log.warning("Max retries reached for %s, giving up", name)

    def _send_update(self, provider: dict, ipv6: str) -> bool:
        url = provider["update_url"].replace("{ipv6}", ipv6)
        method = provider.get("method", "GET").upper()
        auth = (provider["username"], provider["password"]) if provider.get("username") else None
        headers = provider.get("headers", {})
        try:
            resp = requests.request(method, url, headers=headers, auth=auth, timeout=self._timeout)
            if resp.ok:
                log.info("Updated %s -> %s", provider["name"], ipv6)
                return True
            log.warning("Update %s failed: HTTP %s", provider["name"], resp.status_code)
        except requests.RequestException as e:
            log.error("Update %s failed: %s", provider["name"], e)
        return False


class Ipv6Monitor:
    """Detects IPv6 address changes via netlink events and polling."""

    def __init__(self, interface: str | None, poll_interval: int, debounce_delay: float = 15.0):
        self._interface = interface
        self._poll_interval = poll_interval
        self._debounce_delay = debounce_delay
        self._debounce: threading.Timer | None = None
        self._lock = threading.Lock()
        self._current: str | None = None
        self._shutdown = threading.Event()
        self._netlink_ipr: IPRoute | None = None
        self._on_change: list = []

    @property
    def current(self) -> str | None:
        with self._lock:
            return self._current

    @property
    def shutdown(self) -> threading.Event:
        return self._shutdown

    def on_change(self, callback) -> None:
        self._on_change.append(callback)

    def stop(self) -> None:
        self._shutdown.set()
        # Close the netlink socket so ipr.get() unblocks
        if self._netlink_ipr:
            try:
                self._netlink_ipr.close()
            except Exception:
                pass

    def get_global_ipv6(self) -> str | None:
        with IPRoute() as ipr:
            if self._interface:
                idx = ipr.link_lookup(ifname=self._interface)
                if not idx:
                    return None
                addrs = ipr.get_addr(family=socket.AF_INET6, index=idx[0], scope=0)
            else:
                addrs = ipr.get_addr(family=socket.AF_INET6, scope=0)
            for addr in addrs:
                flags = addr.get_attr("IFA_FLAGS") or addr.get("flags") or 0
                if flags & (ifaddrmsg.IFA_F_TEMPORARY |
                            ifaddrmsg.IFA_F_TENTATIVE |
                            ifaddrmsg.IFA_F_DEPRECATED |
                            ifaddrmsg.IFA_F_DADFAILED):
                    continue
                val = addr.get_attr("IFA_ADDRESS")
                if val and not val.startswith(("fe80:", "fd")):
                    return val
        return None

    def set_if_changed(self, ipv6: str) -> bool:
        with self._lock:
            if ipv6 == self._current:
                return False
            self._current = ipv6
            # Debounce: absorb rapid netlink bursts before notifying
            if self._debounce:
                self._debounce.cancel()
            self._debounce = threading.Timer(self._debounce_delay, self._fire_callbacks, (ipv6,))
            self._debounce.daemon = True
            self._debounce.start()
        log.info("IPv6 changed: %s", ipv6)
        return True

    def set_immediate(self, ipv6: str) -> bool:
        """Set address and fire callbacks immediately (for startup seeding)."""
        with self._lock:
            if ipv6 == self._current:
                return False
            self._current = ipv6
        log.info("IPv6 set: %s", ipv6)
        self._fire_callbacks(ipv6)
        return True

    def _fire_callbacks(self, ipv6: str) -> None:
        for cb in self._on_change:
            cb(ipv6)

    def run_netlink(self) -> None:
        """Primary: listen for netlink address events."""
        log.info("Netlink monitor started")
        ipr = IPRoute()
        self._netlink_ipr = ipr
        try:
            ipr.bind(groups=RTMGRP_IPV6_IFADDR)
            while not self._shutdown.is_set():
                try:
                    for msg in ipr.get():
                        if msg.get("event") in ("RTM_NEWADDR", "RTM_DELADDR"):
                            ipv6 = self.get_global_ipv6()
                            if ipv6:
                                self.set_if_changed(ipv6)
                except Exception:
                    if self._shutdown.is_set():
                        break
                    log.exception("Netlink error, retrying in 5s")
                    time.sleep(5)
        finally:
            self._netlink_ipr = None
            ipr.close()

    def run_poll(self) -> None:
        """Safety net: poll at fixed intervals."""
        log.info("Poll monitor started (every %ds)", self._poll_interval)
        while not self._shutdown.is_set():
            self._shutdown.wait(self._poll_interval)
            if self._shutdown.is_set():
                break
            ipv6 = self.get_global_ipv6()
            if ipv6:
                self.set_if_changed(ipv6)


class Ipv6DdnsService:
    """Ties monitoring, updating, retries, and heartbeats together."""

    def __init__(self, config_path: Path):
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)

        retry_interval = cfg.get("retry_interval", 900)
        self._heartbeat_interval = cfg.get("heartbeat_interval", 86400)
        state_dir = Path(cfg["state_dir"]) if cfg.get("state_dir") else None

        self._monitor = Ipv6Monitor(
            interface=cfg.get("monitored_interface"),
            poll_interval=cfg.get("poll_interval", 1800),
            debounce_delay=cfg.get("debounce_delay", 15.0),
        )
        self._updater = DdnsUpdater(
            providers=cfg.get("providers", []),
            retry_interval=retry_interval,
            request_timeout=cfg.get("request_timeout", 30),
            max_retries=cfg.get("max_retries", 5),
            state_dir=state_dir,
        )
        self._monitor.on_change(self._updater.update_all)
        log.info("Loaded %s (%d providers)", config_path, len(cfg.get("providers", [])))

    def run(self) -> None:
        # seed current address
        ipv6 = self._monitor.get_global_ipv6()
        if ipv6:
            log.info("Initial IPv6: %s", ipv6)
            self._monitor.set_immediate(ipv6)
        else:
            log.warning("No global IPv6 at startup")

        threads = [
            threading.Thread(target=self._monitor.run_netlink, name="netlink", daemon=True),
            threading.Thread(target=self._monitor.run_poll, name="poller", daemon=True),
            threading.Thread(target=self._retry_and_heartbeat, name="retry-hb", daemon=True),
        ]
        for t in threads:
            t.start()
        try:
            self._monitor.shutdown.wait()
        except KeyboardInterrupt:
            log.info("Shutting down")
            self._monitor.stop()
            for t in threads:
                t.join(timeout=10)

    def _retry_and_heartbeat(self) -> None:
        last_hb = time.time()
        while not self._monitor.shutdown.is_set():
            self._monitor.shutdown.wait(60)
            if self._monitor.shutdown.is_set():
                break
            ipv6 = self._monitor.current
            if not ipv6:
                continue
            self._updater.process_retries(ipv6)
            if time.time() - last_hb >= self._heartbeat_interval:
                last_hb = time.time()
                log.info("Heartbeat: %s", ipv6)
                self._updater.update_all(ipv6, force=True)


def main() -> None:
    path = Path(sys.argv[1])
    if path.exists():
        Ipv6DdnsService(path).run()
    else:
        log.error("Config not found: %s", path)
        sys.exit(1)


if __name__ == "__main__":
    main()

