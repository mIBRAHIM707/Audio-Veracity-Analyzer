"""CSV export of an alert page (PRD FR-A6 "CSV export"; 19 Nov 2026).

`alerts_csv(alerts)` turns the `alerts` list of `PostgresCallHistory.alerts()` into RFC
4180 text (CRLF line ends, a header row, every field quoted only when needed). Times are
ISO 8601 UTC with microseconds; numbers are written as Python writes them; a missing
value is an empty field. Free-text fields (alert and call IDs) that a spreadsheet would
read as a formula (first character `=`, `+`, `-`, `@`, tab or carriage return) get a
leading apostrophe (OWASP "CSV injection" advice), so opening the file never runs one.
No BOM: the file is UTF-8, which a spreadsheet may need to be told when importing.
"""
import csv
from datetime import datetime, timedelta, timezone
import io

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
COLUMNS = ('alert_id', 'call_id', 'session', 'direction', 'state', 'raised_at', 'raised_window', 'raised_ema',
           'peak_ema', 'closed_at', 'cleared_window', 'cleared_ema')
FORMULA_START = ('=', '+', '-', '@', '\t', '\r')


def _time(ts):
    if ts is None:
        return ''
    # Arithmetic, not fromtimestamp: that raises OSError for negative times on Windows.
    when = EPOCH + timedelta(microseconds=round(ts * 1_000_000))
    return when.isoformat(timespec='microseconds').replace('+00:00', 'Z')


def _text(value):
    if value is None:
        return ''
    value = str(value)
    return "'" + value if value.startswith(FORMULA_START) else value


def _number(value):
    return '' if value is None else repr(value) if isinstance(value, float) else str(value)


def alerts_csv(alerts):
    out = io.StringIO()
    writer = csv.writer(out, lineterminator='\r\n')
    writer.writerow(COLUMNS)
    for alert in alerts:
        writer.writerow([_text(alert['alert_id']), _text(alert['call_id']), _number(alert['session']),
                         _text(alert['direction']), _text(alert['state']), _time(alert['raised_ts']),
                         _number(alert['raised_window']), _number(alert['raised_ema']), _number(alert['peak_ema']),
                         _time(alert['closed_ts']),
                         _number(alert['cleared_window']), _number(alert['cleared_ema'])])
    return out.getvalue()
