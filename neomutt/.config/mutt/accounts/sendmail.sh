#!/usr/bin/env bash

set -e

# TODO: enable when oclif fixes shit
# ~/.config/mutt/accounts/add-html.py | msmtp "$@"

msmtp "$@"
