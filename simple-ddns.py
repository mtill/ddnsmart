#!/usr/bin/env python3
# -*- coding: utf-8 -*-


import time
import socket
import logging
import threading
import urllib.request
import json
import sys
import pathlib
from pyroute2 import IPRoute
from pyroute2.netlink import rtnl
from pyroute2.netlink.rtnl import ifaddrmsg


# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)


class DDNSInterface:
    def __init__(self, interface_name, ddns_services, check_interval):
        self.interface_name = interface_name
        self.ddns_services = ddns_services
        self.check_interval = check_interval

        with IPRoute() as ipr:
            links = ipr.link_lookup(ifname=interface_name)
            if not links:
                raise ValueError(f"Error: Interface {interface_name} not found.")
            self.interface_index = links[0]

    def get_current_ipv6(self):
        with IPRoute() as ipr:
            # Get all IPv6 addresses for the interface
            addresses = ipr.get_addr(index=self.interface_index, family=socket.AF_INET6, scope=0)

            for msg in addresses:
                address = self.parse_ipv6_address(msg=msg)
                if address is not None:
                    return address

        return None

    def parse_ipv6_address(self, msg):
        # 1. Filter for IPv6
        if msg.get('family') == socket.AF_INET6 and msg['event'] == "RTM_NEWADDR":
            if msg.get('index') != self.interface_index:
                return None

            #Summary of Flags to watch:
            #IFA_F_TENTATIVE: Address is being verified. Do not use yet.
            #IFA_F_PERMANENT: This is a standard static or EUI-64 address.
            #IFA_F_TEMPORARY: This is a Privacy Extension address (rotates frequently).
            #IFA_F_DEPRECATED: The address is deprecated (usually happens before a prefix change).

            flags = msg.get_attr('IFA_FLAGS')
            # If the kernel didn't provide IFA_FLAGS (rare for IPv6), 
            # fall back to the header flags
            if flags is None:
                flags = msg['flags']

            logging.debug(f"Received address event: {msg.get_attr('IFA_ADDRESS')} with flags {msg.get_attr('IFA_FLAGS')}")

            # Select ONLY MAC-derived addresses (EUI-64):
            # Must have PERMANENT flag and NOT have TEMPORARY flag
            #if not (flags & ifaddrmsg.IFA_F_PERMANENT):
            #    return None

            if flags & (ifaddrmsg.IFA_F_TEMPORARY | 
                        ifaddrmsg.IFA_F_TENTATIVE |
                        ifaddrmsg.IFA_F_DEPRECATED |
                        ifaddrmsg.IFA_F_DADFAILED):
                return None

            # 2. Filter for Global Scope (Public addresses)
            # Scope 0 is 'universe' (global), 253 is 'link'
            if msg.get('scope') == 0:
                result = msg.get_attr('IFA_ADDRESS')
                if not result.startswith('fdde:'):
                    return result

        return None

    def _propagate_update(self, new_ip, reason="Change", force_update=False):
        for ddns_service in self.ddns_services:
            if ddns_service.update_timer is not None and ddns_service.update_timer.is_alive():
                ddns_service.update_timer.cancel()
            # 15s delay to ensure the address is fully 'preferred' by the OS
            ddns_service.update_timer = threading.Timer(15, ddns_service.update_ip, args=(new_ip, reason, force_update))
            ddns_service.update_timer.start()

    def monitor_loop(self):
        with IPRoute() as ipr:
            ipr.bind()
            logging.info("Monitoring Public IPv6 address changes...")

            while True:
                for msg in ipr.get():
                    address = self.parse_ipv6_address(msg=msg)
                    if address is not None:
                        self._propagate_update(new_ip=address, reason="Kernel Event", force_update=False)

    def check_ip_loop(self):
        logging.info(f"Interface daemon running. IP check every ~{self.check_interval} seconds.")
        while True:
            time.sleep(self.check_interval)
            current_ip = self.get_current_ipv6()
            if current_ip is not None:
                if current_ip != self.last_ip:
                    logging.info("We missed an IP address change since last update.")
                    self._propagate_update(new_ip=current_ip, reason="missed", force_update=False)

    def run(self):
        # 1. Start Kernel Listener in background
        threading.Thread(target=self.monitor_loop, daemon=True).start()

        # 2. Initial sync
        initial_ip = self.get_current_ipv6()
        if initial_ip is not None:
            self._propagate_update(new_ip=initial_ip, reason="Startup Sync", force_update=False)

        # 3. double-check "manually" whether ip address changed, to avoid missing updates if the kernel event is somehow delayed or missed
        threading.Thread(target=self.check_ip_loop, daemon=True).start()


class DDNSService:
    def __init__(self, name, user_agent, state_file, heartbeat_interval, update_url, retry_delay):
        self.lock = threading.Lock()
        
        # Configuration with defaults
        self.name = name
        self.user_agent = user_agent
        self.state_file = state_file
        self.heartbeat_interval = heartbeat_interval
        self.update_url = update_url
        self.retry_delay = retry_delay

        # State tracking
        self.last_ip = self._load_last_ip()
        self.last_update_time = time.time()

        self.update_timer = None
        self.heartbeat_timer = None
        self._restart_heartbeat()
        logging.info(f"[{self.name}] Heartbeat daemon running. Sending force-updates every ~{self.heartbeat_interval} seconds.")

    def _send_heartbeat(self):
        self.update_ip(self.last_ip, reason="Heartbeat", force_update=True)
        self._restart_heartbeat()

    def _restart_heartbeat(self):
        if self.heartbeat_timer is not None and self.heartbeat_timer.is_alive():
            self.heartbeat_timer.cancel()
        self.heartbeat_timer = threading.Timer(self.heartbeat_interval, self._send_heartbeat)
        self.heartbeat_timer.start()

    def _load_last_ip(self):
        try:
            with open(self.state_file, "r") as f:
                return f.read().strip()
        except (FileNotFoundError, AttributeError):
            return None

    def _response_ok(self, result: str) -> bool:
        """Return True if the DDNS provider response indicates success."""
        if not result:
            return False
        normalized = result.strip().lower()
        # Common success indicators (depends on provider)
        if normalized.startswith(("good", "ok", "success", "nochg")):
            return True
        # Treat explicit failure hints as failure
        if "error" in normalized or "fail" in normalized or normalized.startswith(("911", "bad", "ko")):
            return False
        # Default to success for unknown responses (avoid unnecessary retries)
        return True

    def update_ip(self, new_ip, reason="Change", force_update=False):
        """Thread-safe update with state persistence."""
        with self.lock:
            self._restart_heartbeat()  # Reset heartbeat timer on any update attempt
            now = time.time()
            # Skip if IP is identical unless it's a forced update
            if new_ip == self.last_ip and not force_update:
                return

            update_url = self.update_url.replace("<ipv6address>", new_ip)
            try:
                logging.info(f"[{self.name}] Updating DDNS ({reason}): {new_ip}")
                req = urllib.request.Request(
                    f"{update_url}", 
                    headers={'User-Agent': self.user_agent}
                )

                with urllib.request.urlopen(req, timeout=10) as resp:
                    result = resp.read().decode('utf-8').strip()
                    logging.info(f"[{self.name}] API Response: {result}")

                    if not self._response_ok(result):
                        raise ValueError(f"[{self.name}] Negative response from provider: {result}")

                    # Persist state
                    self.last_ip = new_ip
                    self.last_update_time = now
                    with open(self.state_file, "w") as f:
                        f.write(new_ip)

            except Exception as e:
                logging.error(f"[{self.name}] API Update Failed: {e}")
                if self.update_timer is not None and self.update_timer.is_alive():
                    self.update_timer.cancel()
                self.update_timer = threading.Timer(self.retry_delay, self.update_ip, args=(new_ip, "retry", False))
                self.update_timer.start()


if __name__ == "__main__":
    config_dir = pathlib.Path(__file__).parent / "config" if len(sys.argv) == 1 else pathlib.Path(sys.argv[1])
    logging.info(f"Starting simple-ddns with config directory: {config_dir}")
    if not config_dir.is_dir():
        logging.error(f"Config directory '{config_dir}' not found.")
        sys.exit(1)

    for config_file in config_dir.glob("*.json"):
        config_path = config_file.resolve()

        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)

        ddns_services = []
        for provider_name, conf_ddns_provider in config.get("ddns_providers", {}).items():
            last_ip_filename = pathlib.Path("/var/tmp") / ("ddnsmart_" + provider_name + "_ipv6.txt")
            ddns_service = DDNSService(name=provider_name,
                                       user_agent=conf_ddns_provider.get("user_agent", "ddclient/3.10.0"),
                                       state_file=conf_ddns_provider.get("state_file", last_ip_filename),
                                       heartbeat_interval=conf_ddns_provider.get("heartbeat_interval", 86400),
                                       update_url=conf_ddns_provider.get("update_url", ""),
                                       retry_delay=conf_ddns_provider.get("retry_delay", 300))
            ddns_services.append(ddns_service)

        ddns_interface = DDNSInterface(interface_name=config.get("interface_name"), ddns_services=ddns_services, check_interval=config.get("check_interval", 900))
        ddns_interface.run()

