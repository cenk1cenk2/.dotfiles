#!/usr/bin/env bash

set -e
set -o pipefail

~/.config/mutt/accounts/add-html.py | msmtp "$@"
