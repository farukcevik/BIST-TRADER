import subprocess


class MacOSNotificationProvider:
    def send(self, event: str, message: str) -> None:
        safe_event, safe_message = event.replace('"', "'"), message.replace('"', "'")
        try:
            subprocess.run(["osascript", "-e", f'display notification "{safe_message}" with title "BISTBOT: {safe_event}"'],
                           check=False, capture_output=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            pass

