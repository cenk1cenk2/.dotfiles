#!/usr/bin/env python3

import os
import subprocess
import sys
from email import policy
from email.mime.text import MIMEText
from email.parser import Parser
from email.message import EmailMessage

# from: https://github.com/yashlala/dotfiles/blob/global/scripts/.local/scripts/add-html-to-email
def add_html(msg: EmailMessage) -> EmailMessage:
    body = msg.get_body()

    if body.get_content_type() == "text/plain":
        body.make_alternative()
        text = body.get_payload()[0]
        body.set_payload(
            [MIMEText(text.get_content(), "plain", "utf-8"), to_html(text)]
        )

    return msg

def to_html(text: str) -> MIMEText:
    result = subprocess.run(
        ["md-printer", "-I", "-O", "-t", "mail", "-f", "md", "--log-level", "silent"],
        input=text,
        capture_output=True,
    )

    return MIMEText(str(result.stdout), "html", "utf-8")

if __name__ == "__main__":
    msg = Parser(policy=policy.SMTP).parse(sys.stdin)
    msg = add_html(msg)

    os.write(1, msg.as_bytes())
