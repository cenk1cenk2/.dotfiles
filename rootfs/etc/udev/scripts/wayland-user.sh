#!/usr/bin/env sh

# Get all active user sessions
for user_dir in /run/user/*/; do
  # Extract UID from directory path
  uid="$(basename "$user_dir")"

  # Skip if not a valid UID (numeric)
  case "$uid" in
  '' | *[!0-9]*) continue ;;
  esac

  # Get username from UID
  user="$(getent passwd "$uid" | cut -d: -f1)"

  # Skip if user not found
  [ -z "$user" ] && continue

  # Check if user has an active session
  [ -S "/run/user/$uid/bus" ] || continue

  # Set DBUS session bus address and run command for this user
  export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus"
  sudo -u "$user" "$1"
done
