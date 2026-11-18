"""Claude-authored tests (18 November 2026) for the Compose monitoring profile: the
Prometheus scrape configuration and the provisioned Grafana dashboard (backlog 7.2;
PRD FR-O6 "Prometheus metrics (latency per stage, queue depth, active calls, alerts
per minute, dropped windows); Grafana dashboard"; PRD constraint: no telemetry).

Authored by Claude (Claude Code), not by GPT-6 Astra. File checks without Docker; the
running stack is tests/lab/test_compose_stack_lab.py. The central check: every series a
dashboard query names, and every label it filters or groups by, exists in the `/metrics`
text the services really render (a renamed metric must fail here instead of leaving an
empty panel). The live process's text is rendered with the real HealthServer and a real
LiveService on fakes; the API's with the real app when requirements-api.txt is
installed (not on CI), else only by name against its source.
"""
import ipaddress
import json
from pathlib import Path
import re
import unittest

from services.live.background import PumpThread
from services.live.health import HealthServer
from services.store.pump import StorePump
from tests.unit.test_live_health import ManualClock, make_service

ROOT = Path(__file__).resolve().parents[2]
MONITORING = ROOT / 'deploy' / 'compose' / 'monitoring'
DASHBOARD = MONITORING / 'grafana' / 'dashboards' / 'ava-overview.json'
DATASOURCE = MONITORING / 'grafana' / 'provisioning' / 'datasources' / 'prometheus.yaml'
PROVIDER = MONITORING / 'grafana' / 'provisioning' / 'dashboards' / 'ava.yaml'
PROMETHEUS = MONITORING / 'prometheus.yml'
COMPOSE = ROOT / 'deploy' / 'compose' / 'compose.yaml'
API_SOURCE = ROOT / 'services' / 'api' / 'app.py'

SELECTOR = re.compile(r'\b(ava_[a-z0-9_]+|up)\b(\{([^}]*)\})?')
MATCHER = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*(=~|!~|!=|=)\s*"([^"]*)"')
RATED = re.compile(r'\b(?:rate|irate|increase)\(\s*(ava_[a-z0-9_]+)')
GROUPING = re.compile(r'\b(?:by|without)\s*\(([^)]*)\)')
SAMPLE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})? \S+$')
LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="([^"]*)"')
# PRD FR-O6 item -> a series its panel must query.
FR_O6 = {'latency per stage': 'ava_live_stage_seconds_bucket', 'queue depth': 'ava_live_windows_waiting',
         'active calls': 'ava_live_calls_active', 'alerts per minute': 'ava_live_alerts_raised_total',
         'dropped windows': 'ava_live_windows_resolved_total'}


def series_labels(text):
    """`/metrics` text -> {series name: [label dict per sample]}."""
    found = {}
    for line in text.splitlines():
        if not line or line.startswith('#'):
            continue
        match = SAMPLE.match(line)
        assert match, line
        found.setdefault(match.group(1), []).append(dict(LABEL.findall(match.group(2) or '')))
    return found


def family_types(text):
    """`/metrics` text -> {series name: TYPE}; a histogram's _bucket/_sum/_count series
    get `histogram`."""
    types = {}
    for name, kind in re.findall(r'^# TYPE (\S+) (\S+)$', text, re.M):
        types[name] = kind
        if kind == 'histogram':
            types.update({f'{name}_{part}': kind for part in ('bucket', 'sum', 'count')})
    return types


def live_metrics_text():
    """The live process's /metrics with every optional section present: a store pump
    and a Redis forwarder pump, as `python -m services.live` runs them."""
    service = make_service(ManualClock())
    service.ready.set()
    service._tick()
    subscription = service.hub.subscribe(buffer=8)

    class Store:
        def record(self, envelope):
            return 'stored'
    store = StorePump(subscription, Store())
    pumps = [{'thread': PumpThread('store', store.pump), 'resource': None,
              'report': lambda: {'counts': dict(store.counts), 'backlog': store.backlog,
                                 'consecutive_failures': store.consecutive_failures}},
             {'thread': PumpThread('redis', lambda: 0), 'resource': None,
              'report': lambda: {'counts': {'forwarded': 0, 'publish_failures': 0, 'no_receivers': 0}}}]
    health = HealthServer(service, pumps=pumps, port=0)
    try:
        return health.metrics()
    finally:
        health.stop()


def api_metrics_text():
    """The API's /metrics as the Compose `api` runs it (event hub and Redis relay), or
    None without requirements-api.txt."""
    from tests.unit import test_analysis_api as api_fixture
    if api_fixture.TestClient is None:
        return None
    from services.api.app import create_app, relay_lines
    from services.events.fanout import EventHub
    from services.inference import analysis
    service = analysis.AnalysisService(lambda: (lambda samples: [0.0, 1.0]), model_id='fx')
    with api_fixture.TestClient(create_app(service, hub=EventHub())) as client:
        text = client.get('/metrics').text
    # The relay families come from the RelayLoop the CLI attaches; render them directly.
    return text + '\n'.join(relay_lines({'relay': {}, 'subscribed': True, 'alive': True, 'faults': 0})) + '\n'


def queries(dashboard):
    for panel in dashboard['panels']:
        for target in panel.get('targets', []):
            yield panel['title'], target['expr']


class DashboardFileTests(unittest.TestCase):
    def setUp(self):
        self.dashboard = json.loads(DASHBOARD.read_text(encoding='utf-8'))

    def test_panels_are_unique_non_overlapping_and_use_the_provisioned_source(self):
        uid = re.search(r'^\s*uid:\s*(\S+)\s*$', DATASOURCE.read_text(encoding='utf-8'), re.M).group(1)
        self.assertEqual(uid, 'ava-prometheus')
        panels = self.dashboard['panels']
        ids = [panel['id'] for panel in panels]
        self.assertEqual(len(ids), len(set(ids)))
        cells = {}
        for panel in panels:
            box = panel['gridPos']
            self.assertLessEqual(box['x'] + box['w'], 24, panel['title'])
            for x in range(box['x'], box['x'] + box['w']):
                for y in range(box['y'], box['y'] + box['h']):
                    self.assertNotIn((x, y), cells, f'{panel["title"]} overlaps {cells.get((x, y))}')
                    cells[(x, y)] = panel['title']
            if panel['type'] == 'row':
                continue
            self.assertEqual(panel['datasource'], {'type': 'prometheus', 'uid': uid}, panel['title'])
            self.assertTrue(panel.get('description'), f'{panel["title"]} explains itself')
            refs = [target['refId'] for target in panel['targets']]
            self.assertTrue(refs and len(refs) == len(set(refs)), panel['title'])
        self.assertEqual(self.dashboard['uid'], 'ava-overview')
        self.assertFalse(self.dashboard['editable'])

    def test_every_fr_o6_item_has_a_panel(self):
        exprs = ' '.join(expr for _, expr in queries(self.dashboard))
        for item, series in FR_O6.items():
            self.assertRegex(exprs, rf'\b{series}\b', f'FR-O6 {item}')

    def test_the_provider_loads_the_mounted_folder_read_only(self):
        provider = PROVIDER.read_text(encoding='utf-8')
        self.assertIn('path: /etc/grafana/dashboards', provider)
        self.assertIn('allowUiUpdates: false', provider)
        compose = COMPOSE.read_text(encoding='utf-8')
        self.assertIn('./monitoring/grafana/dashboards:/etc/grafana/dashboards:ro', compose)
        self.assertIn('./monitoring/grafana/provisioning:/etc/grafana/provisioning:ro', compose)
        self.assertIn('./monitoring/prometheus.yml:/etc/prometheus/prometheus.yml:ro', compose)


class QueriesMatchRenderedMetricsTests(unittest.TestCase):
    """Every queried series and label exists in what the services render."""

    def setUp(self):
        self.dashboard = json.loads(DASHBOARD.read_text(encoding='utf-8'))
        self.jobs = set(re.findall(r'job_name:\s*(\S+)', PROMETHEUS.read_text(encoding='utf-8')))

    def check(self, rendered, accept, types):
        """`accept(name)` picks the series this rendering is responsible for; `types` maps
        series to their TYPE. Returns (series references checked, problems found)."""
        checked, problems = 0, []
        for title, expr in queries(self.dashboard):
            # Review L7: rate/irate/increase only make sense on counters (and histogram parts).
            for name in RATED.findall(expr):
                if accept(name) and name in types and types[name] not in ('counter', 'histogram'):
                    problems.append(f'{title}: rate/increase over the {types[name]} {name}')
            selected = [(m.group(1), m.group(3) or '') for m in SELECTOR.finditer(expr)]
            if not selected:
                problems.append(f'{title}: no metric in {expr}')
            ours = [(name, matchers) for name, matchers in selected if accept(name)]
            for name, matchers in ours:
                checked += 1
                if name not in rendered:
                    problems.append(f'{title}: {name} is not exported')
                    continue
                for label, op, value in MATCHER.findall(matchers):
                    values = {sample.get(label) for sample in rendered[name]} - {None}
                    if not values:
                        problems.append(f'{title}: {name} has no label {label}')
                    elif op == '=' and value not in values:
                        problems.append(f'{title}: {name}{{{label}="{value}"}} is never rendered')
                    elif op == '=~' and not any(re.fullmatch(value, v) for v in values):
                        problems.append(f'{title}: {name}{{{label}=~"{value}"}} matches nothing')
            if ours:
                # Grouping labels must exist on a queried series (le on a bucket series).
                for group in GROUPING.findall(expr):
                    for label in filter(None, (part.strip() for part in group.split(','))):
                        if not any(label in sample for name, _ in ours for sample in rendered.get(name, ())):
                            problems.append(f'{title}: no queried series has label {label}')
        return checked, problems

    def test_live_panels_match_the_live_process_metrics(self):
        text = live_metrics_text()
        checked, problems = self.check(series_labels(text), lambda name: name.startswith('ava_live_'),
                                       family_types(text))
        self.assertEqual(problems, [])
        self.assertGreaterEqual(checked, 25)

    def test_api_panels_match_the_api_metrics(self):
        text = api_metrics_text()
        if text is None:
            self.skipTest('requirements-api.txt not installed (the by-name check below still runs)')
        checked, problems = self.check(series_labels(text), lambda name: name.startswith('ava_')
                                       and not name.startswith('ava_live_'), family_types(text))
        self.assertEqual(problems, [])
        self.assertGreaterEqual(checked, 8)

    def test_api_series_are_named_in_the_api_source(self):
        # Runs everywhere (CI has no FastAPI): the family name must appear in app.py.
        source = API_SOURCE.read_text(encoding='utf-8')
        names = {m.group(1) for _, expr in queries(self.dashboard) for m in SELECTOR.finditer(expr)
                 if m.group(1).startswith('ava_') and not m.group(1).startswith('ava_live_')}
        self.assertGreaterEqual(len(names), 6)
        for name in names:
            family = re.sub(r'_(bucket|sum|count)$', '', name)
            self.assertIn(family, source, name)

    def test_up_panels_name_configured_jobs(self):
        self.assertEqual(self.jobs, {'ava-live', 'ava-api'})
        for title, expr in queries(self.dashboard):
            for match in SELECTOR.finditer(expr):
                if match.group(1) != 'up':
                    continue
                for label, op, value in MATCHER.findall(match.group(3) or ''):
                    self.assertEqual(label, 'job')
                    hits = {job for job in self.jobs if re.fullmatch(value, job)} if op == '=~' else \
                        ({value} & self.jobs)
                    self.assertEqual(hits, self.jobs if op == '=~' else {value}, title)

    def test_the_checker_catches_a_renamed_metric_and_label(self):
        # The check itself is not vacuous.
        text = live_metrics_text()
        rendered, types = series_labels(text), family_types(text)
        live = lambda name: name.startswith('ava_live_')  # noqa: E731
        cases = {'rate(ava_live_alerts_raisd_total[5m])': 'is not exported',
                 'rate(ava_live_windows_waiting[5m])': 'over the gauge',
                 'increase( ava_live_calls_active[5m])': 'over the gauge',
                 'rate(ava_live_windows_resolved_total{outcome="droped"}[5m])': 'is never rendered',
                 'rate(ava_live_windows_resolved_total{reason!="x"}[5m])': 'has no label reason',
                 'ava_live_pump_alive{pump=~"stor"}': 'matches nothing',
                 'sum by (route) (ava_live_windows_waiting)': 'has label route',
                 'vector(1)': 'no metric'}
        for expr, message in cases.items():
            self.dashboard = {'panels': [{'title': 'bad', 'targets': [{'expr': expr}]}]}
            _, problems = self.check(rendered, live, types)
            self.assertEqual(len(problems), 1, (expr, problems))
            self.assertIn(message, problems[0], expr)
        self.dashboard = {'panels': [{'title': 'good', 'targets': [
            {'expr': 'sum by (le, stage) (rate(ava_live_stage_seconds_bucket{stage="apply"}[5m]))'}]}]}
        self.assertEqual(self.check(rendered, live, types), (1, []))


class ComposeMonitoringTests(unittest.TestCase):
    """Text checks of compose.yaml (no YAML parser in the CI environment)."""

    def setUp(self):
        self.compose = COMPOSE.read_text(encoding='utf-8')

    def service(self, name):
        match = re.search(rf'^  {name}:\n(.*?)(?=^  [a-z]+:\n|^[a-z]+:)', self.compose, re.M | re.S)
        self.assertIsNotNone(match, name)
        return match.group(1)

    def test_monitoring_images_are_pinned_by_version_and_digest_and_opt_in(self):
        for name, image in (('prometheus', 'prom/prometheus'), ('grafana', 'grafana/grafana-oss')):
            block = self.service(name)
            self.assertRegex(block, rf'image: {re.escape(image)}:v?[0-9][0-9.]*@sha256:[0-9a-f]{{64}}\n', name)
            self.assertIn('profiles: [monitoring]', block, name)
            self.assertIn('networks: [metrics]', block, f'{name} is on the scrape network only')
            for hardening in ('read_only: true', 'cap_drop: [ALL]', 'no-new-privileges:true', 'mem_limit:'):
                self.assertIn(hardening, block, f'{name}: {hardening}')
        self.assertNotIn('ports:', self.compose, 'nothing is published on the host')

    def test_grafana_never_calls_home_and_needs_a_password(self):
        block = self.service('grafana')
        for key in ('GF_ANALYTICS_REPORTING_ENABLED', 'GF_ANALYTICS_CHECK_FOR_UPDATES',
                    'GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES', 'GF_ANALYTICS_FEEDBACK_LINKS_ENABLED',
                    'GF_NEWS_NEWS_FEED_ENABLED', 'GF_PLUGINS_PLUGIN_ADMIN_ENABLED', 'GF_SNAPSHOTS_EXTERNAL_ENABLED',
                    'GF_PUBLIC_DASHBOARDS_ENABLED', 'GF_CLOUD_MIGRATION_ENABLED', 'GF_AUTH_ANONYMOUS_ENABLED',
                    'GF_USERS_ALLOW_SIGN_UP'):
            self.assertRegex(block, rf'{key}: "false"', key)
        for key in ('GF_PLUGINS_PREINSTALL_DISABLED', 'GF_PLUGINS_PUBLIC_KEY_RETRIEVAL_DISABLED',
                    'GF_SECURITY_DISABLE_GRAVATAR'):
            self.assertRegex(block, rf'{key}: "true"', key)
        self.assertIn('GF_SECURITY_ADMIN_PASSWORD: ${AVA_GRAFANA_ADMIN_PASSWORD:?', block)
        self.assertIn('GF_LOG_MODE: console', block, 'no log file on the read-only root')
        example = (ROOT / 'deploy' / 'compose' / 'stack.env.example').read_text(encoding='utf-8')
        self.assertRegex(example, r'(?m)^AVA_GRAFANA_ADMIN_PASSWORD=')

    def test_live_health_binds_only_to_its_metrics_address(self):
        live = self.service('live')
        default = '${AVA_LIVE_METRICS_IP:-10.231.250.10}'
        self.assertIn(f'- --health-host\n', live)
        self.assertRegex(live, rf'- --health-host\n(?:\s*#[^\n]*\n)*\s*- {re.escape(default)}\n')
        self.assertIn(f'ipv4_address: {default}', live)
        self.assertIn(f"http://{default}:9100/readyz", live, 'the health check reads the same address')
        self.assertNotIn('0.0.0.0', live)
        networks = self.compose[self.compose.index('\nnetworks:\n'):]
        self.assertRegex(networks, r'metrics:\n(?:\s*#[^\n]*\n)*\s*internal: true\n')
        subnet = re.search(r'subnet: \$\{AVA_METRICS_SUBNET:-([0-9./]+)\}', networks)
        dynamic = re.search(r'ip_range: \$\{AVA_METRICS_DYNAMIC_RANGE:-([0-9./]+)\}', networks)
        self.assertTrue(subnet and dynamic, networks)
        # Review M1: live's fixed address is in the subnet but never handed to another container.
        address = ipaddress.ip_address('10.231.250.10')
        subnet, dynamic = ipaddress.ip_network(subnet.group(1)), ipaddress.ip_network(dynamic.group(1))
        self.assertIn(address, subnet)
        self.assertTrue(dynamic.subnet_of(subnet))
        self.assertNotIn(address, dynamic)
        # Review M2: outside Docker's default address pools (172.17-172.31.x, inside
        # 172.16.0.0/12, and 192.168.0.0/16).
        for pool in ('172.16.0.0/12', '192.168.0.0/16'):
            self.assertFalse(subnet.overlaps(ipaddress.ip_network(pool)), pool)
        self.assertIn("targets: ['live:9100']", PROMETHEUS.read_text(encoding='utf-8'))
        self.assertIn("targets: ['api:8000']", PROMETHEUS.read_text(encoding='utf-8'))
        self.assertIn('networks: [backend, metrics]', self.service('api'))


if __name__ == '__main__':
    unittest.main()
