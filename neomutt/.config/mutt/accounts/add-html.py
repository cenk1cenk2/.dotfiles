#!/usr/bin/env python3

import os
import subprocess
import sys
import tempfile
from email import policy
from email.parser import Parser
from email.message import EmailMessage

def handle_multipart_mixed(msg: EmailMessage) -> EmailMessage:
    plain = None
    index = None

    for i, part in enumerate(msg.get_payload()):
        if (part.get_content_type() == "text/plain" and
            part.get_content_disposition() not in [ "attachment" ]):
            plain = part
            index = i
            break

    if not plain:
        return msg

    content = plain.get_content()
    if not content or not content.strip():
        return msg

    alternatives = EmailMessage()
    alternatives.make_alternative()
    alternatives.add_alternative(content, subtype='plain')
    alternatives.add_alternative(to_html(content), subtype='html', charset='utf-8')

    payload = list(msg.get_payload())
    payload[index] = alternatives
    msg.set_payload(payload)

    return msg

def handle_multipart_alternative(msg: EmailMessage) -> EmailMessage:
    plain = None

    for part in msg.get_payload():
        if part.get_content_type() == "text/plain":
            plain = part
        elif part.get_content_type() == "text/html":
            return msg

    if plain:
        msg.add_alternative(to_html(plain.get_content()), subtype='html', charset='utf-8')

    return msg

def handle_generic_multipart(msg: EmailMessage) -> EmailMessage:
    for part in msg.walk():
        if (part.get_content_type() == "text/plain" and
            part.get_content_disposition() not in [ "attachment" ] and
            not part.is_multipart()):

            content = part.get_content()
            if content and content.strip():
                part.clear_content()
                part.make_alternative()
                part.add_alternative(content, subtype='plain')
                part.add_alternative(to_html(content), subtype='html', charset='utf-8')
                break

    return msg

# from: https://github.com/yashlala/dotfiles/blob/global/scripts/.local/scripts/add-html-to-email
def with_html(msg: EmailMessage) -> EmailMessage:
    if msg.is_multipart():
        if msg.get_content_type() == "multipart/mixed":
            return handle_multipart_mixed(msg)
        elif msg.get_content_type() == "multipart/alternative":
            return handle_multipart_alternative(msg)
        else:
            return handle_generic_multipart(msg)

    else:
        content = msg.get_content()
        if not content or not content.strip():
            return msg

        msg.clear_content()
        msg.make_alternative()

        msg.add_alternative(content, subtype='plain')
        msg.add_alternative(to_html(content), subtype='html', charset='utf-8')

    return msg

def to_html(text: str) -> str:
    with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as file:
        file.write(text)
        temp_file = file.name

    try:
        result = subprocess.run(
            [os.path.expanduser("md-printer"), "-O", "-t", "mail", "-f", "md", "-F", "html", "--log-level", "silent", temp_file],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            raise Exception(f"Can not process file: {result.returncode} -> {result.stderr}")

        return result.stdout

    finally:
        try:
            os.unlink(temp_file)
        except OSError:
            pass

if __name__ == "__main__":
    os.write(1, with_html(Parser(policy=policy.SMTP).parse(sys.stdin)).as_bytes())
