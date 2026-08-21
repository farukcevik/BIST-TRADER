from typing import Protocol


class DailyReportProvider(Protocol):
    def generate(self) -> str: ...

