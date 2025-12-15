#!/usr/bin/env python3


import logging
logging.basicConfig(format='%(asctime)s %(levelname)-8s %(message)s', filename='/var/tmp/ddnsmart/ddnsmart.log', level=logging.DEBUG)
import sys
import os
import subprocess
import time
import urllib.request
import urllib.parse
import json


class DDNSmart():
    def __init__(self, theconfig):
        self.theconfig = theconfig
        self.providers = []
        for pname, p in self.theconfig["providers"].items():
            if pname.startswith("_"):
                logging.warning("ignoring provider " + pname + " (starts with _)")
                continue
            self.providers.append(DDNSProvider(
                theipv4uri=p.get("ipv4uri", None),
                ipv4params=p.get("ipv4params", None),
                theipv6uri=p.get("ipv6uri", None),
                ipv6params=p.get("ipv6params", None),
                theipv4v6uri=p.get("upv4v6uri", None),
                ipv4v6params=p.get("ipv4v6params", None),
                waitXseconds=p.get("waitXseconds", 60)
            ))
            if "authtype" in p:
                passman = urllib.request.HTTPPasswordMgrWithDefaultRealm()
                passman.add_password(p.get("authrealm", None), p["authdomain"], p.get("authuser", None), p.get("authpassword", None))
                if p["authtype"] == "digest":
                    authhandler = urllib.request.HTTPDigestAuthHandler(passman)
                else:
                    authhandler = urllib.request.HTTPBasicAuthHandler(passman)
                opener = urllib.request.build_opener(authhandler)
                urllib.request.install_opener(opener)

    def _getWebIP(self, theuri):
        with urllib.request.urlopen(theuri) as r:
            theip = r.read().decode('utf-8')
        return theip

    def _getGlobalV6IPs(self, networkinterface):
        result = []
        out = subprocess.check_output("ip -json -6 addr show dev eth0 scope global -deprecated", shell=True).decode()
        ipjson = json.loads(out)
        #print(ipjson)
        for line in ipjson:
            if "addr_info" in line:
                for addr in line["addr_info"]:
                    if "local" in addr:
                        result.append(addr["local"])
        return result

    def getIPv4(self):
        if self.theconfig["ipv4check"]["type"] != "web":
            raise Exception("ipv4check type != web")
        return self._getWebIP(theuri=self.theconfig["ipv4check"]["uri"])

    def getGlobalIPv6(self):
        if "ipv6check" in self.theconfig:
            if self.theconfig["ipv6check"].get("type", "proc") == "proc":
                currentIP6s = self._getGlobalV6IPs(networkinterface=self.theconfig["ipv6check"]["networkinterface"])
                if len(currentIP6s) != 0:
                    return currentIP6s[0]
            elif self.theconfig["ipv6check"].get("type", "proc") == "web":
                return self._getWebIP(theuri=self.theconfig["ipv6check"]["uri"])

        return None

    def readState(self):
        result = {"ipv4address": None, "ipv6address": None, "timestamp": 0}
        if os.path.exists(self.theconfig["statefile"]):
            with open(self.theconfig["statefile"], "r") as statefileobj:
                result = json.load(statefileobj)
        return result

    def writeState(self, thestate):
        tmpname = self.theconfig["statefile"] + ".tmp"
        with open(tmpname, "w") as statefileobj:
            json.dump(thestate, statefileobj)
        os.rename(tmpname, self.theconfig["statefile"])

    def sendUpdate(self, ipv4address, ipv6address):
        for i in self.providers:
            i.sendUpdate(ipv4address=ipv4address, ipv6address=ipv6address)

    def runCheck(self, thestate, forceRun):
        updateIP4 = self.getIPv4()
        updateIP6 = self.getGlobalIPv6()

        performUpdate = False
        if forceRun:
            performUpdate = True
        else:
            if updateIP4 is None:
                logging.info("no IPv4 address assigned.")
            if updateIP6 is None:
                logging.info("no IPv6 address assigned.")

            if updateIP4 == thestate.get("ipv4address", None):
                updateIP4 = None
            else:
                thestate["ipv4address"] = updateIP4
            if updateIP6 == thestate.get("ipv6address", None):
                updateIP6 = None
            else:
                thestate["ipv6address"] = updateIP6

        if updateIP4 is not None or updateIP6 is not None:
            thestate["timestamp"] = int(time.time())
            self.writeState(thestate=thestate)
            self.sendUpdate(ipv4address=updateIP4, ipv6address=updateIP6)


class DDNSProvider():
    def __init__(self, theipv4uri, ipv4params, theipv6uri, ipv6params, theipv4v6uri, ipv4v6params, waitXseconds):
        self.theipv4uri = theipv4uri
        self.ipv4params = ipv4params
        self.theipv6uri = theipv6uri
        self.ipv6params = ipv6params
        self.theipv4v6uri = theipv4v6uri
        self.ipv4v6params = ipv4v6params
        self.waitXseconds = waitXseconds

    def _sendUpdate(self, curruri):
        #logging.warning("dry run: " + curruri)
        #return
        logging.debug(curruri)
        for i in range(0, 3):
            try:
                with urllib.request.urlopen(curruri) as r2:
                    theresponse = r2.read()
                    logging.debug(theresponse)
                break
            except:
                logging.warning("connection failed. trying again in 300s ...")
                time.sleep(300)

    def _prepareURI(self, theuri, params, ipv4address, ipv6address):
        newparams = {}
        for k, v in params.items():
            kk = k
            vv = v
            if ipv4address is not None:
                kk = kk.replace("<ipv4address>", ipv4address)
                vv = None if vv is None else vv.replace("<ipv4address>", ipv4address)
            if ipv6address is not None:
                kk = kk.replace("<ipv6address>", ipv6address)
                vv = None if vv is None else vv.replace("<ipv6address>", ipv6address)
            newparams[kk] = vv

        sepchar = "&" if "?" in theuri else "?"
        return theuri + sepchar + urllib.parse.urlencode(newparams)

    def sendUpdate(self, ipv4address, ipv6address):
        if ipv4address is not None and ipv6address is not None and self.theipv4v6uri is not None:
            curruri = self._prepareURI(theuri=self.theipv4v6uri, params=self.ipv4v6params, ipv4address=ipv4address, ipv6address=ipv6address)
            self._sendUpdate(curruri=curruri)
        else:
            dowait = False
            if ipv4address is not None and self.theipv4uri is not None:
                curruri = self._prepareURI(theuri=self.theipv4uri, params=self.ipv4params, ipv4address=ipv4address, ipv6address=None)
                self._sendUpdate(curruri=curruri)
                dowait = True
            if ipv6address is not None and self.theipv6uri is not None:
                if dowait:
                    logging.debug("waiting " + str(self.waitXseconds) + "s")
                    time.sleep(self.waitXseconds)
                curruri = self._prepareURI(theuri=self.theipv6uri, params=self.ipv6params, ipv4address=None, ipv6address=ipv6address)
                self._sendUpdate(curruri=curruri)


def getInstance():
    with open("config.json", "r") as configobj:
        theconfig = json.load(configobj)

    ddnsmart = DDNSmart(theconfig=theconfig)
    return ddnsmart


if __name__ == "__main__":
    ddnsmart = getInstance()
    thestate = ddnsmart.readState()
    ddnsmart.runCheck(thestate=thestate, forceRun=False)
