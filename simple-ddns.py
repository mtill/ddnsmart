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
import tempfile
from pyroute2 import IPRoute
from pyroute2.netlink.rtnl import ifaddrmsg

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

# Global registry for cleanup
interfaces = []
services = []


class IPMonitor:
    """Monitor IPv6 address changes and trigger DDNS updates."""
    
    def __init__(self, interface_name, handlers, check_interval=900):
        with IPRoute() as ipr:
            links = ipr.link_lookup(ifname=interface_name)
            if not links:
                raise ValueError(f"Interface {interface_name} not found")
            self.interface_index = links[0]
        
        self.interface_name = interface_name
        self.handlers = handlers
        self.check_interval = check_interval
        self._stop = False
        self._threads = []
    
    def _get_ipv6(self):
        """Get current public IPv6 address."""
        try:
            with IPRoute() as ipr:
                for msg in ipr.get_addr(index=self.interface_index, family=socket.AF_INET6, scope=0):
                    addr = self.parse_ipv6_address(msg, check_event=False)
                    if addr:
                        return addr
        except Exception as e:
            logging.error(f"Error getting IPv6 for {self.interface_name}: {e}")
        return None
    
    def parse_ipv6_address(self, msg, check_event=True):
        """Parse and validate IPv6 address from netlink message."""
        # Only check for RTM_NEWADDR event when processing live netlink events
        # Skip event check when parsing static address records from get_addr()
        if check_event and msg.get('event') != "RTM_NEWADDR":
            return None
        if msg.get('family') != socket.AF_INET6:
            return None
        if msg.get('index') != self.interface_index:
            return None
        
        flags = msg.get_attr('IFA_FLAGS') or msg.get('flags')
        if flags is None:
            return None
        
        if flags & (ifaddrmsg.IFA_F_TEMPORARY | ifaddrmsg.IFA_F_TENTATIVE |
                    ifaddrmsg.IFA_F_DEPRECATED | ifaddrmsg.IFA_F_DADFAILED):
            return None
        
        if msg.get('scope') == 0:
            addr = msg.get_attr('IFA_ADDRESS')
            if addr and not addr.startswith('fdde:'):
                return addr
        return None
    
    def _monitor_kernel(self):
        """Listen for kernel IPv6 address change events."""
        try:
            with IPRoute() as ipr:
                ipr.bind()
                while not self._stop:
                    for msg in ipr.get():
                        if self._stop or msg.get('index') != self.interface_index:
                            continue
                        addr = self.parse_ipv6_address(msg)
                        if addr:
                            for h in self.handlers:
                                try:
                                    h(addr, "kernel")
                                except Exception as e:
                                    logging.error(f"Handler error for {self.interface_name}: {e}")
        except Exception as e:
            logging.error(f"Kernel monitor failed for {self.interface_name}: {e}")
    
    def _periodic_check(self):
        """Periodically check IPv6 address."""
        while not self._stop:
            time.sleep(self.check_interval)
            if not self._stop:
                addr = self._get_ipv6()
                if addr:
                    for h in self.handlers:
                        try:
                            h(addr, "periodic")
                        except Exception as e:
                            logging.error(f"Handler error for {self.interface_name}: {e}")
    
    def start(self):
        """Start monitoring."""
        addr = self._get_ipv6()
        if addr:
            for h in self.handlers:
                h(addr, "startup")
        
        for target in [self._monitor_kernel, self._periodic_check]:
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)
    
    def stop(self):
        """Stop monitoring."""
        self._stop = True
        for t in self._threads:
            t.join(timeout=5)


class DNSUpdater:
    """Handle DDNS updates with retry logic and heartbeat."""
    
    def __init__(self, name, update_url, state_file, retry_delay=300, heartbeat_interval=86400, user_agent="ddclient/3.10.0"):
        if not update_url or not update_url.strip():
            raise ValueError(f"update_url cannot be empty for {name}")
        
        self.name = name
        self.update_url = update_url
        self.state_file = pathlib.Path(state_file)
        self.retry_delay = retry_delay
        self.heartbeat_interval = heartbeat_interval
        self.user_agent = user_agent
        self.max_backoff = min(retry_delay * 32, 86400)
        
        self.last_ip = self._read_state()
        self.failures = 0
        self.max_failures = 5
        self._lock = threading.Lock()
        self._timers = {}
        
        self._schedule_heartbeat()
        logging.info(f"[{name}] Initialized with heartbeat every {heartbeat_interval}s")
    
    def _read_state(self):
        """Read last known IP from state file."""
        try:
            return self.state_file.read_text().strip()
        except FileNotFoundError:
            return None
    
    def _write_state(self, ip):
        """Atomically write state file."""
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', dir=self.state_file.parent, delete=False, encoding='utf-8') as f:
                f.write(ip)
            pathlib.Path(f.name).replace(self.state_file)
        except Exception as e:
            logging.error(f"[{self.name}] Failed to write state: {e}")
    
    def _is_success(self, response):
        """Check if response indicates success."""
        if not response:
            return False
        resp = response.strip().lower()
        return resp.startswith(("good", "ok", "success", "nochg")) and \
               "error" not in resp and "fail" not in resp
    
    def _cancel_timers(self, exclude=None):
        """Cancel all active timers except specified ones."""
        for key, timer in list(self._timers.items()):
            if key != exclude and timer and timer.is_alive():
                timer.cancel()
                del self._timers[key]
    
    def _do_update(self, ip, is_heartbeat=False):
        """Perform DDNS update."""
        should_schedule_hb = False
        url = None
        with self._lock:
            if ip is None or (ip == self.last_ip and not is_heartbeat):
                should_schedule_hb = True
            else:
                url = self.update_url.replace("<ipv6address>", ip)
        
        if should_schedule_hb:
            self._schedule_heartbeat()
            return
        
        try:
            logging.info(f"[{self.name}] Updating: {ip}")
            with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': self.user_agent}), timeout=10) as resp:
                response = resp.read().decode('utf-8').strip()
                logging.info(f"[{self.name}] Response: {response}")
                if not self._is_success(response):
                    raise ValueError(f"Provider rejected: {response}")
            
            with self._lock:
                self.last_ip = ip
                self.failures = 0
                self._write_state(ip)
        except Exception as e:
            logging.error(f"[{self.name}] Update failed: {e}")
            with self._lock:
                self.failures += 1
                if self.failures >= self.max_failures:
                    logging.warning(f"[{self.name}] Max retries ({self.max_failures}) reached")
                else:
                    delay = min(self.retry_delay * (2 ** (self.failures - 1)), self.max_backoff)
                    logging.info(f"[{self.name}] Retry in {delay}s (attempt {self.failures}/{self.max_failures})")
                    self._timers['retry'] = threading.Timer(delay, self._do_update, (ip, False))
                    self._timers['retry'].start()
                    return
        finally:
            self._schedule_heartbeat()
    
    def _schedule_heartbeat(self):
        """Schedule next heartbeat."""
        with self._lock:
            self._cancel_timers(exclude='retry')
            if self.last_ip:
                self._timers['heartbeat'] = threading.Timer(self.heartbeat_interval, self._do_update, (self.last_ip, True))
                self._timers['heartbeat'].start()
    
    def on_ip_change(self, ip, reason):
        """Handle IP address change."""
        with self._lock:
            self._cancel_timers()  # Cancel all timers including retry for old IP
            self._timers['update'] = threading.Timer(15, self._do_update, (ip, False))
            self._timers['update'].start()
    
    def stop(self):
        """Stop all timers."""
        with self._lock:
            self._cancel_timers()


def load_config(path):
    """Load and validate configuration."""
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        logging.error(f"Invalid JSON in {path}: {e}")
        return None
    except Exception as e:
        logging.error(f"Failed to read {path}: {e}")
        return None


def main():
    """Main application loop."""
    config_dir = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(__file__).parent / "config"
    
    if not config_dir.is_dir():
        logging.error(f"Config directory not found: {config_dir}")
        sys.exit(1)
    
    logging.info(f"Loading config from: {config_dir}")
    
    for config_file in config_dir.glob("*.json"):
        config = load_config(config_file)
        if not config:
            continue
        
        check_interval = config.get("check_interval", 900)
        if not isinstance(check_interval, int) or check_interval < 1:
            logging.warning(f"Invalid check_interval in {config_file}, using 900")
            check_interval = 900
        
        updaters = []
        for provider_name, provider_cfg in config.get("ddns_providers", {}).items():
            try:
                state_file = provider_cfg.get("state_file") or f"/var/tmp/ddnsmart_{provider_name}_ipv6.txt"
                updater = DNSUpdater(
                    name=provider_name,
                    update_url=provider_cfg.get("update_url", ""),
                    state_file=state_file,
                    retry_delay=provider_cfg.get("retry_delay", 300),
                    heartbeat_interval=provider_cfg.get("heartbeat_interval", 86400),
                    user_agent=provider_cfg.get("user_agent", "ddclient/3.10.0")
                )
                updaters.append(updater)
                services.append(updater)
            except Exception as e:
                logging.error(f"Failed to initialize {provider_name}: {e}")
        
        if not updaters:
            logging.warning(f"No valid providers in {config_file}")
            continue
        
        interface_name = config.get("interface_name")
        if not interface_name or not interface_name.strip():
            logging.error(f"Missing interface_name in {config_file}")
            continue
        
        try:
            monitor = IPMonitor(
                interface_name=interface_name,
                handlers=[u.on_ip_change for u in updaters],
                check_interval=check_interval
            )
            monitor.start()
            interfaces.append(monitor)
        except Exception as e:
            logging.error(f"Failed to start monitor for {interface_name}: {e}")
    
    def cleanup(signum=None, frame=None):
        logging.info("Shutting down...")
        for m in interfaces:
            m.stop()
        for s in services:
            s.stop()
        logging.shutdown()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGHUP, cleanup)
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        cleanup()


if __name__ == "__main__":
    main()

