#!/bin/sh

set -eu

. "${0%/*}/common.sh" # source common functions

multiple="$1"
directory="$2"
save="$3"
path="$4"
out="$5"

cmd="ranger"
termcmd=$(default_termcmd)

info=$(
  cat <<EOF
xdg-desktop-portal-termfilechooser saving files tutorial

!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
!!!                 === WARNING! ===                 !!!
!!! The contents of *whatever* file you open last in !!!
!!! ranger will be *overwritten*!                    !!!
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!

Instructions:
1) Move this file wherever you want.
2) Rename the file if needed.
3) Confirm your selection by opening the file, for
   example by pressing <Enter>.

Notes:
1) This file is provided for your convenience. You
   could delete it and choose another file to overwrite
   that, for example.
2) If you quit ranger without opening a file, this file
   will be removed and the save operation aborted.
EOF
)

if [ "$save" = "1" ]; then
  create_save_file "$path" "$out"
  set -- --choosefile="$out" --cmd="echo Select save path for the given file." --selectfile "$path"
elif [ "$directory" = "1" ]; then
  set -- --show-only-dirs --cmd="echo Select directory ('Q'uit in dir to select it), 'q' to cancel selection" --cmd="map Q chain shell echo %d > \"$out\" ; quitall"
elif [ "$multiple" = "1" ]; then
  set -- --choosefiles="$out" --cmd="echo Select file(s) (open file to select it; <Space> to select multiple)"
else
  set -- --choosefile="$out" --cmd="echo Select file (open file to select it)"
fi

$termcmd $cmd "$@"
