#!/usr/bin/env bash

/etc/udev/scripts/notification.sh -u critical -i dialog-warning "Virus found!" "Signature detected by clamav: $CLAM_VIRUSEVENT_VIRUSNAME in $CLAM_VIRUSEVENT_FILENAME"
