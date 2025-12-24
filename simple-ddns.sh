#!/bin/bash


[ "${FLOCKER_SIMPLE_DDNS}" != "$0" ] && exec env FLOCKER_SIMPLE_DDNS="$0" flock -e -w 10 "$0" "$0" "$@" || :

SCRIPT_DIR=$(dirname "$(readlink -f "$0")")
OPTS=$(getopt -o c:h:t: --long config_dir:,host:,trigger: -n 'simple-ddns.sh' -- "$@")
if [ $? -ne 0 ]; then
  echo "Failed to parse options" >&2
  exit 1
fi

## Reset the positional parameters to the parsed options
eval set -- "$OPTS"

## Process the options
CONFIG_DIR="."
HOST=""
GET_IP_SCRIPT=""
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

LAST_IP_FILE="/var/tmp/simple_ddns-$HOST.lastip"
SKIP_FILE="/tmp/simple_ddns-$HOST.lock"
LOG_FILE="/var/log/simple_ddns-$HOST.log"
SUCCESS_RESPONSE_PATTERNS="good|nochg"
MAX_AGE_SECONDS=172800  # 2 * 24 hours * 60 minutes * 60 seconds


# read config file
source "$CONFIG_DIR/$HOST"

if [ -z "${GET_IP_SCRIPT}" ]; then
  echo "error: GET_IP_SCRIPT not specified."
  exit 1
fi

set_skip_timer() {
    local CURRENT_TIME=$(date +%s)
    local SKIP_UNTIL=$((CURRENT_TIME + SKIP_DURATION_SECONDS))
    echo "$SKIP_UNTIL" > "$SKIP_FILE"
    echo "$(date) [${TRIGGER}]: BLOCKING for ${SKIP_DURATION_SECONDS}s." >> $LOG_FILE
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
	    echo "$(date) [${TRIGGER}]: ** BLOCKED - remaining seconds: ${REMAINING_SECONDS}s. **" >> $LOG_FILE
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

read_last_ip() {
    if [ -f "$LAST_IP_FILE" ]; then
        FILE_MOD_TIME=$(stat -c %Y "$LAST_IP_FILE")
	CURRENT_TIME=$(date +%s)
	FILE_AGE_SECONDS=$((CURRENT_TIME - FILE_MOD_TIME))
	if [ "$FILE_AGE_SECONDS" -lt "$MAX_AGE_SECONDS" ]; then
          cat "$LAST_IP_FILE"
        fi
    fi

    echo ""
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


LAST_IP="$(read_last_ip)"
CURRENT_IP="$($GET_IP_SCRIPT)"

if [[ -z "$CURRENT_IP" ]]; then
    echo "$(date) [${TRIGGER}]: ERROR: could not get current IP address." >> $LOG_FILE
    exit 1
fi

if [ "$CURRENT_IP" != "$LAST_IP" ]; then
    echo "$(date) [${TRIGGER}]: IP address updated - old: $LAST_IP | new: $CURRENT_IP" >> $LOG_FILE

    if send_ddns_update_and_check "$CURRENT_IP"; then
        echo "$CURRENT_IP" > "$LAST_IP_FILE"
        clear_skip_flag
    else
        set_skip_timer
        echo "$(date) [${TRIGGER}]: FAIL: Updating DDNS record failed." >> $LOG_FILE
        exit 1
    fi
    
else
    clear_skip_flag
    exit 0
fi

exit 0


