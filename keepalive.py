#!/usr/bin/env python3


import logging
logging.basicConfig(format='%(asctime)s %(levelname)-8s %(message)s', filename='/var/tmp/ddnsmart/keepalive.log', level=logging.DEBUG)
import time
import threading
from ddnsmart import getInstance


if __name__ == "__main__":
    logging.info("up and running.")

    while True:
        ddnsmart = getInstance()
        thestate = ddnsmart.readState()
        age = (time.time() - thestate.get("timestamp", 0))
        startInS = ddnsmart.theconfig["maxAgeInSeconds"] - age
        if startInS > 0:
            time.sleep(startInS)
            continue   # recheck age

        logging.info("force updating NOW")
        ddnsmart.runCheck(thestate=thestate, forceRun=True)
        time.sleep(ddnsmart.theconfig["maxAgeInSeconds"])

