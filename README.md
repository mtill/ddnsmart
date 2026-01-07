# ddnsmart - simple DDNS client

# how to use
<code>
chmod a+x ipv6-monitor.sh
cp ipv6-monitor.service /etc/systemd/system/ipv6-monitor.service
(review /etc/systemd/system/ipv6-monitor.service and update paths accordingly)
sudo systemctl daemon-reload
sudo systemctl enable --now ipv6-monitor.service

add to crontab:
3,13,23,33,43,53 * * * *    root    /etc/serverscripts/simple-ddns-update-all.sh /etc/serverscripts/simple-ddns.config 6 cron > /dev/null 2&1

for monitoring, use:
journalctl -u ipv6-monitor.service -f
</code>

