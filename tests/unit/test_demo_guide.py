"""Claude-authored tests (22 November 2026; not GPT-6 Astra): the lab demo guide
docs/operations/demo.md stays true to the repository (backlog 11.2; PRD roadmap 5.9,
BO-4, BO-8).

Standard library only, so they run in the CI unit suite. Cases:
DG01 the guide has a step for each PRD 5.9 item (live tapped call, timeline, batch upload,
     egress proof) plus start and stop;
DG02 every repository path it cites exists;
DG03 every `python -m lab.demo` command parses with the real parser, and every lab.demo
     subcommand is shown;
DG04 the Compose commands use the compose file with the `lab` and `demo` profiles, the
     port it names is the compose default, and the env example documents AVA_DEMO_PORT;
DG05 the rehearsal it describes exists: the lab test, the spec with DR01-DR03 and the
     Playwright project;
DG06 synthetic guides show each rule catching its failure.
Passing says the guide's references exist; the lab test shows the demo works.
"""
import re
import shlex
import unittest
from pathlib import Path

from lab import demo

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / 'docs' / 'operations' / 'demo.md'
COMPOSE = ROOT / 'deploy' / 'compose' / 'compose.yaml'
SPEC = ROOT / 'tests' / 'browser' / 'compose-demo-rehearsal.spec.cjs'
LAB_TEST = ROOT / 'tests' / 'lab' / 'test_compose_demo_lab.py'
PLAYWRIGHT = ROOT / 'playwright.config.cjs'
ENV_EXAMPLE = ROOT / 'deploy' / 'compose' / 'stack.env.example'
REQUIRED_STEPS = ('the live board', 'a live tapped call', 'the timeline', 'batch upload', 'stored history',
                  'egress proof')
REQUIRED_SECTIONS = ('Start the stack', 'Stop', 'Rehearsal evidence')
PATH_PREFIXES = ('deploy/', 'services/', 'tests/', 'docs/', 'lab/', 'scripts/', 'models/', 'dashboard/', 'data/')


def step_problems(text):
    steps = [line[4:].lower() for line in text.splitlines() if line.startswith('### Step ')]
    sections = [re.sub(r'^\d+\.\s*', '', line[3:]).strip() for line in text.splitlines() if line.startswith('## ')]
    problems = [f'no step for {name!r}' for name in REQUIRED_STEPS if not any(name in step for step in steps)]
    return problems + [f'no section {name!r}' for name in REQUIRED_SECTIONS if name not in sections]


def cited_paths(text):
    """Repository paths in inline code and in the commands of code blocks."""
    inline = re.findall(r'(?<!`)`([^`\s]+)`(?!`)', text)
    commands = [token for block in re.findall(r'```text\n(.*?)```', text, re.S) for token in block.split()]
    return sorted({p.rstrip('.,') for p in inline + commands if p.startswith(PATH_PREFIXES)})


def path_problems(text):
    return [f'missing: {p}' for p in cited_paths(text) if not (ROOT / p).exists()]


def demo_commands(text):
    """Argument lists after `python -m lab.demo` in the guide's code blocks."""
    blocks = re.findall(r'```text\n(.*?)```', text, re.S)
    return [shlex.split(line.split('python -m lab.demo', 1)[1]) for block in blocks for line in block.splitlines()
            if 'python -m lab.demo' in line]


def demo_command_problems(text):
    problems, seen = [], set()
    parser = demo.build_parser()
    parser.exit = lambda status=0, message=None: (_ for _ in ()).throw(ValueError(message))
    parser.error = lambda message: (_ for _ in ()).throw(ValueError(message))
    for sub in parser._subparsers._group_actions[0].choices.values():
        sub.error = parser.error
    for argv in demo_commands(text):
        try:
            args = parser.parse_args(argv)
        except (ValueError, SystemExit) as error:
            problems.append(f'lab.demo {" ".join(argv)}: {error}')
            continue
        seen.add(args.command)
    subcommands = set(parser._subparsers._group_actions[0].choices)
    return problems + [f'lab.demo {name} not shown' for name in sorted(subcommands - seen)]


def compose_problems(text, compose_text, env_example):
    problems = []
    lines = [line for block in re.findall(r'```text\n(.*?)```', text, re.S) for line in block.splitlines()
             if line.startswith('docker compose ')]
    if not lines:
        problems.append('no docker compose command')
    for line in lines:
        if '-f deploy/compose/compose.yaml' not in line or '--profile lab --profile demo' not in line:
            problems.append(f'compose command without the file or the lab and demo profiles: {line}')
    default = re.search(r'"127\.0\.0\.1:\$\{AVA_DEMO_PORT:-(\d+)\}:8080"', compose_text)
    if default is None:
        problems.append('compose.yaml has no loopback demo port')
    else:
        # Dashboard addresses only: Prometheus and Grafana URLs (`/api/...`) are inside
        # their containers.
        ports = set(re.findall(r'http://127\.0\.0\.1:(\d+)/(?!api/)', text))
        if ports != {default.group(1)}:
            problems.append(f'guide ports {sorted(ports)} differ from the compose default {default.group(1)}')
    if not re.search(r'(?m)^# AVA_DEMO_PORT=', env_example):
        problems.append('stack.env.example does not document AVA_DEMO_PORT')
    if 'AVA_DEMO_PORT' not in text:
        problems.append('the guide does not name AVA_DEMO_PORT')
    return problems


class DemoGuideTests(unittest.TestCase):
    def setUp(self):
        self.text = GUIDE.read_text(encoding='utf-8')

    def test_dg01_every_5_9_item_has_a_step(self):
        self.assertEqual(step_problems(self.text), [])

    def test_dg02_cited_paths_exist(self):
        self.assertGreater(len(cited_paths(self.text)), 8)
        self.assertEqual(path_problems(self.text), [])

    def test_dg03_lab_demo_commands_parse(self):
        self.assertGreaterEqual(len(demo_commands(self.text)), 3)
        self.assertEqual(demo_command_problems(self.text), [])

    def test_dg04_compose_commands_profiles_and_port(self):
        self.assertEqual(compose_problems(self.text, COMPOSE.read_text(encoding='utf-8'),
                                          ENV_EXAMPLE.read_text(encoding='utf-8')), [])

    def test_dg05_the_rehearsal_exists(self):
        spec = SPEC.read_text(encoding='utf-8')
        for case in ('DR01', 'DR02', 'DR03'):
            self.assertIn(f"test('{case} ", spec)
            self.assertIn(f'{case}:', self.text, f'{case} described in the guide')
        self.assertIn("name: 'compose-demo'", PLAYWRIGHT.read_text(encoding='utf-8'))
        self.assertIn('--project=compose-demo', LAB_TEST.read_text(encoding='utf-8'))
        self.assertIn('python -m unittest tests.lab.test_compose_demo_lab', self.text)

    def test_dg06_each_rule_catches_its_failure(self):
        self.assertIn("no step for 'egress proof'", step_problems(self.text.replace('### Step 6: egress proof', '### Step 6')))
        self.assertIn('missing: lab/no_such_helper.py', path_problems(self.text + '\nSee `lab/no_such_helper.py`.\n'))
        bad = self.text + '\n```text\nuv run python -m lab.demo call --env-file x.env --codec opus --wav a.wav\n```\n'
        self.assertTrue(any('--codec' in p or 'opus' in p for p in demo_command_problems(bad)), demo_command_problems(bad))
        without_egress = re.sub(r'python -m lab\.demo egress[^\n]*', 'true', self.text)
        self.assertIn('lab.demo egress not shown', demo_command_problems(without_egress))
        compose_text = COMPOSE.read_text(encoding='utf-8')
        env_example = ENV_EXAMPLE.read_text(encoding='utf-8')
        self.assertTrue(compose_problems(self.text.replace('127.0.0.1:8080/#live', '127.0.0.1:9090/#live'),
                                         compose_text, env_example))
        self.assertTrue(compose_problems(self.text.replace('--profile lab --profile demo up', '--profile lab up'),
                                         compose_text, env_example))
        self.assertIn('stack.env.example does not document AVA_DEMO_PORT',
                      compose_problems(self.text, compose_text, env_example.replace('# AVA_DEMO_PORT=', '# X=')))


if __name__ == '__main__':
    unittest.main()
