"""Claude-authored tests (22 November 2026): the FR-O8 deployment guide stays true to the
repository (backlog 11.2; PRD FR-O8, §6.3).

Authored by Claude (Claude Code), not by GPT-6 Astra. Standard library only, so they run
in the CI unit suite. A guide that names a removed flag, file, test or setting, drops a
required FR-O8 section, misses a PRD §6.3 item, or gives backup commands that differ
from the ones the lab test runs, fails here. Each rule also has a synthetic case showing
that it catches its failure. Passing says the guide's references exist; it does not say
the deployment works (that is the lab tests' job).
"""
import re
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / 'docs' / 'operations' / 'deployment-guide.md'
PRD = ROOT / '_bmad-output' / 'planning-artifacts' / 'prd.md'
LAB_TEST = ROOT / 'tests' / 'lab' / 'test_compose_stack_lab.py'
CLI_SOURCES = [ROOT / 'services' / name / '__main__.py' for name in ('live', 'api', 'auth', 'store')]

# The FR-O8 items, each a section heading of the guide.
REQUIRED_SECTIONS = ('Hardware', 'PBX tap configuration', 'Recording-failure policy (FR-T4)',
                     'Upgrade and rollback', 'Backup and restore', 'Security controls (PRD §6.3)')
# Options of tools other than AVA's (Docker Compose, pg_dump, pg_restore) the guide uses.
EXTERNAL_FLAGS = frozenset({'--env-file', '--profile', '--wait', '--format', '--file', '--list', '--no-owner',
                            '--no-privileges', '--single-transaction', '--exit-on-error'})
MIN_ROW_WORDS = 3  # a security row quotes at least this many words of its PRD item
PATH_PREFIXES = ('deploy/', 'services/', 'tests/', 'docs/', 'lab/', 'scripts/', 'models/', 'dashboard/', 'data/')


def headings(text):
    return [re.sub(r'^\d+\.\s*', '', line.lstrip('#').strip()) for line in text.splitlines()
            if line.startswith('## ')]


def missing_sections(text):
    present = set(headings(text))
    return [name for name in REQUIRED_SECTIONS if name not in present]


def prd_security_items(prd_text):
    """The bullets of PRD §6.3, in order."""
    match = re.search(r'^### 6\.3 [^\n]*\n(.*?)^### ', prd_text, re.S | re.M)
    if match is None:
        raise ValueError('PRD §6.3 not found')
    return [line[2:].strip() for line in match.group(1).splitlines() if line.startswith('- ')]


def guide_security_rows(text):
    """First cells of the table in the guide's §6.3 section, header excluded."""
    match = re.search(r'^## \d+\. Security controls \(PRD §6\.3\)\n(.*?)(?=^## )', text, re.S | re.M)
    if match is None:
        return []
    rows = [line for line in match.group(1).splitlines() if line.startswith('|')]
    cells = [row.strip('|').split('|')[0].strip() for row in rows]
    return [cell for cell in cells[2:]]  # header and separator


def security_problems(guide_text, prd_text):
    items, rows = prd_security_items(prd_text), guide_security_rows(guide_text)
    problems = []
    if len(rows) != len(items):
        problems.append(f'{len(rows)} security rows for {len(items)} PRD §6.3 items')
    for index, (row, item) in enumerate(zip(rows, items), 1):
        if len(row.split()) < MIN_ROW_WORDS:
            problems.append(f'row {index} {row!r} quotes fewer than {MIN_ROW_WORDS} words')
        elif not item.lower().startswith(row.lower()):
            problems.append(f'row {index} {row!r} does not start PRD §6.3 item {item!r}')
    return problems


def restore_problems(guide_text, lab_source):
    """The guide's restore steps against the lab test's: the same SQL to empty the
    database (EMPTY_DATABASE_SQL in the lab module), and in the guide's restore block the
    order stop -> empty -> pg_restore -> start."""
    match = re.search(r'^EMPTY_DATABASE_SQL = \((.*?)\)$', lab_source, re.M)
    if match is None:
        return ['the lab test has no EMPTY_DATABASE_SQL']
    statements = re.findall(r"'([^']+)'", match.group(1))
    block = re.search(r'^\*\*Restore \(Compose; tested\)\.\*\*.*?```text\n(.*?)```', guide_text, re.S | re.M)
    if block is None:
        return ['the guide has no Compose restore block']
    lines = block.group(1).splitlines()
    problems = [f'the guide does not run {sql!r}' for sql in statements
                if not any(f'-c "{sql}"' in line for line in lines)]

    def first(needle):
        return next((i for i, line in enumerate(lines) if needle in line), None)
    order = [first(' stop live api'), first(' cp '), first(' pg_restore --list '), first('DROP DATABASE'),
             first(' pg_restore -U '), first(' --profile lab up -d --wait')]
    if None in order or order != sorted(order) or len(set(order)) != len(order):
        problems.append(f'restore steps missing or out of order: {order}')
    elif first('CREATE DATABASE') is None or \
            lines[order[3]].find('DROP DATABASE') > lines[order[3]].find('CREATE DATABASE'):
        problems.append('CREATE DATABASE must follow DROP DATABASE on the same line')
    return problems


def cited_paths(text):
    paths = set()
    for token in re.findall(r'`([^`\s]+)`', text):
        token = token.rstrip('.,:;')
        if token.startswith(PATH_PREFIXES) and '<' not in token:
            paths.add(token)
    return paths


def missing_paths(text, root=ROOT):
    return sorted(path for path in cited_paths(text) if not (root / path).exists())


def cli_flags(sources):
    flags = set()
    for source in sources:
        flags.update(re.findall(r"add_argument\(\s*'(--[a-z][a-z0-9-]*)'", source))
    return flags


def unknown_flags(text, known):
    used = set(re.findall(r'(?<![\w-])(--[a-z][a-z0-9-]*)', text))
    return sorted(used - known - EXTERNAL_FLAGS)


def missing_tests(text, test_sources):
    names = set(re.findall(r'`(test_[a-z0-9_]+)`', text))
    defined = set()
    for source in test_sources:
        defined.update(re.findall(r'^\s*def (test_[a-z0-9_]+)\(', source, re.M))
    return sorted(names - defined)


def unknown_settings(text, sources):
    names = set(re.findall(r'\bAVA_[A-Z0-9_]+\b', text))
    return sorted(name for name in names if not any(name in source for source in sources))


def command_flags(text, tool):
    """The flags of the first line in `text` that runs `tool`, in order; a `--list`
    check line is skipped (the restore check reads the listing step separately)."""
    for line in text.splitlines():
        if f' {tool} ' in f' {line} ' and f'{tool} --list' not in line:
            return re.findall(r'--[a-z][a-z0-9-]*(?:=[^\s\'",)]+)?', line.split(tool, 1)[1])
    return None


def lab_command_flags(source, tool):
    """The `--flags` of the first compose call or argument list in the lab test that runs
    `tool` (a `--list` check skipped), in order, up to the closing parenthesis or bracket."""
    for match in re.finditer(r"'%s'(.*?)[)\]]" % re.escape(tool), source, re.S):
        flags = re.findall(r"'(--[a-z][a-z0-9-]*(?:=[^']*)?)'", match.group(1))
        if flags[:1] != ['--list']:
            return flags
    return None


class RealGuideTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.guide = GUIDE.read_text(encoding='utf-8')
        cls.prd = PRD.read_text(encoding='utf-8')
        cls.lab = LAB_TEST.read_text(encoding='utf-8')

    def test_every_fr_o8_item_has_a_section(self):
        self.assertEqual(missing_sections(self.guide), [])

    def test_every_prd_security_item_has_a_row_in_order(self):
        self.assertGreaterEqual(len(prd_security_items(self.prd)), 7)
        self.assertEqual(security_problems(self.guide, self.prd), [])

    def test_every_cited_path_exists(self):
        self.assertGreater(len(cited_paths(self.guide)), 20)
        self.assertEqual(missing_paths(self.guide), [])

    def test_every_flag_is_an_ava_or_named_tool_option(self):
        known = cli_flags(source.read_text(encoding='utf-8') for source in CLI_SOURCES)
        self.assertIn('--receiver-host', known)
        self.assertEqual(unknown_flags(self.guide, known), [])

    def test_every_named_test_exists(self):
        sources = [path.read_text(encoding='utf-8') for path in (ROOT / 'tests').rglob('test_*.py')]
        self.assertIn('test_a_database_backup_restores_into_a_new_stack_and_scoring_resumes', self.guide)
        self.assertEqual(missing_tests(self.guide, sources), [])

    def test_every_setting_name_exists_in_the_deployment_files(self):
        sources = [path.read_text(encoding='utf-8') for folder in ('deploy', 'services')
                   for path in (ROOT / folder).rglob('*') if path.suffix in ('.yaml', '.example', '.py', '.json')]
        self.assertEqual(unknown_settings(self.guide, sources), [])

    def test_backup_and_restore_commands_are_the_ones_the_lab_test_runs(self):
        for tool in ('pg_dump', 'pg_restore'):
            guide, lab = command_flags(self.guide, tool), lab_command_flags(self.lab, tool)
            self.assertIsNotNone(guide, tool)
            self.assertIsNotNone(lab, tool)
            self.assertEqual(guide, lab, tool)
        self.assertEqual(command_flags(self.guide, 'pg_restore'),
                         ['--no-owner', '--no-privileges', '--single-transaction', '--exit-on-error'])

    def test_restore_steps_are_the_lab_tests_in_order(self):
        self.assertEqual(restore_problems(self.guide, self.lab), [])


class SyntheticRuleTests(unittest.TestCase):
    PRD = '### 6.3 Security\n\n- No egress at all (tested).\n- TLS between A and B.\n\n### 6.4 Next\n'

    def test_a_missing_section_is_reported(self):
        text = '\n'.join(f'## {i}. {name}' for i, name in enumerate(REQUIRED_SECTIONS) if name != 'Backup and restore')
        self.assertEqual(missing_sections(text), ['Backup and restore'])

    def test_security_rows_must_match_the_prd_items_in_order_and_number(self):
        def guide(*cells):
            body = '\n'.join(f'| {cell} | met | x |' for cell in cells)
            return f'## 7. Security controls (PRD §6.3)\n\n| Control | State | How |\n|---|---|---|\n{body}\n\n## 8. Next\n'
        self.assertEqual(security_problems(guide('No egress at', 'TLS between A and B'), self.PRD), [])
        self.assertEqual(len(security_problems(guide('No egress at'), self.PRD)), 1, 'a missing row')
        self.assertEqual(len(security_problems(guide('TLS between A and B', 'No egress at'), self.PRD)), 2,
                         'rows out of order')
        self.assertEqual(len(security_problems(guide('No egress at', 'TLS to C'), self.PRD)), 1, 'a reworded control')
        self.assertEqual(len(security_problems(guide('No', 'TLS between A and B'), self.PRD)), 1,
                         'a one-word prefix is not a quote')

    def test_restore_steps_must_match_and_keep_their_order(self):
        lab = "EMPTY_DATABASE_SQL = ('DROP DATABASE ava WITH (FORCE)', 'CREATE DATABASE ava OWNER ava')\n"

        def guide(*steps):
            return '**Restore (Compose; tested).** text\n\n```text\n' + '\n'.join(steps) + '\n```\n'
        stop, up = 'docker compose ... stop live api', 'docker compose ... --profile lab up -d --wait'
        copy, listing = 'x cp a.dump postgres:/tmp/ava.dump', 'x exec -T postgres pg_restore --list /tmp/ava.dump'
        empty = 'x exec -T postgres psql -c "DROP DATABASE ava WITH (FORCE)" -c "CREATE DATABASE ava OWNER ava"'
        restore = 'x exec -T postgres pg_restore -U ava /tmp/ava.dump'
        self.assertEqual(restore_problems(guide(stop, copy, listing, empty, restore, up), lab), [])
        self.assertEqual(len(restore_problems(guide(stop, copy, listing, restore, up), lab)), 3,
                         'no empty step (two SQL + order)')
        self.assertEqual(len(restore_problems(guide(stop, copy, listing, restore, empty, up), lab)), 1,
                         'empty after restore')
        self.assertEqual(len(restore_problems(guide(stop, copy, empty, listing, restore, up), lab)), 1,
                         'the database dropped before the dump is checked (re-review N1)')
        self.assertEqual(len(restore_problems(guide(stop, copy, empty, restore, up), lab)), 1, 'no listing check')
        self.assertEqual(len(restore_problems(guide(copy, listing, empty, restore, up), lab)), 1,
                         'services not stopped')
        swapped = 'x exec -T postgres psql -c "CREATE DATABASE ava OWNER ava" -c "DROP DATABASE ava WITH (FORCE)"'
        self.assertEqual(restore_problems(guide(stop, copy, listing, swapped, restore, up), lab),
                         ['CREATE DATABASE must follow DROP DATABASE on the same line'])
        other = empty.replace('WITH (FORCE)', '')
        self.assertEqual(len(restore_problems(guide(stop, copy, listing, other, restore, up), lab)), 1,
                         'different SQL from the lab')
        self.assertEqual(restore_problems(guide(stop, copy, listing, empty, restore, up), 'nothing'),
                         ['the lab test has no EMPTY_DATABASE_SQL'])

    def test_a_missing_path_is_reported(self):
        self.assertEqual(missing_paths('see `deploy/compose/README.md` and `deploy/nothing-here.md`.'),
                         ['deploy/nothing-here.md'])
        self.assertEqual(missing_paths('a `<path to>/deploy/k8s/base` placeholder'), [])

    def test_an_unknown_flag_is_reported(self):
        known = cli_flags(["parser.add_argument('--ari-url', required=True)"])
        self.assertEqual(unknown_flags('`--ari-url`, `--ari-urls`, `--format=custom`', known), ['--ari-urls'])

    def test_an_unknown_test_name_is_reported(self):
        self.assertEqual(missing_tests('`test_one` and `test_two`', ['    def test_one(self):']), ['test_two'])

    def test_an_unknown_setting_is_reported(self):
        self.assertEqual(unknown_settings('`AVA_ONNX_DIR` and `AVA_GONE`', ['AVA_ONNX_DIR=x']), ['AVA_GONE'])

    def test_differing_backup_flags_are_seen(self):
        guide = 'x exec -T postgres pg_restore -U ava --no-owner --single-transaction /tmp/a'
        lab = "restore = ['exec', 'pg_restore', '-U', 'ava', '--no-owner', '--no-privileges', '/tmp/a']"
        self.assertNotEqual(command_flags(guide, 'pg_restore'), lab_command_flags(lab, 'pg_restore'))
        self.assertEqual(command_flags(guide, 'pg_restore'), ['--no-owner', '--single-transaction'])
        self.assertEqual(lab_command_flags(lab, 'pg_restore'), ['--no-owner', '--no-privileges'])


if __name__ == '__main__':
    unittest.main()
