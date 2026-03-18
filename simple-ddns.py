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


class DDNSUpdater:
    def __init__(self, interface_name, state_file, heartbeat_interval, ddns_url):
        self.lock = threading.Lock()
        
        # Configuration with defaults
        self.interface_name = interface_name
        self.state_file = state_file
        self.heartbeat_interval = heartbeat_interval
        self.ddns_url = ddns_url

        # State tracking
        self.last_ip = self._load_last_ip()
        self.last_update_time = time.time()

        with IPRoute() as ipr:
            links = ipr.link_lookup(ifname=self.interface_name)
            if not links:
                raise ValueError(f"Error: Interface {self.interface_name} not found.")
            self.interface_index = links[0]

    def parse_ipv6_address(self, msg):
        # 1. Filter for IPv6
        if msg.get('family') == socket.AF_INET6 and msg['event'] == rtnl.RTM_NEWADDR:
            if msg.get('index') != self.interface_index:
                return None

            #Summary of Flags to watch:
            #IFA_F_TENTATIVE: Address is being verified. Do not use yet.
            #IFA_F_PERMANENT: This is a standard static or EUI-64 address.
            #IFA_F_TEMPORARY: This is a Privacy Extension address (rotates frequently).
            #IFA_F_DEPRECATED: The address is deprecated (usually happens before a prefix change).

            flags = msg.get_attr('IFA_FLAGS') or 0
            if flags & (ifaddrmsg.IFA_F_TEMPORARY | 
                        ifaddrmsg.IFA_F_TENTATIVE |
                        ifaddrmsg.IFA_F_OPTIMISTIC |
                        ifaddrmsg.IFA_F_DADFAILED):
                return None

            # 2. Filter for Global Scope (Public addresses)
            # Scope 0 is 'universe' (global), 253 is 'link'
            if msg.get('scope') == 0:
                return msg.get_attr('IFA_ADDRESS')

        return None

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

    def update_ip(self, new_ip, reason="Change"):
        """Thread-safe update with state persistence."""
        with self.lock:
            now = time.time()
            # Skip if IP is identical unless it's a forced heartbeat
            if new_ip == self.last_ip and reason != "Heartbeat":
                return

            try:
                logging.info(f"Updating DDNS ({reason}): {new_ip}")
                req = urllib.request.Request(
                    f"{self.ddns_url}{new_ip}", 
                    headers={'User-Agent': 'simple-ddns/1.0'}
                )

                with urllib.request.urlopen(req, timeout=10) as resp:
                    result = resp.read().decode('utf-8').strip()
                    logging.info(f"API Response: {result}")

                    if not self._response_ok(result):
                        raise ValueError(f"Negative response from provider: {result}")

                    # Persist state
                    self.last_ip = new_ip
                    self.last_update_time = now
                    with open(self.state_file, "w") as f:
                        f.write(new_ip)
            except Exception as e:
                logging.error(f"API Update Failed: {e}")

    def get_current_ipv6(self):
        with IPRoute() as ipr:
            # Get all IPv6 addresses for the interface
            addresses = ipr.get_addr(index=self.interface_index, family=socket.AF_INET6)

            for msg in addresses:
                address = self.parse_ipv6_address(msg=msg)
                if address is not None:
                    return address

        return None

    def monitor_loop(self):
        with IPRoute() as ipr:
            ipr.bind()
            print("Monitoring Public IPv6 address changes...")

            while True:
                for msg in ipr.get():
                    address = self.parse_ipv6_address(msg=msg)
                    if address is not None:
                        # 10s delay to ensure the address is fully 'preferred' by the OS
                        threading.Timer(10, self.update_ip, args=(address, "Kernel Event")).start()

    def run(self):
        # 1. Start Kernel Listener in background
        threading.Thread(target=self.monitor_loop, daemon=True).start()

        # 2. Initial sync
        initial_ip = self.get_current_ipv6()
        if initial_ip:
            self.update_ip(initial_ip, "Startup Sync")

        # 3. Main thread handles the configurable heartbeat sleep
        logging.info(f"Daemon running. Heartbeat check every {self.heartbeat_interval} seconds.")
        while True:
            time.sleep(self.heartbeat_interval)

            current_ip = self.get_current_ipv6()
            if current_ip is not None:
                self.update_ip(current_ip, "Heartbeat")


if __name__ == "__main__":
    config_dir = pathlib.Path(__file__).parent / "config" if len(sys.argv) == 1 else pathlib.Path(sys.argv[1])
    print("Starting simple-ddns with config directory:", config_dir)
    if not config_dir.is_dir():
        logging.error(f"Config directory '{config_dir}' not found.")
        sys.exit(1)

    for config_file in config_dir.glob("*.json"):
        config_path = config_file.resolve()

        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)

        updater = DDNSUpdater(interface_name=config.get("interface_name"),
                            state_file=config.get("state_file", "current_ipv6.txt"),
                            heartbeat_interval=config.get("heartbeat_interval", 86400),
                            ddns_url=config.get("ddns_url", ""))
        updater.run()

