#!/bin/bash


DEV="$1"

ip -6 addr list scope global "$DEV" | grep -v " fd" | sed -n 's/.*inet6 \([0-9a-f:]\+\).*/\1/p' | head -n 1

