# ddnsmart - simple DDNS client

example configuration file:

<code>
{
  "interface_name": "enp1s0f0",
  "ddns_providers": {
    "spdyn": {
      "update_url": "https://update.spdyn.de/nic/update?hostname=MY_HOSTNAME.spdns.de&myip=<ipv6address>&user=MY_USERNAME.spdns.de&pass=MY_TOKEN"
    }
  }
}
</code>

