"""Claude-authored tests (22 November 2026): the operations runbooks stay true to the code
(backlog 11.2; PRD FR-O8, FR-O6, FR-A8, FR-T4).

Authored by Claude (Claude Code), not by GPT-6 Astra. Standard library only (sources are
parsed, never imported), so they run in the CI unit suite. A runbook that names a metric,
a label value the metric does not have, a log event, readiness reason, Grafana panel, AVA
flag, deployment-guide section, path or test that does not exist fails here; so does a
live or API readiness reason, or a warning/error log event of the live service or the API
relay, that no runbook section covers, and a runbook heading, part or index entry that is
malformed, duplicated, empty or missing. Each rule has a synthetic case showing it
catches its failure. Passing says the references are real; it does not say a procedure
works.
"""
import ast
import json
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_deployment_guide import (  # noqa: E402  (shared rules)
    CLI_SOURCES, cli_flags, headings, missing_paths, missing_tests, unknown_flags)

ROOT = Path(__file__).resolve().parents[2]
RUNBOOKS = ROOT / 'docs' / 'operations' / 'runbooks.md'
GUIDE = ROOT / 'docs' / 'operations' / 'deployment-guide.md'
SERVICES = ROOT / 'services'
HEALTH = SERVICES / 'live' / 'health.py'
EVENTLOG = SERVICES / 'live' / 'eventlog.py'
CONFIG_RELOAD = SERVICES / 'live' / 'config_reload.py'
RETENTION = SERVICES / 'live' / 'retention.py'
LIVE_MAIN = SERVICES / 'live' / '__main__.py'
API_OPLOG = SERVICES / 'api' / 'oplog.py'
API_APP = SERVICES / 'api' / 'app.py'
API_MAIN = SERVICES / 'api' / '__main__.py'
DASHBOARD = ROOT / 'deploy' / 'compose' / 'monitoring' / 'grafana' / 'dashboards' / 'ava-overview.json'

REQUIRED_PARTS = ('Signal', 'Calls', 'Check', 'Act', 'Tested')
NOT_METRICS = frozenset({'ava_reader'})  # `ava_` names that are not metrics (the read-only database login)
OTHER_TOOL_FLAGS = frozenset({'--since'})  # docker compose logs
CONFIG_WARNINGS = ('rejected', 'unreadable')  # config_reload outcomes logged as warnings
# `alert.raised` is logged as a warning, but an alert is the product's output for the
# reviewer (FR-U2), not an operations fault, so it needs no runbook.
PRODUCT_EVENTS = frozenset({'alert.raised'})
# Live events that must exist and need a runbook although the source tables do not mark them.
REQUIRED_LIVE_EVENTS = ('detector_failing', 'log_dropped')
# Labelled metric -> (label name, source file, the tuple of values in that file).
LABEL_TABLES = {
    'ava_live_tap_events_total': ('kind', HEALTH, 'TAP_COUNTERS'),
    'ava_live_sink_events_total': ('kind', HEALTH, 'SINK_COUNTERS'),
    'ava_live_rtp_receiver_total': ('kind', HEALTH, 'RECEIVER_COUNTERS'),
    'ava_live_jitter_buffer_total': ('kind', HEALTH, 'JITTER_COUNTERS'),
    'ava_live_store_events_total': ('outcome', HEALTH, 'STORE_OUTCOMES'),
    'ava_live_store_waits_total': ('kind', HEALTH, 'STORE_WAITS'),
    'ava_live_snippets_total': ('outcome', RETENTION, 'COUNTS'),
    'ava_live_config_reloads_total': ('outcome', CONFIG_RELOAD, 'OUTCOMES'),
}


# -- what the code defines ---------------------------------------------------------------

def _assigned(source, name):
    """The literal value assigned to `name` at module level of `source`."""
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(getattr(t, 'id', None) == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError(f'{name} not assigned')


def known_metrics(sources):
    names = set()
    for source in sources:
        names.update(re.findall(r"""['"](ava_[a-z0-9_]+)""", source))
    return names


def label_tables(tables=LABEL_TABLES):
    """{metric: (label, frozenset(values))} read from the source tuples."""
    return {metric: (label, frozenset(_assigned(path.read_text(encoding='utf-8'), name)))
            for metric, (label, path, name) in tables.items()}


def live_readiness_reasons(health_source):
    return set(re.findall(r"return False, '([a-z_]+)'", health_source))


def api_readiness_reasons(app_source):
    """The fixed reasons of the API's `/readyz` (a model error's text is not fixed)."""
    match = re.search(r"@app\.get\('/readyz'\)(.*?)(?=\n    @app\.)", app_source, re.S)
    if match is None:
        raise ValueError('API /readyz route not found')
    body = match.group(1)
    return set(re.findall(r"'reason': '([^']+)'", body)) | set(re.findall(r"reason = '([^']+)' if", body))


def live_log_events(eventlog_source, config_source, cli_sources):
    """(every event the live service can log, the warning/error ones)."""
    deltas = _assigned(eventlog_source, 'DELTAS')
    hub = _assigned(eventlog_source, 'HUB_FIELDS')
    levels = _assigned(eventlog_source, 'HUB_LEVELS')
    outcomes = _assigned(config_source, 'OUTCOMES')
    events = {event for _, _, event, _ in deltas}
    events |= {name.replace('.', '_') for name in hub}
    events |= {f'config_{"reloaded" if o == "applied" else o}' for o in outcomes}
    events |= set(re.findall(r"'event': '([a-z_]+)'", eventlog_source))
    for source in cli_sources:
        events |= set(re.findall(r"'event': '([a-z_]+)'", source))
    alarming = {event for _, _, event, level in deltas if level in ('warning', 'error')}
    alarming |= {name.replace('.', '_') for name, level in levels.items()
                 if level in ('warning', 'error') and name not in PRODUCT_EVENTS}
    alarming |= {f'config_{o}' for o in CONFIG_WARNINGS if o in outcomes}
    alarming |= set(REQUIRED_LIVE_EVENTS)
    return events, alarming


def api_log_events(oplog_source, app_source, main_source=''):
    relay = {event for _, event in _assigned(oplog_source, 'RELAY_DELTAS')}
    events = set(relay)
    events |= set(re.findall(r"\bemit\('([a-z_]+)'", oplog_source))
    events |= set(re.findall(r"'event': '([a-z_]+)'", oplog_source))
    events |= set(re.findall(r"_emit\('([a-z_]+)'", app_source))
    events |= set(re.findall(r"'event': '([a-z_]+)'", main_source))
    return events, relay


def panel_titles(dashboard_json):
    return {panel['title'] for panel in json.loads(dashboard_json)['panels'] if panel.get('type') != 'row'}


# -- what the runbooks cite --------------------------------------------------------------

STRICT_HEADING = re.compile(r'^## (R\d+)\. \S[^\n]*$', re.M)


def runbook_sections(text):
    """{'R1': body, ...} for each `## R<n>. Title` section (body up to the next `## `)."""
    sections = {}
    matches = list(STRICT_HEADING.finditer(text))
    for match in matches:
        rest = text[match.end():]
        end = re.search(r'^## ', rest, re.M)
        sections.setdefault(match.group(1), rest if end is None else rest[:end.start()])
    return sections


def heading_problems(text):
    problems = []
    loose = re.findall(r'^##\s*R\d+[^\n]*$', text, re.M)
    strict = [m.group(0) for m in STRICT_HEADING.finditer(text)]
    problems += [f'malformed runbook heading {line!r}' for line in loose if line not in strict]
    names = [m.group(1) for m in STRICT_HEADING.finditer(text)]
    problems += [f'duplicate runbook {name}' for name in sorted({n for n in names if names.count(n) > 1})]
    return problems


def missing_parts(text):
    problems = heading_problems(text)
    sections = runbook_sections(text)
    if not sections:
        return problems + ['no runbook sections']
    for name, body in sections.items():
        present = re.findall(r'^\*\*([A-Za-z]+)\.\*\*', body, re.M)
        if present != list(REQUIRED_PARTS):
            problems.append(f'{name} parts {present} (want {list(REQUIRED_PARTS)})')
            continue
        for part, content in re.findall(r'^\*\*([A-Za-z]+)\.\*\*(.*?)(?=^\*\*[A-Za-z]+\.\*\*|\Z)', body, re.S | re.M):
            if not content.strip():
                problems.append(f'{name} part {part} is empty')
    return problems


def index_problems(text):
    """Every runbook is in the index, and the index names no missing runbook."""
    sections = set(runbook_sections(text))
    match = re.search(r'^## Index\n(.*?)(?=^## )', text, re.S | re.M)
    if match is None:
        return ['no index']
    indexed = set(re.findall(r'\|\s*(R\d+)\s*\|\s*$', match.group(1), re.M))
    return ([f'{name} not in the index' for name in sorted(sections - indexed)]
            + [f'index names missing {name}' for name in sorted(indexed - sections)])


def cited_metrics(text):
    """{(name, label, value)}: label and value are None without a label."""
    cited = set()
    for token in re.findall(r'`(ava_[a-z0-9_]+(?:\{[^`]*\})?)`', text):
        if token in NOT_METRICS:
            continue
        match = re.fullmatch(r'(ava_[a-z0-9_]+)(?:\{([a-z_]+)="([^"]+)"\})?', token)
        if match is None:
            cited.add((token, 'malformed', None))
        else:
            cited.add(match.groups())
    return cited


def metric_problems(text, metrics, tables):
    problems = []
    for name, label, value in sorted(cited_metrics(text), key=str):
        base = re.sub(r'_(bucket|sum|count)$', '', name)
        if label == 'malformed':
            problems.append(f'malformed metric reference {name!r}')
        elif name not in metrics and base not in metrics:
            problems.append(f'unknown metric {name}')
        elif value is not None:
            if name not in tables:
                problems.append(f'no label table for {name}')
            elif tables[name][0] != label:
                problems.append(f'{name} has no label {label} (it has {tables[name][0]})')
            elif value not in tables[name][1]:
                problems.append(f'unknown label value {name}{{{label}="{value}"}}')
    return problems


def _flat(text):
    """Line breaks inside a sentence or a quoted title are ordinary spaces."""
    return re.sub(r'\s+', ' ', text)


def _listed(text, word, quote, item):
    """Items named after `word` (e.g. log, logs): `word Q1`, `word Q1, Q2 or Q3`."""
    q = re.escape(quote)
    found = set()
    pattern = rf'\b(?i:{word}) ((?:{q}{item}{q}(?:,? (?:or |and |then )?)?)+)'
    for group in re.findall(pattern, _flat(text)):
        found.update(re.findall(rf'{q}({item}){q}', group))
    return found


def cited_log_events(text):
    return _listed(text, 'logs?', '`', '[a-z_]+')


def cited_reasons(text):
    return _listed(text, 'reasons?', '`', '[^`]+')


def cited_panels(text):
    return _listed(text, 'panels?', '"', '[^"]+')


def cited_guide_sections(text):
    return set(re.findall(r'deployment guide(?:\'s)?,? "([^"]+)"', _flat(text)))


def coverage_problems(text, required_reasons, required_events):
    """Coverage counts only mentions inside runbook sections, not the intro or index."""
    body = ''.join(runbook_sections(text).values())
    reasons, events = cited_reasons(body), cited_log_events(body)
    return ([f'no runbook for readiness reason {r}' for r in sorted(required_reasons - reasons)]
            + [f'no runbook for log event {e}' for e in sorted(required_events - events)])


class RealRunbookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = RUNBOOKS.read_text(encoding='utf-8')
        cls.sources = [path.read_text(encoding='utf-8') for path in SERVICES.rglob('*.py')]
        cls.health = HEALTH.read_text(encoding='utf-8')
        cls.api_reasons = api_readiness_reasons(API_APP.read_text(encoding='utf-8'))
        cls.live_events, cls.live_alarming = live_log_events(
            EVENTLOG.read_text(encoding='utf-8'), CONFIG_RELOAD.read_text(encoding='utf-8'),
            [LIVE_MAIN.read_text(encoding='utf-8')])
        cls.api_events, cls.relay_events = api_log_events(
            API_OPLOG.read_text(encoding='utf-8'), API_APP.read_text(encoding='utf-8'),
            API_MAIN.read_text(encoding='utf-8'))

    def test_every_runbook_has_all_parts_in_order(self):
        self.assertGreaterEqual(len(runbook_sections(self.text)), 14)
        self.assertEqual(missing_parts(self.text), [])

    def test_the_index_lists_every_runbook(self):
        self.assertEqual(index_problems(self.text), [])

    def test_every_cited_metric_and_label_value_exists(self):
        self.assertGreater(len(cited_metrics(self.text)), 30)
        self.assertEqual(metric_problems(self.text, known_metrics(self.sources), label_tables()), [])

    def test_every_cited_log_event_exists(self):
        cited = cited_log_events(self.text)
        self.assertGreater(len(cited), 15)
        self.assertEqual(sorted(cited - self.live_events - self.api_events), [])

    def test_every_cited_readiness_reason_exists(self):
        self.assertEqual(self.api_reasons, {'busy', 'job worker stopped'})
        known = live_readiness_reasons(self.health) | self.api_reasons
        self.assertEqual(sorted(cited_reasons(self.text) - known), [])

    def test_every_cited_panel_exists(self):
        cited = cited_panels(self.text)
        self.assertGreater(len(cited), 8)
        self.assertEqual(sorted(cited - panel_titles(DASHBOARD.read_text(encoding='utf-8'))), [])

    def test_every_alarming_signal_has_a_runbook(self):
        reasons = live_readiness_reasons(self.health)
        self.assertEqual(reasons, {'ari_disconnected', 'loop_not_started', 'loop_stalled', 'detector_failing'})
        for event in REQUIRED_LIVE_EVENTS + ('tap_failure',):
            self.assertIn(event, self.live_events)
        self.assertEqual(coverage_problems(self.text, reasons | self.api_reasons,
                                           self.live_alarming | self.relay_events), [])

    def test_every_cited_flag_and_guide_section_exists(self):
        known = cli_flags(p.read_text(encoding='utf-8') for p in CLI_SOURCES) | OTHER_TOOL_FLAGS
        self.assertIn('--max-calls', known)
        self.assertEqual(unknown_flags(self.text, known), [])
        cited = cited_guide_sections(self.text)
        self.assertGreaterEqual(len(cited), 3)
        self.assertEqual(sorted(cited - set(headings(GUIDE.read_text(encoding='utf-8')))), [])

    def test_every_cited_path_and_test_exists(self):
        self.assertEqual(missing_paths(self.text), [])
        sources = [path.read_text(encoding='utf-8') for path in (ROOT / 'tests').rglob('test_*.py')]
        self.assertGreater(len(re.findall(r'`test_[a-z0-9_]+`', self.text)), 10)
        self.assertEqual(missing_tests(self.text, sources), [])


class SyntheticRuleTests(unittest.TestCase):
    def runbook(self, *parts, name='R1'):
        body = '\n\n'.join(f'**{part}.** text' for part in parts)
        return f'## {name}. Title\n\n{body}\n'

    def test_a_missing_reordered_or_empty_part_is_reported(self):
        self.assertEqual(missing_parts(self.runbook(*REQUIRED_PARTS)), [])
        self.assertEqual(len(missing_parts(self.runbook('Signal', 'Calls', 'Act', 'Tested'))), 1)
        self.assertEqual(len(missing_parts(self.runbook('Calls', 'Signal', 'Check', 'Act', 'Tested'))), 1)
        empty = self.runbook(*REQUIRED_PARTS).replace('**Check.** text', '**Check.**  ')
        self.assertEqual(missing_parts(empty), ['R1 part Check is empty'])
        self.assertEqual(missing_parts('no sections'), ['no runbook sections'])

    def test_malformed_and_duplicate_headings_are_reported(self):
        good = self.runbook(*REQUIRED_PARTS)
        self.assertEqual(missing_parts(good + '## R2 No period\n\n**Signal.** x\n'),
                         ["malformed runbook heading '## R2 No period'"])
        self.assertEqual(missing_parts(good + good), ['duplicate runbook R1'])

    def test_the_index_must_match_the_sections(self):
        index = '## Index\n\n| See | Runbook |\n|---|---|\n| a | R1 |\n| b | R3 |\n\n'
        text = index + self.runbook(*REQUIRED_PARTS) + self.runbook(*REQUIRED_PARTS, name='R2')
        self.assertEqual(index_problems(text), ['R2 not in the index', 'index names missing R3'])

    def test_unknown_metrics_and_wrong_label_values_are_reported(self):
        text = ('`ava_live_ready`, `ava_live_gone_total`, `ava_x_seconds_bucket`, '
                '`ava_live_tap_events_total{kind="swept"}`, `ava_live_tap_events_total{kind="errors"}`, '
                '`ava_live_tap_events_total{outcome="swept"}`, `ava_x_seconds{stage="a"}`, '
                '`ava_live_ready{kind=swept}`')
        tables = {'ava_live_tap_events_total': ('kind', frozenset({'swept'})),
                  'ava_live_sink_events_total': ('kind', frozenset({'errors'}))}
        problems = metric_problems(text, {'ava_live_ready', 'ava_x_seconds', 'ava_live_tap_events_total'}, tables)
        self.assertEqual(sorted(problems), sorted([
            'unknown metric ava_live_gone_total',
            'unknown label value ava_live_tap_events_total{kind="errors"}',  # a value of another metric
            'ava_live_tap_events_total has no label outcome (it has kind)',
            'no label table for ava_x_seconds',
            "malformed metric reference 'ava_live_ready{kind=swept}'"]))

    def test_label_tables_are_read_from_the_source_tuples(self):
        tables = label_tables()
        self.assertIn('swept', tables['ava_live_tap_events_total'][1])
        self.assertNotIn('errors', tables['ava_live_tap_events_total'][1])
        self.assertEqual(tables['ava_live_config_reloads_total'],
                         ('outcome', frozenset({'applied', 'unchanged', 'rejected', 'unreadable'})))

    def test_log_events_reasons_panels_and_sections_are_read_from_their_phrases(self):
        text = ('log `ari_disconnected`, then; logs `relay_gap`, `relay_interruption` or `relay_x`; '
                '`not_a_log`; reason `loop_stalled`; panel "Queue depth"; panels "A" and "B"; Log `config_x`; '
                'log `split`,\n`line`; (deployment guide, "Upgrade and\nrollback"); the deployment guide\'s "Hardware"')
        self.assertEqual(cited_log_events(text), {'ari_disconnected', 'relay_gap', 'relay_interruption', 'relay_x',
                                                  'config_x', 'split', 'line'})
        self.assertEqual(cited_reasons(text), {'loop_stalled'})
        self.assertEqual(cited_panels(text), {'Queue depth', 'A', 'B'})
        self.assertEqual(cited_guide_sections(text), {'Upgrade and rollback', 'Hardware'})

    def test_an_uncovered_reason_or_event_is_reported_and_the_index_does_not_count(self):
        text = ('## Index\n\n| reason `loop_stalled`, log `sink_error` | R1 |\n\n'
                + self.runbook(*REQUIRED_PARTS).replace('**Signal.** text',
                                                        '**Signal.** reason `ari_disconnected`; log `tap_failure`'))
        self.assertEqual(coverage_problems(text, {'ari_disconnected', 'loop_stalled'}, {'tap_failure', 'sink_error'}),
                         ['no runbook for readiness reason loop_stalled', 'no runbook for log event sink_error'])

    def test_event_and_reason_parsing_follows_the_sources(self):
        eventlog = ("DELTAS = (('service', 'connects', 'ari_connected', 'info'),\n"
                    "          ('tap', 'tap_failures', 'tap_failure', 'error'))\n"
                    "HUB_FIELDS = {'call.started': (), 'system.health': ('reason',), 'alert.raised': ()}\n"
                    "HUB_LEVELS = {'system.health': 'warning', 'alert.raised': 'warning'}\n"
                    "x = {'event': 'detector_failing'}\n")
        config = "OUTCOMES = ('applied', 'rejected')\n"
        events, alarming = live_log_events(eventlog, config, ["{'event': 'refused'}"])
        self.assertEqual(events, {'ari_connected', 'tap_failure', 'call_started', 'system_health', 'alert_raised',
                                  'detector_failing', 'config_reloaded', 'config_rejected', 'refused'})
        self.assertEqual(alarming, {'tap_failure', 'system_health', 'config_rejected', *REQUIRED_LIVE_EVENTS})
        health = "    return False, 'ari_down', d\n    return True, None, d\n"
        self.assertEqual(live_readiness_reasons(health), {'ari_down'})
        app = ("    @app.get('/readyz')\n    async def ready():\n"
               "        return JSONResponse({'status': 'x', 'reason': 'stopped'})\n"
               "        reason = 'busy' if isinstance(e, B) else str(e)\n\n    @app.get('/metrics')\n")
        self.assertEqual(api_readiness_reasons(app), {'stopped', 'busy'})
        oplog = "RELAY_DELTAS = (('gaps', 'relay_gap'),)\ndef request(self):\n    return self.emit('request')\n"
        self.assertEqual(api_log_events(oplog, "self._emit('job_done', 'info')", "{'event': 'refused'}"),
                         ({'relay_gap', 'request', 'job_done', 'refused'}, {'relay_gap'}))
        dashboard = json.dumps({'panels': [{'type': 'row', 'title': 'Row'}, {'type': 'stat', 'title': 'Up'}]})
        self.assertEqual(panel_titles(dashboard), {'Up'})


if __name__ == '__main__':
    unittest.main()
