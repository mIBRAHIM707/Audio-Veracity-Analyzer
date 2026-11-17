"""Docker/WSL preflight for the telephony lab (backlog 6.1; lab/preflight.py).

Claude-authored test design, 17 November 2026. Docker, PATH, disk, ffmpeg and process
runs are injected fakes, so these run on CI without Docker; the real run on the
development host is recorded in lab/asterisk/README.md. Covers the normal path, every
failure that stops the lab (no CLI, engine down or hanging, Windows containers, image
missing), warnings (non-WSL backend, small engine, leftovers, low disk on either
measured drive, lab test environment not enabling the tests, ffmpeg absent, broken or
not the pin), the lab test environment (.local/lab-test.env under the process
environment, AVA_LAB_IMAGE, AVA_FFMPEG), malformed docker output, exit codes, the
resolved docker executable and timeout, and that nothing beyond the selected fields is
printed.
"""
from collections import namedtuple
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from lab import preflight

Usage = namedtuple('Usage', 'total used free')
GIB = 1024 ** 3
VERSION = {'Client': {'Version': '29.9.3'}, 'Server': {'Version': '29.9.3', 'Os': 'linux'}}
SECRETS = {'HttpsProxy': 'http://user:secret@proxy.example:3128',
           'RegistryConfig': {'IndexConfigs': {'secret-registry.example': {}}}, 'Labels': ['secret-label'],
           'Name': 'secret-host'}
INFO = {'OperatingSystem': 'Docker Desktop', 'KernelVersion': '6.18.33.2-microsoft-standard-WSL2', 'NCPU': 8,
        'MemTotal': 8170106880, **SECRETS}
PINNED_BANNER = f'ffmpeg version {preflight.FFMPEG_VERSION}-full_build-www.gyan.dev Copyright (c)\nbuilt with gcc\n'


class FakeDocker:
    """Answers the four read-only docker commands; records every call."""

    def __init__(self, version=VERSION, info=INFO, image='sha256:' + 'ab' * 32, leftovers=(), fail=None):
        self.version, self.info, self.image, self.leftovers, self.fail = version, info, image, leftovers, fail
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        if self.fail and args[0] in self.fail:
            return self.fail[args[0]]
        if args[0] == 'version':
            return 0, json.dumps(self.version) if isinstance(self.version, dict) else self.version, ''
        if args[0] == 'info':
            return 0, json.dumps(self.info) if isinstance(self.info, dict) else self.info, ''
        if args[:2] == ['image', 'inspect']:
            return (0, self.image + '\n', '') if self.image and args[2] == self.expected_image else \
                (1, '', 'Error: No such image')
        if args[0] == 'ps':
            return 0, ''.join(f'{name}\n' for name in self.leftovers), ''
        raise AssertionError(f'unexpected docker command {args}')

    expected_image = preflight.DEFAULT_IMAGE


def codec_listing(names):
    return ' ------\n' + ''.join(f' A....D {name}  some codec\n' for name in names)


def fake_run(banner=PINNED_BANNER, code=0, encoders=preflight.FFMPEG_CODECS, decoders=preflight.FFMPEG_CODECS):
    """Answers ffmpeg -version / -encoders / -decoders (anything else gets `banner`)."""
    calls = []

    def run(command):
        calls.append(list(command))
        if command[-1] == '-encoders':
            return code, codec_listing(encoders), ''
        if command[-1] == '-decoders':
            return code, codec_listing(decoders), ''
        return code, banner, ''
    run.calls = calls
    return run


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix='ava-preflight-'))
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        (self.repo / '.local').mkdir()
        self.env_file('AVA_TEST_DOCKER_LAB=1\n')

    def env_file(self, text):
        (self.repo / '.local' / 'lab-test.env').write_text(text)

    def run_checks(self, docker='fake', **overrides):
        options = dict(docker=FakeDocker() if docker == 'fake' else docker, which=lambda name: '/usr/bin/docker', env={},
                       disk_usage=lambda path: Usage(500 * GIB, 300 * GIB, 200 * GIB), platform='win32',
                       repo=self.repo, ffmpeg=lambda env: 'C:/ffmpeg.exe', run=fake_run(),
                       temp_dir=str(self.repo / 'tmp'))
        options.update(overrides)
        return {c['name']: c for c in preflight.run_checks(**options)}

    def statuses(self, checks):
        return {name: c['status'] for name, c in checks.items()}

    # --- docker ---------------------------------------------------------------------------
    def test_ready_host_passes_every_check_with_read_only_commands(self):
        docker = FakeDocker()
        checks = self.run_checks(docker)
        self.assertEqual(list(checks), ['docker_cli', 'docker_engine', 'docker_backend', 'lab_image',
                                        'leftover_containers', 'disk_free', 'lab_env', 'ffmpeg'])
        self.assertEqual(set(self.statuses(checks).values()), {'pass'})
        self.assertIn('7.6 GiB', checks['docker_backend']['detail'])
        verbs = {call[0] if call[0] != 'image' else ' '.join(call[:2]) for call in docker.calls}
        self.assertEqual(verbs, {'version', 'info', 'image inspect', 'ps'}, 'only read-only docker commands')
        self.assertIn(f'label={preflight.LAB_LABEL}', docker.calls[-1])

    def test_missing_cli_fails_and_skips_engine_checks_without_calling_docker(self):
        docker = FakeDocker()
        checks = self.run_checks(docker, which=lambda name: None)
        self.assertEqual(checks['docker_cli']['status'], 'fail')
        for name in ('docker_engine', 'docker_backend', 'lab_image', 'leftover_containers'):
            self.assertEqual(checks[name]['status'], 'skip', name)
        self.assertEqual(len(checks), 8, 'review L4: the same eight checks on every run')
        self.assertEqual(docker.calls, [])
        self.assertEqual(checks['disk_free']['status'], 'pass', 'host checks still run')

    def test_engine_down_hanging_or_garbled_fails_with_a_fix(self):
        cases = {'not running': {'version': (1, json.dumps({'Client': {'Version': '29'}}), 'cannot connect')},
                 'timeout': {'version': (None, '', 'TimeoutExpired')},
                 'garbled': {'version': (0, 'not json', '')},
                 'truncated': {'version': (0, '{"Server": {"Os": "lin', '')},
                 'json list': {'version': (0, '[1, 2]', '')},
                 'no server part': {'version': (0, json.dumps({'Client': {}}), '')},
                 'null server': {'version': (0, json.dumps({'Server': None}), '')}}
        for label, fail in cases.items():
            with self.subTest(label):
                checks = self.run_checks(FakeDocker(fail=fail))
                self.assertEqual(checks['docker_engine']['status'], 'fail')
                self.assertIn('Docker Desktop', checks['docker_engine']['fix'])
                self.assertEqual(checks['lab_image']['status'], 'skip')
                self.assertEqual(len(checks), 8)

    def test_windows_containers_mode_fails(self):
        version = {'Server': {'Version': '29.9.3', 'Os': 'windows'}}
        checks = self.run_checks(FakeDocker(version=version))
        self.assertEqual(checks['docker_engine']['status'], 'fail')
        self.assertIn("'windows'", checks['docker_engine']['detail'])
        self.assertIn('Linux containers', checks['docker_engine']['fix'])

    def test_backend_warnings_for_non_wsl_engine_and_small_resources(self):
        hyperv = dict(INFO, KernelVersion='5.15.0-linuxkit')
        self.assertEqual(self.run_checks(FakeDocker(info=hyperv))['docker_backend']['status'], 'warn')
        self.assertEqual(self.run_checks(FakeDocker(info=hyperv), platform='linux')['docker_backend']['status'],
                         'pass', 'WSL 2 is only expected on Windows')
        for field, value in (('NCPU', 1), ('MemTotal', 4 * GIB - 1), ('NCPU', True), ('MemTotal', None)):
            with self.subTest(field=field, value=value):
                check = self.run_checks(FakeDocker(info=dict(INFO, **{field: value})))['docker_backend']
                self.assertEqual(check['status'], 'warn')
        boundary = dict(INFO, NCPU=2, MemTotal=4 * GIB)
        self.assertEqual(self.run_checks(FakeDocker(info=boundary))['docker_backend']['status'], 'pass')
        for broken in ('garbage', '"a string"'):
            self.assertEqual(self.run_checks(FakeDocker(info=broken))['docker_backend']['status'], 'warn')
        failed = FakeDocker(fail={'info': (1, '', 'boom')})
        self.assertEqual(self.run_checks(failed)['docker_backend']['status'], 'warn')

    def test_missing_image_fails_with_the_build_command(self):
        checks = self.run_checks(FakeDocker(image=None))
        self.assertEqual(checks['lab_image']['status'], 'fail')
        self.assertEqual(checks['lab_image']['fix'], 'docker build -t ava-lab-asterisk:dev lab/asterisk')
        odd = self.run_checks(FakeDocker(image='not-a-digest'))
        self.assertEqual(odd['lab_image']['status'], 'fail')

    def test_image_follows_the_option_then_ava_lab_image_then_the_default(self):
        # Review M3: the tests use AVA_LAB_IMAGE (tests/lab/test_asterisk_lab.py).
        for label, options, expected in (('env file', {}, 'ava-lab-asterisk:file'),
                                         ('process env wins', {'env': {'AVA_LAB_IMAGE': 'ava-lab-asterisk:shell'}},
                                          'ava-lab-asterisk:shell'),
                                         ('option wins', {'image': 'ava-lab-asterisk:opt'}, 'ava-lab-asterisk:opt')):
            with self.subTest(label):
                self.env_file('AVA_TEST_DOCKER_LAB=1\nAVA_LAB_IMAGE=ava-lab-asterisk:file\n')
                docker = FakeDocker()
                docker.expected_image = expected
                checks = self.run_checks(docker, **options)
                self.assertEqual(checks['lab_image']['status'], 'pass')
                self.assertIn(['image', 'inspect', expected, '--format', '{{.Id}}'], docker.calls)
                self.assertIn(expected, checks['lab_image']['fix'] if 'fix' in checks['lab_image'] else
                              checks['lab_image']['detail'])

    def test_leftover_lab_containers_warn_and_are_never_removed(self):
        docker = FakeDocker(leftovers=['ava-lab-test-1', 'ava-lab-test-2'])
        check = self.run_checks(docker)['leftover_containers']
        self.assertEqual(check['status'], 'warn')
        self.assertIn('ava-lab-test-1, ava-lab-test-2', check['detail'])
        self.assertIn(f'label={preflight.LAB_LABEL}', check['fix'], 'review L5: the fix covers every leftover')
        self.assertFalse(any(call[0] in ('rm', 'stop', 'kill') for call in docker.calls))
        many = self.run_checks(FakeDocker(leftovers=[f'c{i}' for i in range(30)]))['leftover_containers']
        self.assertIn('30 lab test container(s)', many['detail'])
        self.assertIn('(first 10 of 30)', many['detail'])
        broken = self.run_checks(FakeDocker(fail={'ps': (1, '', 'boom')}))['leftover_containers']
        self.assertEqual(broken['status'], 'warn')

    def test_default_docker_runs_the_resolved_executable(self):
        # Review L2: like harness.py, the path `which` found is what runs.
        run = fake_run(banner=json.dumps(VERSION))
        checks = self.run_checks(docker=None, which=lambda name: '/opt/docker/bin/docker', run=run)
        self.assertEqual(run.calls[0], ['/opt/docker/bin/docker', 'version', '--format', '{{json .}}'])
        self.assertEqual(checks['docker_engine']['status'], 'pass')

    # --- host ----------------------------------------------------------------------------
    def test_disk_threshold_is_inclusive_per_drive_in_gib_and_errors_warn(self):
        temp = str(self.repo / 'tmp')

        def usage(repo_free, temp_free):
            return lambda path: Usage(0, 0, temp_free if str(path) == temp else repo_free)
        exactly = self.run_checks(disk_usage=usage(10 * GIB, 10 * GIB))['disk_free']
        self.assertEqual(exactly['status'], 'pass')
        self.assertIn('repository 10.0 GiB free; temp (lab I/O) 10.0 GiB free', exactly['detail'])
        for repo_free, temp_free, low in ((10 * GIB - 1, 50 * GIB, 'repository'), (50 * GIB, 10 * GIB - 1, 'temp')):
            with self.subTest(low=low):
                check = self.run_checks(disk_usage=usage(repo_free, temp_free))['disk_free']
                self.assertEqual(check['status'], 'warn')
                self.assertIn(f'GiB: {low}', check['detail'])
        custom = self.run_checks(disk_usage=usage(3 * GIB, 3 * GIB), min_free_gib=2)
        self.assertEqual(custom['disk_free']['status'], 'pass')

        def broken(path):
            raise OSError('drive gone')
        self.assertEqual(self.run_checks(disk_usage=broken)['disk_free']['status'], 'warn')

    def test_lab_env_must_enable_the_lab_tests(self):
        # Review M1: the tests skip unless AVA_TEST_DOCKER_LAB is exactly '1'.
        for label, text, env, status in (('missing', None, {}, 'warn'), ('empty', '', {}, 'warn'),
                                         ('zero', 'AVA_TEST_DOCKER_LAB=0\n', {}, 'warn'),
                                         ('quoted', '# lab\nexport AVA_TEST_DOCKER_LAB="1"\n', {}, 'pass'),
                                         ('inline comment', 'AVA_TEST_DOCKER_LAB=1  # enable lab\n', {}, 'pass'),
                                         ('shell overrides file', 'AVA_TEST_DOCKER_LAB=1\n',
                                          {'AVA_TEST_DOCKER_LAB': '0'}, 'warn'),
                                         ('enabled', 'AVA_TEST_DOCKER_LAB=1\n', {}, 'pass')):
            with self.subTest(label):
                path = self.repo / '.local' / 'lab-test.env'
                if text is None:
                    path.unlink(missing_ok=True)
                else:
                    self.env_file(text)
                check = self.run_checks(env=env)['lab_env']
                self.assertEqual(check['status'], status)
                if status == 'warn':
                    self.assertIn('AVA_TEST_DOCKER_LAB=1', check['fix'])

    def test_env_file_values_are_never_printed(self):
        self.env_file('AVA_TEST_DOCKER_LAB=1\nAVA_ARI_PASSWORD=TopSecretValue123456\n')
        out = io.StringIO()
        preflight.main(['--json'], out=out, docker=FakeDocker(), which=lambda name: 'docker', env={},
                       disk_usage=lambda path: Usage(0, 0, 200 * GIB), platform='win32', repo=self.repo,
                       ffmpeg=lambda env: None, run=fake_run(), temp_dir=str(self.repo))
        self.assertNotIn('TopSecretValue', out.getvalue())

    # --- ffmpeg --------------------------------------------------------------------------
    def test_ffmpeg_version_is_checked_like_the_codec_wrapper(self):
        # Review M2: a path alone is not the pin; 9.0.20 is not 9.0.2.
        for label, run, status, words in (
                ('pinned', fake_run(), 'pass', preflight.FFMPEG_VERSION),
                ('other version', fake_run('ffmpeg version 7.1-full_build\n'), 'warn', 'not the pinned'),
                ('prefix trap', fake_run(f'ffmpeg version {preflight.FFMPEG_VERSION}0\n'), 'warn', 'not the pinned'),
                ('does not run', fake_run('', code=None), 'warn', 'does not run'),
                ('not ffmpeg', fake_run('hello world\n'), 'warn', 'does not run')):
            with self.subTest(label):
                check = self.run_checks(run=run)['ffmpeg']
                self.assertEqual(check['status'], status)
                self.assertIn(words, check['detail'])
                self.assertEqual(run.calls[0], ['C:/ffmpeg.exe', '-hide_banner', '-version'])
        # Review N3: the wrapper also needs every codec, both ways.
        for label, run, missing in (('no libgsm encoder', fake_run(encoders=('libopus', 'libopencore_amrnb')),
                                     'encoder libgsm'),
                                    ('no libopus decoder', fake_run(decoders=('libgsm', 'libopencore_amrnb')),
                                     'decoder libopus')):
            with self.subTest(label):
                check = self.run_checks(run=run)['ffmpeg']
                self.assertEqual(check['status'], 'warn')
                self.assertIn(missing, check['detail'])
        absent = self.run_checks(ffmpeg=lambda env: None)['ffmpeg']
        self.assertEqual(absent['status'], 'warn')
        self.assertIn('skip', absent['detail'])

    def test_ffmpeg_is_looked_up_in_the_lab_test_environment(self):
        seen = []
        self.env_file('AVA_TEST_DOCKER_LAB=1\nAVA_FFMPEG=D:/tools/ffmpeg.exe\n')
        self.run_checks(ffmpeg=lambda env: seen.append(env.get('AVA_FFMPEG')))
        self.assertEqual(seen, ['D:/tools/ffmpeg.exe'])

    def test_find_ffmpeg_matches_the_lab_tests_lookup(self):
        # AVA_FFMPEG is used as given (the tests do not check it exists), else the winget pin.
        self.assertEqual(preflight.find_ffmpeg({'AVA_FFMPEG': '/no/such/ffmpeg'}), '/no/such/ffmpeg')
        self.assertIsNone(preflight.find_ffmpeg({}))
        root = self.repo / 'appdata'
        packages = root / 'Microsoft' / 'WinGet' / 'Packages' / 'Gyan.FFmpeg_Microsoft.Winget.Source_x'
        wrong = packages / f'ffmpeg-{preflight.FFMPEG_VERSION}0-full_build' / 'bin'
        wrong.mkdir(parents=True)
        (wrong / 'ffmpeg.exe').write_bytes(b'')
        self.assertIsNone(preflight.find_ffmpeg({'LOCALAPPDATA': str(root)}), '9.0.20 is not the pin')
        right = packages / f'ffmpeg-{preflight.FFMPEG_VERSION}-full_build' / 'bin'
        right.mkdir(parents=True)
        (right / 'ffmpeg.exe').write_bytes(b'')
        self.assertEqual(Path(preflight.find_ffmpeg({'LOCALAPPDATA': str(root)})), right / 'ffmpeg.exe')

    def test_ffmpeg_constants_match_the_codec_wrapper(self):
        from data.prepare.ffmpeg_codecs import CODECS, PINNED_VERSION
        self.assertEqual(preflight.FFMPEG_VERSION, PINNED_VERSION)
        for key in ('encoder', 'decoder'):
            self.assertEqual(set(preflight.FFMPEG_CODECS), {spec[key] for spec in CODECS.values()}, key)

    # --- command line and process runs ----------------------------------------------------
    def test_cli_exit_codes_text_and_json_without_secrets(self):
        options = dict(docker=FakeDocker(info=dict(INFO, NCPU=1)), which=lambda name: 'docker', env={},
                       disk_usage=lambda path: Usage(0, 0, 200 * GIB), platform='win32', repo=self.repo,
                       ffmpeg=lambda env: None, run=fake_run(), temp_dir=str(self.repo))
        out = io.StringIO()
        self.assertEqual(preflight.main([], out=out, **options), 0, 'warnings alone do not fail')
        text = out.getvalue()
        self.assertIn('WARN  ffmpeg', text)
        self.assertIn('WARN  docker_backend', text)
        self.assertIn('fix: install ffmpeg', text)
        self.assertIn('preflight passed', text)
        out = io.StringIO()
        self.assertEqual(preflight.main(['--json'], out=out, **dict(options, docker=FakeDocker(image=None))), 1)
        result = json.loads(out.getvalue())
        self.assertFalse(result['ok'])
        self.assertEqual(result['counts']['fail'], 1)
        for secret in ('secret', 'proxy.example', 'user:'):
            self.assertNotIn(secret, text + out.getvalue())
        for bad in (['--min-free-gib', '-1'], ['--min-free-gib', 'nan'], ['--min-free-gib', 'inf'], ['--bogus']):
            with self.subTest(argv=bad), mock.patch('sys.stderr', io.StringIO()), \
                    self.assertRaises(SystemExit) as raised:
                preflight.main(bad, out=io.StringIO(), **options)
            self.assertEqual(raised.exception.code, 2)

    def test_run_command_bounds_time_decodes_utf8_and_reports_no_answer(self):
        with mock.patch.object(subprocess, 'run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, 'out', 'err')
            self.assertEqual(preflight.run_command(['docker', 'version']), (0, 'out', 'err'))
            kwargs = run.call_args.kwargs
            self.assertEqual((kwargs['timeout'], kwargs['encoding'], kwargs['errors']),
                             (preflight.TIMEOUT_S, 'utf-8', 'replace'))
        with mock.patch.object(subprocess, 'run', side_effect=FileNotFoundError('docker')):
            self.assertEqual(preflight.run_command(['docker']), (None, '', 'FileNotFoundError'))
        with mock.patch.object(subprocess, 'run', side_effect=subprocess.TimeoutExpired('docker', 20)):
            self.assertEqual(preflight.run_command(['docker']), (None, '', 'TimeoutExpired'))

    def test_run_command_survives_non_ascii_output(self):
        # Review L1: a real child process printing UTF-8 that a Windows code page would reject.
        import sys
        code, out, _ = preflight.run_command(
            [sys.executable, '-c', 'import sys; sys.stdout.buffer.write("caf\\u00e9 \\u2713 \\x81".encode("utf-8"))'])
        self.assertEqual(code, 0)
        self.assertIn('caf\u00e9', out)


if __name__ == '__main__':
    unittest.main()
