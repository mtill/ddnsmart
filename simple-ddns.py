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
import signal
import atexit
import tempfile
from pyroute2 import IPRoute
from pyroute2.netlink.rtnl import ifaddrmsg


# --- Logging Setup ---
_log_handlers = [logging.StreamHandler(sys.stdout)]
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=_log_handlers
)


class DDNSInterface:
    def __init__(self, interface_name, ddns_services, check_interval):
        self.interface_name = interface_name
        self.ddns_services = ddns_services
        self.check_interval = check_interval
        self._shutdown = False  # Flag to gracefully stop threads
        self._threads = []  # Track threads for cleanup
        self._services_updated = threading.Event()  # Signal when services are added

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
        if msg.get('family') == socket.AF_INET6 and msg.get('event') == "RTM_NEWADDR":
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
                flags = msg.get('flags')
            
            # Guard against None flags (shouldn't happen but be safe)
            if flags is None:
                return None

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
                if result is not None and not result.startswith('fdde:'):
                    return result

        return None

    def _propagate_update(self, new_ip, reason="Change", force_update=False):
        for ddns_service in self.ddns_services:
            # 15s delay to ensure the address is fully 'preferred' by the OS
            ddns_service.schedule_update(new_ip, reason, force_update, delay=15)

    def monitor_loop(self):
        try:
            with IPRoute() as ipr:
                ipr.bind()
                logging.info("Monitoring Public IPv6 address changes...")

                while not self._shutdown:
                    for msg in ipr.get():
                        if self._shutdown:
                            break
                        address = self.parse_ipv6_address(msg=msg)
                        if address is not None:
                            self._propagate_update(new_ip=address, reason="Kernel Event", force_update=False)
        except Exception as e:
            logging.error(f"Monitor loop error for {self.interface_name}: {e}", exc_info=True)
            # Don't re-raise; let thread exit gracefully to avoid hanging monitor_thread.join()

    def check_ip_loop(self):
        logging.info(f"Interface daemon running. IP check every ~{self.check_interval} seconds.")
        try:
            while not self._shutdown:
                time.sleep(self.check_interval)
                if self._shutdown:
                    break
                current_ip = self.get_current_ipv6()
                if current_ip is not None:
                    self._propagate_update(new_ip=current_ip, reason="check-missed", force_update=False)
        except Exception as e:
            logging.error(f"Check IP loop error for {self.interface_name}: {e}", exc_info=True)
            # Don't re-raise; let thread exit gracefully to avoid hanging check_thread.join()

    def run(self):
        # Initial sync
        initial_ip = self.get_current_ipv6()
        if initial_ip is not None:
            self._propagate_update(new_ip=initial_ip, reason="Startup Sync", force_update=False)

        # Start Kernel Listener in background
        monitor_thread = threading.Thread(target=self.monitor_loop, daemon=True)
        monitor_thread.start()
        self._threads.append(monitor_thread)

        # double-check "manually" whether ip address changed, to avoid missing updates if the kernel event is somehow delayed or missed
        check_thread = threading.Thread(target=self.check_ip_loop, daemon=True)
        check_thread.start()
        self._threads.append(check_thread)
        # Note: threads run in background as daemon threads, don't block on join()

    def cleanup(self):
        """Cleanup resources."""
        # Signal threads to stop
        self._shutdown = True
        # Give threads time to exit gracefully
        for thread in self._threads:
            thread.join(timeout=30)
        # Cleanup DDNS services
        for ddns_service in self.ddns_services:
            ddns_service.cleanup()


class DDNSService:
    def __init__(self, name, user_agent, state_file, heartbeat_interval, update_url, retry_delay):
        # Validate required parameters
        if not update_url or not update_url.strip():
            raise ValueError(f"update_url cannot be empty for provider '{name}'")

        # Validate and constrain timing parameters
        if not isinstance(heartbeat_interval, int) or heartbeat_interval < 60:
            raise ValueError(f"heartbeat_interval must be >=60 seconds, got {heartbeat_interval}")
        if not isinstance(retry_delay, int) or retry_delay < 10:
            raise ValueError(f"retry_delay must be >=10 seconds, got {retry_delay}")

        # Configuration with defaults
        self.name = name
        self.user_agent = user_agent
        self.state_file = state_file
        self.heartbeat_interval = heartbeat_interval
        self.update_url = update_url
        self.retry_delay = retry_delay
        self.max_backoff = retry_delay * 32  # Max 32x backoff to prevent excessive delays

        # State tracking
        self.last_ip = self._load_last_ip()
        self.last_update_time = time.time()
        self._retry_pending = False  # Flag to track if retry is scheduled
        self._consecutive_failures = 0  # Track consecutive failures for exponential backoff

        self.timer_lock = threading.Lock()
        self.update_timer = None
        self.retry_timer = None
        self.heartbeat_timer = None

        self._schedule_next_heartbeat()
        logging.info(f"[{self.name}] Heartbeat daemon running. Sending force-updates every ~{self.heartbeat_interval} seconds.")

    def _send_heartbeat(self):
        # Only send heartbeat if we have a valid IP and no retry is pending
        if self.last_ip is not None and not self._retry_pending:
            self.update_ip(self.last_ip, reason="Heartbeat", force_update=True)
        # Schedule next heartbeat at the end to avoid re-entrance issues
        self._schedule_next_heartbeat()

    def schedule_update(self, new_ip, reason="Change", force_update=False, delay=15):
        """Thread-safe method to schedule an IP update with a delay."""
        with self.timer_lock:
            # Cancel any pending update timer
            if self.update_timer is not None and self.update_timer.is_alive():
                self.update_timer.cancel()
            # Cancel retry timer
            if self.retry_timer is not None and self.retry_timer.is_alive():
                self.retry_timer.cancel()
            # Cancel heartbeat to prevent overlapping updates during the delay window
            if self.heartbeat_timer is not None and self.heartbeat_timer.is_alive():
                self.heartbeat_timer.cancel()

            self.update_timer = threading.Timer(delay, self.update_ip, args=(new_ip, reason, force_update))
            self.update_timer.start()

    def _schedule_next_heartbeat(self):
        """Schedule next heartbeat without canceling current timer (avoids re-entrance)."""
        with self.timer_lock:
            self.heartbeat_timer = threading.Timer(self.heartbeat_interval, self._send_heartbeat)
            self.heartbeat_timer.start()

    def _restart_heartbeat(self):
        """Restart heartbeat by canceling current timer and scheduling new one."""
        with self.timer_lock:
            if self.heartbeat_timer is not None and self.heartbeat_timer.is_alive():
                self.heartbeat_timer.cancel()
            self.heartbeat_timer = threading.Timer(self.heartbeat_interval, self._send_heartbeat)
            self.heartbeat_timer.start()

    def _load_last_ip(self):
        try:
            with open(self.state_file, "r") as f:
                return f.read().strip()
        except (FileNotFoundError, OSError):
            return None

    def _do_retry(self, new_ip):
        """Perform retry with exponential backoff and clear the retry pending flag."""
        try:
            self.update_ip(new_ip, reason="retry", force_update=False)
        finally:
            # Clear retry pending flag under lock to prevent race conditions
            with self.timer_lock:
                self._retry_pending = False

    def cleanup(self):
        """Cancel all timers"""
        with self.timer_lock:
            if self.update_timer is not None and self.update_timer.is_alive():
                self.update_timer.cancel()
            if self.retry_timer is not None and self.retry_timer.is_alive():
                self.retry_timer.cancel()
            if self.heartbeat_timer is not None and self.heartbeat_timer.is_alive():
                self.heartbeat_timer.cancel()

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
        # Check state and determine if update is needed (with lock)
        with self.timer_lock:
            now = time.time()
            # Skip if IP is identical unless it's a forced update
            if new_ip is None or (new_ip == self.last_ip and not force_update):
                self._restart_heartbeat()
                return

            update_url = self.update_url.replace("<ipv6address>", new_ip)
        
        # Perform network I/O outside of lock to avoid lock contention
        result = None
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

            # Update state after successful response (with lock)
            with self.timer_lock:
                self.last_ip = new_ip
                self.last_update_time = now
                self._consecutive_failures = 0  # Reset failure counter on success
                # Use atomic write to prevent corruption
                try:
                    # Ensure parent directory exists
                    self.state_file.parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.NamedTemporaryFile(mode='w', dir=self.state_file.parent, 
                                                     delete=False, encoding='utf-8') as tmp:
                        tmp.write(new_ip)
                        tmp_path = tmp.name
                    # Atomic rename
                    pathlib.Path(tmp_path).replace(self.state_file)
                except OSError as e:
                    logging.error(f"[{self.name}] Failed to write state file: {e}")

        except Exception as e:
            logging.error(f"[{self.name}] API Update Failed: {e}")
            with self.timer_lock:
                self._consecutive_failures += 1  # Increment failure counter
                # Calculate backoff with exponential growth: retry_delay * (2 ^ failures)
                backoff_delay = min(self.retry_delay * (2 ** (self._consecutive_failures - 1)), self.max_backoff)
                self._retry_pending = True  # Mark retry as pending
                if self.retry_timer is not None and self.retry_timer.is_alive():
                    self.retry_timer.cancel()
                logging.info(f"[{self.name}] Scheduling retry in {backoff_delay}s (attempt {self._consecutive_failures})")
                self.retry_timer = threading.Timer(backoff_delay, self._do_retry, args=(new_ip,))
                self.retry_timer.start()
        finally:
            # Only restart heartbeat if not being called from heartbeat itself
            if reason != "Heartbeat":
                self._restart_heartbeat()


if __name__ == "__main__":
    # Global tracking for cleanup
    all_interfaces = []
    all_services = []

    def signal_handler(signum, frame):
        """Handle shutdown signals gracefully."""
        logging.info(f"Received signal {signum}, shutting down gracefully...")
        cleanup_on_exit()
        sys.exit(0)

    def cleanup_on_exit():
        """Cleanup on normal exit."""
        for interface in all_interfaces:
            interface.cleanup()
        for service in all_services:
            service.cleanup()
        logging.shutdown()  # Flush all log handlers

    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGHUP, signal_handler)
    atexit.register(cleanup_on_exit)

    config_dir = pathlib.Path(__file__).parent / "config" if len(sys.argv) == 1 else pathlib.Path(sys.argv[1])
    logging.info(f"Starting simple-ddns with config directory: {config_dir}")
    if not config_dir.is_dir():
        logging.error(f"Config directory '{config_dir}' not found.")
        sys.exit(1)

    for config_file in config_dir.glob("*.json"):
        try:
            config_path = config_file.resolve()

            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)

            check_interval = config.get("check_interval", 900)
            ddns_services = []
            for provider_name, conf_ddns_provider in config.get("ddns_providers", {}).items():
                last_ip_filename = pathlib.Path("/var/tmp") / ("ddnsmart_" + provider_name + "_ipv6.txt")
                state_file = pathlib.Path(conf_ddns_provider["state_file"]) if "state_file" in conf_ddns_provider else last_ip_filename
                ddns_service = DDNSService(name=provider_name,
                                            user_agent=conf_ddns_provider.get("user_agent", "ddclient/3.10.0"),
                                            state_file=state_file,
                                            heartbeat_interval=conf_ddns_provider.get("heartbeat_interval", 86400),
                                            update_url=conf_ddns_provider.get("update_url", ""),
                                            retry_delay=conf_ddns_provider.get("retry_delay", 300))
                ddns_services.append(ddns_service)
                all_services.append(ddns_service)

            if not ddns_services:
                logging.warning(f"No valid DDNS services configured in {config_path}")
                continue

            interface_name = config.get("interface_name")
            if not interface_name or not interface_name.strip():
                logging.error(f"Invalid or missing interface_name in {config_path}")
                continue

            try:
                ddns_interface = DDNSInterface(interface_name=interface_name, 
                                               ddns_services=ddns_services, 
                                               check_interval=check_interval)
                all_interfaces.append(ddns_interface)
                # Run threads in background (non-blocking)
                ddns_interface.run()
            except Exception as e:
                logging.error(f"Failed to initialize interface: {e}", exc_info=True)
        except json.JSONDecodeError as e:
            logging.error(f"Failed to parse config file {config_file}: {e}")
        except Exception as e:
            logging.error(f"Error processing config file {config_file}: {e}", exc_info=True)

