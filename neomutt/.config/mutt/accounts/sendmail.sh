#!/usr/bin/env bash

set -e pipefail

# TODO: enable when oclif fixes shit
# ~/.config/mutt/accounts/add-html.py | msmtp "$@"

msmtp "$@"
