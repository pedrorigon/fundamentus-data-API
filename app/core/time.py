from datetime import date, datetime
from zoneinfo import ZoneInfo


def today_in_timezone(timezone_name: str) -> date:
    return datetime.now(ZoneInfo(timezone_name)).date()
