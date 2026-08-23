"""Display formatting helpers."""


def format_runtime(minutes):
    if not minutes:
        return "N/A"
    hours = minutes // 60
    remaining_minutes = minutes % 60
    return f"{hours}h {remaining_minutes}m" if hours > 0 else f"{remaining_minutes}m"


def format_currency(amount):
    if not amount:
        return "N/A"
    return f"{amount:,}"
