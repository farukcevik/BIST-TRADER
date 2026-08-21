from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage


class EmailNotificationProvider:
    def __init__(self,host: str|None=None,username: str|None=None,password: str|None=None,
                 recipient: str|None=None,port: int|None=None,timeout_seconds: float=5):
        self.host=host or os.getenv("SMTP_HOST"); self.username=username or os.getenv("SMTP_USERNAME")
        self.password=password or os.getenv("SMTP_PASSWORD"); self.recipient=recipient or os.getenv("SMTP_RECIPIENT")
        self.port=port or int(os.getenv("SMTP_PORT","587")); self.timeout_seconds=timeout_seconds

    @property
    def enabled(self) -> bool: return bool(self.host and self.username and self.password and self.recipient)

    def send(self,event: str,message: str) -> None:
        if not self.enabled: return
        mail=EmailMessage(); mail["Subject"]=f"BISTBOT: {event}"; mail["From"]=self.username; mail["To"]=self.recipient
        mail.set_content(message)
        try:
            with smtplib.SMTP(self.host,self.port,timeout=self.timeout_seconds) as smtp:
                smtp.starttls(); smtp.login(self.username,self.password); smtp.send_message(mail)
        except Exception:
            return
