#!/usr/bin/env python3
# -*- coding: utf-8 -*-


from pathlib import Path
import subprocess
from flask import Flask, request
import socket # Used for simple IP validation

app = Flask(__name__)


LAST_IP_FILE = Path("/tmp/ipv4-address-router.txt")
WEB_SECRET_KEY = None
UPDATE_CALL = ["/etc/serverscripts/simple-ddns-update-all.sh", "/etc/serverscripts/simple-ddns.config", "4", "simple-ddns-server.py"]


def read_last_ip():
    if LAST_IP_FILE.exists():
        with open(LAST_IP_FILE, "r", encoding="utf-8") as f:
            return f.read()
    return None


def update_last_ip(new_ip):
    with open(LAST_IP_FILE, "w", encoding="utf-8") as f:
        f.write(new_ip)


@app.route('/update/<ipaddr>', methods=['GET'])
def update_dns(ipaddr):
    secret_key = request.args.get('password', None)
    if WEB_SECRET_KEY is not None and secret_key != WEB_SECRET_KEY:
        return "badauth", 403

    # Basic IP validation
    try:
        socket.inet_aton(ipaddr)
    except socket.error:
        return "badip", 400

    current_ip=read_last_ip()
    nochg = current_ip == ipaddr

    # invoke update script, even iff ip did not change
    subprocess.run(UPDATE_CALL)

    if nochg:
        return "nochg", 200

    update_last_ip(new_ip=ipaddr)
    return "good", 200


