# ddnsmart - simple DDNS client

example configuration file:

<code>
{
  "monitored_interface": "enp1s0f0",
  "state_dir": "/var/tmp/ddnsmart",
  "poll_interval": 60,
  "debounce_delay": 15.0,
  "retry_interval": 900,
  "heartbeat_interval": 86400,
  "request_timeout": 30,
  "max_retries": 5,
  "providers": [
    {
      "name": "spdyn",
      "update_url": "https://update.spdyn.de/nic/update?hostname=HOSTNAME&myip={ipv6}&user=USERNAME&pass=TOKEN",
      "method": "GET"
    }
  ]
}
</code>

