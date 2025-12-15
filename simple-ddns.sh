#!/bin/bash


[ "${FLOCKER_SIMPLE_DDNS}" != "$0" ] && exec env FLOCKER_SIMPLE_DDNS="$0" flock -e -w 10 "$0" "$0" "$@" || :

OPTS=$(getopt -o c:h:t:s: --long config_dir:,host:,trigger:,skip_duration_seconds: -n 'simple-ddns.sh' -- "$@")
if [ $? -ne 0 ]; then
  echo "Failed to parse options" >&2
  exit 1
fi

## Reset the positional parameters to the parsed options
eval set -- "$OPTS"

## Process the options
CONFIG_DIR="."
HOST=""
TRIGGER="manual"
SKIP_DURATION_SECONDS=900
while true; do
  case "$1" in
    -c | --config_dir)
      CONFIG_DIR="$2"
      shift 2
      ;;
    -h | --host)
      HOST="$2"
      shift 2
      ;;
    -t | --trigger)
      TRIGGER="$2"
      shift 2
      ;;
    -d | --duration_seconds)
      SKIP_DURATION_SECONDS="$2"
      shift 2
      ;;
    --)
      shift
      break
      ;;
    *)
      echo "internal error"
      exit 1
      ;;
  esac
done

if [ -z "${HOST}" ]; then
  echo "error: please specify host"
  exit 1
fi

LAST_IP_FILE="/tmp/simple_ddns_last_ipv6-$HOST.txt"
SKIP_FILE="/tmp/simple_ddns-$HOST.lock"
LOG_FILE="/tmp/simple_ddns-$HOST.log"
SUCCESS_RESPONSE_PATTERNS="good|nochg"


# read config file
source "$CONFIG_DIR/$HOST"


set_skip_timer() {
    local CURRENT_TIME=$(date +%s)
    local SKIP_UNTIL=$((CURRENT_TIME + SKIP_DURATION_SECONDS))
    echo "$SKIP_UNTIL" > "$SKIP_FILE"
    echo "$(date) [${TRIGGER}]: setting skip timer - next run: $(date -d @${SKIP_UNTIL})." >> $LOG_FILE
}

clear_skip_flag() {
    rm -f "$SKIP_FILE"
}

check_skip_timer() {
    if [ -f "$SKIP_FILE" ]; then
        local SKIP_UNTIL_TIMESTAMP=$(cat "$SKIP_FILE")
        local CURRENT_TIME=$(date +%s)
        
        if [ "$CURRENT_TIME" -lt "$SKIP_UNTIL_TIMESTAMP" ]; then
            local REMAINING_SECONDS=$((SKIP_UNTIL_TIMESTAMP - CURRENT_TIME))
	    echo "$(date) [${TRIGGER}]: ** SKIP-TIMER ACTIVE - remaining seconds: ${REMAINING_SECONDS}s. **" >> $LOG_FILE
            return 0 # Timer aktiv: SKIP
        else
            # timer expired
            clear_skip_flag
            return 1 # go!
        fi
    else
        return 1 # no timer was set - go!
    fi
}

get_current_ipv6() {
    echo $(ip -6 addr list scope global eth0 | grep -v " fd" | sed -n 's/.*inet6 \([0-9a-f:]\+\).*/\1/p' | head -n 1)
}

read_last_ipv6() {
    if [ -f "$LAST_IP_FILE" ]; then
        cat "$LAST_IP_FILE"
    else
      echo ""
    fi
}

send_ddns_update_and_check() {
    local NEW_IP="$1"
    
    echo "$(date) [${TRIGGER}]: updating DDNS record ..." >> $LOG_FILE
    local FINAL_UPDATE_URL=$(echo "${DDNS_UPDATE_URL}" | sed "s/myip=::/myip=${NEW_IP}/")
    local RESPONSE=$(echo -e "silent\nfail\nmax-time 30\nurl=\"${FINAL_UPDATE_URL}\"" | curl --config - 2>&1)
    echo "$(date) [${TRIGGER}]: DDNS response: ${RESPONSE}" >> $LOG_FILE
    
    if echo "${RESPONSE}" | grep -qE "${SUCCESS_RESPONSE_PATTERNS}"; then
        return 0 # success
    else
        return 1 # failure
    fi
}


if check_skip_timer; then
    exit 0
fi


LAST_IPV6="$(read_last_ipv6)"
CURRENT_IPV6="$(get_current_ipv6)"

if [[ -z "$CURRENT_IPV6" ]]; then
    echo "$(date) [${TRIGGER}]: ERROR: could not get current IPv6 address." >> $LOG_FILE
    exit 1
fi

if [ "$CURRENT_IPV6" != "$LAST_IPV6" ]; then
    echo "$(date) [${TRIGGER}]: IPv6 address updated - old: $LAST_IPV6 | new: $CURRENT_IPV6" >> $LOG_FILE

    if send_ddns_update_and_check "$CURRENT_IPV6"; then
        echo "$CURRENT_IPV6" > "$LAST_IP_FILE"
        clear_skip_flag
    else
        set_skip_timer
        echo "$(date) [${TRIGGER}]: FAIL: Updating DDNS record failed - setting 15 min timer" >> $LOG_FILE
        exit 1
    fi
    
else
    clear_skip_flag
    exit 0
fi

exit 0

