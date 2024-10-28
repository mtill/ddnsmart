# ddnsmart - experimental DDNS client
ddnsmart is a small, experimental DDNS client.


## configuration
Refer to config.json as a description on how to configure and use ddnsmart:

```json
{
    "statefile": "/var/tmp/ddnsmart/state",
    "ipv4check": {"type": "web", "uri": "http://checkip4.spdyn.de"},
    "_ipv6check": {"type": "web", "uri": "http://checkip6.spdyn.de"},
    "ipv6check": {"type": "proc", "networkinterface": "eth0"},
    "maxAgeInSeconds": 86400,

    "providers": {
        "afraid.org": {
            "ipv4uri": "https://freedns.afraid.org/dynamic/update.php?...",
            "ipv4params": {"address": "<ipv4address>"},
            "ipv6uri": "https://freedns.afraid.org/dynamic/update.php?...",
            "ipv6params": {"address": "<ipv6address>"}
        }
    }
}
```

## how to run ddnsmart
If you're using dhcpcd, create the file /etc/dhcpcd.exit-hook with the following content:

```bash
#!/bin/bash


if [ "${interface}" = "eth0" ]; then
  cd /home/pi/ddnsmart
  sudo -u pi ./ddnsmart.py &
fi
```

This will call ddnsmart each time a new IP address is assigned.


## please contribute
I look forward to your feedback and pull requests!

