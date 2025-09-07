#!/usr/bin/env python3

import os
import subprocess
import sys
import tempfile
from email import policy
from email.parser import Parser
from email.message import EmailMessage

def create_alternatives_structure(content: str) -> EmailMessage:
    related = EmailMessage()
    related.set_type('multipart/related')
    related.add_related(to_html(content), subtype='html', charset='utf-8')

    alternatives = EmailMessage()
    alternatives.make_alternative()
    alternatives.add_alternative(content, subtype='plain')
    alternatives.attach(related)

    return alternatives

def find_plain_text_part(msg: EmailMessage) -> tuple:
    if not msg.is_multipart():
        if msg.get_content_type() == "text/plain":
            content = msg.get_content()
            if content and content.strip():
                return content, msg, -1
        return None, None, -1

    for i, part in enumerate(msg.get_payload()):
        if (part.get_content_type() == "text/plain" and
            part.get_content_disposition() not in [ "attachment" ] and
            not part.is_multipart()):
            content = part.get_content()
            if content and content.strip():
                return content, part, i

        if part.is_multipart():
            content, plain, index = find_plain_text_part(part)
            if content:
                return content, plain, i

    return None, None, -1

def has_html_part(msg: EmailMessage):
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            return True
    return False

def with_html(msg: EmailMessage) -> EmailMessage:
    if has_html_part(msg):
        return msg

    content, plain, index = find_plain_text_part(msg)
    if not content:
        return msg

    alternatives = create_alternatives_structure(content)

    if not msg.is_multipart():
        msg_mixed = EmailMessage()
        msg_mixed.set_type('multipart/mixed')

        for key, value in msg.items():
            if key.lower() not in ['content-type', 'content-transfer-encoding', 'mime-version']:
                msg_mixed[key] = value

        msg_mixed.attach(alternatives)
        return msg_mixed

    elif msg.get_content_type() == "multipart/mixed":
        payload = list(msg.get_payload())
        payload[index] = alternatives
        msg.set_payload(payload)
        return msg

    else:
        msg_mixed = EmailMessage()
        msg_mixed.set_type('multipart/mixed')

        for key, value in msg.items():
            if key.lower() not in ['content-type', 'content-transfer-encoding', 'mime-version']:
                msg_mixed[key] = value

        msg_mixed.attach(alternatives)

        for i, part in enumerate(msg.get_payload()):
            if i != index:
                msg_mixed.attach(part)

        return msg_mixed

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
