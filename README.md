# ddnsmart - simple DDNS client

# how to use
<code>
chmod a+x ipv6-monitor.sh
cp ipv6-monitor.service /etc/systemd/system/ipv6-monitor.service
(review /etc/systemd/system/ipv6-monitor.service and update paths accordingly)
sudo systemctl daemon-reload
sudo systemctl enable --now ipv6-monitor.service

for monitoring, use:
journalctl -u ipv6-monitor.service -f
</code>

