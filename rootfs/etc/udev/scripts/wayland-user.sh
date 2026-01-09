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

  # Set up environment for Wayland session
  export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus"
  export XDG_RUNTIME_DIR="/run/user/$uid"

  # Find the first available Wayland display socket
  wayland_display=""
  for socket in /run/user/$uid/wayland-*; do
    [ -S "$socket" ] && {
      wayland_display="$(basename "$socket")"
      break
    }
  done

  # Export WAYLAND_DISPLAY if found
  [ -n "$wayland_display" ] && export WAYLAND_DISPLAY="$wayland_display"

  # Run command for this user with proper environment
  sudo -u "$user" env DBUS_SESSION_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS" \
    XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR" \
    WAYLAND_DISPLAY="$WAYLAND_DISPLAY" \
    zsh -ic "$*"
done
