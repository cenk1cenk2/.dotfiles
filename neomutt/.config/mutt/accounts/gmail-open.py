#!/usr/bin/env python3
"""
Extract Message-ID from email and open in Gmail web interface
"""

import sys
import re
import subprocess
import urllib.parse
from email import message_from_file
from email.header import decode_header

def decode_header_value(header_value):
    """Decode email header value handling encoding"""
    if not header_value:
        return ""

    decoded_parts = decode_header(header_value)
    decoded_string = ""

    for part, encoding in decoded_parts:
        if isinstance(part, bytes):
            decoded_string += part.decode(encoding or "utf-8", errors="ignore")
        else:
            decoded_string += part

    return decoded_string.strip()

def extract_message_info(email_content):
    """Extract Message-ID and Subject from email"""
    try:
        msg = message_from_file(email_content)

        message_id = msg.get("Message-ID", "")
        if message_id:
            message_id = re.sub(r"^<(.*)>$", r"\1", message_id.strip())

        subject = decode_header_value(msg.get("Subject", ""))

        return message_id, subject

    except Exception as e:
        print(f"Error parsing email: {e}", file=sys.stderr)
        return "", ""

def test_gmail_search(base_url, search_term, description):
    """Test a single Gmail search URL"""
    try:
        encoded_search = urllib.parse.quote(search_term)
        url = f"{base_url}#search/{encoded_search}"

        print(f"Trying {description}: {search_term}", file=sys.stderr)

        result = subprocess.run(
            ["xdg-open", url], capture_output=True, text=True, timeout=10
        )

        if result.returncode == 0:
            print(f"Successfully opened Gmail with {description}")
            return True
        else:
            print(
                f"Failed to open URL (return code: {result.returncode})",
                file=sys.stderr,
            )
            if result.stderr:
                print(f"Error: {result.stderr}", file=sys.stderr)
            return False

    except subprocess.TimeoutExpired:
        print(f"Timeout opening {description}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"Error opening {description}: {e}", file=sys.stderr)
        return False

def open_in_gmail(message_id, subject, account=0):
    """Open email in Gmail web interface"""
    base_url = f"https://mail.google.com/mail/u/{account}/"

    search_strategies = []

    if message_id:
        clean_message_id = message_id.strip()
        if clean_message_id.startswith("<") and clean_message_id.endswith(">"):
            clean_message_id = clean_message_id[1:-1]
        search_strategies.extend(
            [
                (f"rfc822msgid:{clean_message_id}", "Message-ID RFC822 quoted"),
            ]
        )

    if subject:
        clean_subject = re.sub(
            r"^(Re:|Fwd?:|RE:|FWD?:)\s*", "", subject, flags=re.IGNORECASE
        )
        clean_subject = clean_subject.strip()
        if clean_subject:
            search_strategies.extend(
                [
                    (f'subject:"{clean_subject}"', "Subject search"),
                ]
            )

    for search_term, description in search_strategies:
        if test_gmail_search(base_url, search_term, description):
            return True

    print("All search strategies failed, opening inbox", file=sys.stderr)
    try:
        result = subprocess.run(
            ["xdg-open", f"{base_url}#inbox"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            print("Opened Gmail inbox as fallback")
            return True
    except Exception as e:
        print(f"Failed to open Gmail inbox: {e}", file=sys.stderr)

    return False

def main():
    """Main function"""
    try:
        message_id, subject = extract_message_info(sys.stdin)

        print(f"Extracted Message-ID: {message_id}", file=sys.stderr)
        print(f"Extracted Subject: {subject}", file=sys.stderr)

        if not message_id and not subject:
            print("Could not extract Message-ID or Subject from email", file=sys.stderr)
            sys.exit(1)

        success = open_in_gmail(message_id, subject, account=0)

        if not success:
            print("All attempts to open Gmail failed", file=sys.stderr)
            sys.exit(1)

    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
