# ddnsmart - IPv6 DDNS Monitor and Update Service

A lightweight, event-driven Dynamic DNS (DDNS) client for IPv6 that automatically detects changes to your server's IPv6 address and updates your DDNS provider(s) in real-time.

## Overview

**ddnsmart** watches for IPv6 address changes on a specified network interface and automatically sends updates to configured DDNS providers. It uses netlink events for fast detection with polling as a fallback, ensuring your DNS records stay synchronized with your current IPv6 address.

### Key Features

- **Event-driven monitoring**: Uses netlink notifications for instant IPv6 change detection
- **Multi-provider support**: Update multiple DDNS providers simultaneously
- **Automatic retry logic**: Failed updates are retried with exponential backoff
- **State persistence**: Tracks the last successful IPv6 per provider to avoid unnecessary updates
- **Debouncing**: Filters rapid address change bursts to prevent request storms
- **Systemd integration**: Includes a service file for system-level deployment
- **Polling fallback**: Periodic polling ensures no changes are missed
- **Heartbeat monitoring**: Can verify connectivity at regular intervals

## Requirements

- Python
- `requests` library (for HTTP requests to DDNS providers)
- `pyroute2` library (for netlink interface monitoring)
- Linux with netlink support

## Installation

1. **Clone or download the repository:**
   ```bash
   git clone <repository-url>
   cd ddnsmart
   ```

2. **Install Python dependencies:**
   ```bash
   pip install requests pyroute2
   ```

3. **Create a configuration file:**
   Copy and customize a configuration file for your needs (see Configuration section below).

4. **(Optional) Install as a systemd service:**
   ```bash
   sudo cp simple-ddns.service ~/.config/systemd/user/ddnsmart.service
   # Edit the service file paths if needed
   sudo cp simple-ddns.py /usr/local/bin/ddnsmart
   systemctl --user daemon-reload
   systemctl --user enable ddnsmart
   systemctl --user start ddnsmart
   ```

## Configuration

Configuration is provided as JSON file(s). Pass the config directory path as an argument:

```bash
python3 simple-ddns.py /path/to/config/directory
```

### Configuration Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `monitored_interface` | string | Yes | Network interface to monitor for IPv6 changes (e.g., `enp1s0f0`, `eth0`) |
| `state_dir` | string | Yes | Directory where last-known IPv6 addresses are stored (must be writable) |
| `poll_interval` | integer | Yes | Polling interval in seconds to check for IPv6 changes |
| `debounce_delay` | float | No | Delay in seconds before triggering callbacks after an address change. Default: `15.0` |
| `retry_interval` | integer | Yes | Base retry interval in seconds for failed DDNS updates |
| `heartbeat_interval` | integer | No | Interval in seconds for connectivity verification (optional) |
| `request_timeout` | integer | Yes | Timeout in seconds for HTTP requests to DDNS providers |
| `max_retries` | integer | Yes | Maximum number of retry attempts for failed updates |
| `providers` | array | Yes | List of DDNS provider configurations |

### Provider Configuration

Each provider object in the `providers` array should include:

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | Yes | Unique identifier for the provider (e.g., `spdyn`, `dyndns`) |
| `update_url` | string | Yes | URL template for the DDNS update endpoint. Use `{ipv6}` as a placeholder for the IPv6 address |
| `method` | string | No | HTTP method to use (`GET` or `POST`). Default: `GET` |
| `username` | string | No | Username for basic HTTP authentication |
| `password` | string | No | Password for basic HTTP authentication |
| `headers` | object | No | Additional HTTP headers to include in the request |

### Example Configuration File

```json
{
  "monitored_interface": "enp1s0f0",
  "state_dir": "/var/tmp/ddnsmart",
  "poll_interval": 1800,
  "debounce_delay": 15.0,
  "retry_interval": 900,
  "heartbeat_interval": 86400,
  "request_timeout": 30,
  "max_retries": 5,
  "providers": [
    {
      "name": "spdyn",
      "update_url": "https://update.spdyn.de/nic/update?hostname=example.spdns.de&myip={ipv6}&user=example.spdns.de&pass=YOUR_AUTH_TOKEN",
      "method": "GET"
    },
    {
      "name": "custom_provider",
      "update_url": "https://api.example.com/update",
      "method": "POST",
      "headers": {
        "Authorization": "Bearer YOUR_API_KEY"
      }
    }
  ]
}
```

## Usage

### Running Directly

```bash
python3 simple-ddns.py /path/to/config/directory
```

### Running as a systemd Service

```bash
# Start the service
systemctl --user start ddnsmart

# Check status
systemctl --user status ddnsmart

# View logs
journalctl --user -u ddnsmart -f

# Stop the service
systemctl --user stop ddnsmart
```

### Running with Logging

The application logs to stdout with timestamps and thread information:

```
2024-04-12 10:15:23,456 [MainThread] INFO IPv6 set: 2001:db8::1
2024-04-12 10:15:24,123 [MainThread] INFO Updated spdyn -> 2001:db8::1
```

## How It Works

1. **Initialization**: Reads configuration and loads the last known IPv6 addresses for each provider from the state directory
2. **Monitoring**: Sets up netlink listener for IPv6 address changes with polling as fallback
3. **Detection**: When an IPv6 address change is detected:
   - The change is debounced (waits for rapid changes to settle)
   - Callbacks are triggered for registered handlers
4. **Update**: For each provider:
   - If the IPv6 address differs from the last known address, sends an update
   - On success: saves the new IPv6 to state and logs the update
   - On failure: schedules a retry with exponential backoff (delay = `retry_interval * 2^(attempt-1)`)
   - After `max_retries` attempts, gives up and logs a warning
5. **Retry Loop**: Periodically checks for pending retries and re-attempts failed updates
6. **State Persistence**: Maintains provider-specific state files to track successful updates

## Troubleshooting

### No IPv6 address detected
- Verify the interface name is correct: `ip link show`
- Check the interface has a non-link-local, non-deprecated global IPv6: `ip addr show enp1s0f0`
- Ensure the application is running with appropriate privileges
- Check logs for errors

### DDNS updates not working
- Verify the update URL and credentials are correct
- Check the `request_timeout` value if requests are timing out
- Review the log output for specific error messages
- Test the update URL manually with curl to verify it's accessible

