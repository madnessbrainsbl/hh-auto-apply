"""Shared rolling-window accounting; only confirmed sends consume a slot."""
from datetime import datetime, timedelta


def count_recent_applications(shared, browser, window_hours=24, now=None):
    now = now or datetime.now()
    cutoff = now - timedelta(hours=window_hours)
    entries = list(shared.items()) if isinstance(shared, dict) else []
    if isinstance(browser, dict):
        entries.extend((key, entry.get('date')) for key, entry in browser.items()
                       if isinstance(entry, dict)
                       and entry.get('status') in ('sent', 'sent_wrong_filter'))
    sent_ids = set()
    for vacancy_id, stamp in entries:
        try:
            applied_at = datetime.strptime(str(stamp), '%Y-%m-%d %H:%M:%S')
        except ValueError:
            continue
        if cutoff <= applied_at <= now:
            sent_ids.add(str(vacancy_id))
    return len(sent_ids)
