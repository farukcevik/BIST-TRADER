from __future__ import annotations

import json
import os
import urllib.request


class TelegramNotificationProvider:
    def __init__(self,token: str|None=None,chat_id: str|None=None,timeout_seconds: float=5):
        self.token=token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id=chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self.timeout_seconds=timeout_seconds

    @property
    def enabled(self) -> bool: return bool(self.token and self.chat_id)

    def send(self,event: str,message: str) -> None:
        if not self.enabled: return
        payload=json.dumps({"chat_id":self.chat_id,"text":f"BISTBOT — {event}\n{message}"}).encode()
        request=urllib.request.Request(f"https://api.telegram.org/bot{self.token}/sendMessage",data=payload,
                                       headers={"Content-Type":"application/json"},method="POST")
        try:
            with urllib.request.urlopen(request,timeout=self.timeout_seconds): pass
        except Exception:
            return
