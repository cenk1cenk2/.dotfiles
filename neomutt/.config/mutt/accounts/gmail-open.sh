#!/bin/bash
# Extract Message-ID and open in Gmail web interface
msgid=$(grep -i "^Message-ID:" | sed 's/^Message-ID: *<\(.*\)>/\1/')
if [ -n "$msgid" ]; then
    xdg-open "https://mail.google.com/mail/u/0/#search/rfc822msgid%3A$msgid"
fi