#!/bin/bash


#IIFACE="eth0"
IIFACE=$(ip -6 route show default | awk '{print $5}' | head -n1)
CONFIG_PATH="/etc/serverscripts/simple-ddns.config"
LAST_IP=""


function run_update {
  /etc/serverscripts/simple-ddns-update-all.sh "$CONFIG_PATH" 6 ipv6-monitor
}


run_update

# This monitor listens for any IPv6 address addition/update
ip -6 monitor addr | while read -r line; do
    # Check if the line indicates an address was added/updated
    if echo "$line" | grep -q "inet6"; then
        # Extract the interface and the address
        # Example line: "2: eth0    inet6 2001:db8::1/64 scope global ..."
        IFACE=$(echo "$line" | awk '{print $2}')
        ADDR=$(echo "$line" | grep -oP 'inet6 \K[0-9a-fA-F:]+')
        SCOPE=$(echo "$line" | grep -oP 'scope \K\w+')

        # Filter: Only act on global, non-temporary addresses 
        # (Or remove 'grep -v temporary' if you DO want privacy addresses)
        if [[ "$IFACE" == "$IIFACE" ]] && [[ "$SCOPE" == "global" ]] && [[ ! "$line" =~ "temporary" ]] && [[ ! "$ADDR" == fd* ]] && [[ "$ADDR" != "$LAST_IP" ]]; then
            logger "IPv6 Monitor detected change: $ADDR on $IFACE"
	    run_update
	    LAST_IP="$ADDR"
        fi
    fi
done


