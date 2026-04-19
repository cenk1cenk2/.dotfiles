#!/usr/bin/env zsh
#
# Quick note -> clipboard. Opens nvim on a throwaway markdown file; every
# :w pipes the buffer through cbcp (zsh function) into the Wayland
# clipboard. The temp file is removed on exit.
#
# shellcmdflag=-ic forces :! to run under an interactive zsh so cbcp
# (and any other user aliases/functions) resolve.

set -eu

tmp="$(mktemp --suffix=.md)"
trap 'rm -f "$tmp"' EXIT

nvim \
  --cmd 'set shellcmdflag=-ic' \
  -c 'autocmd BufWritePost <buffer> silent !cbcp < %' \
  "$tmp"
