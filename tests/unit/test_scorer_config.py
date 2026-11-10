"""Scoring configuration validation and call-scoped reload (backlog 4.1; PRD FR-I4,
FR-A9 subset; architecture §2.4 and §7.3).

Claude-authored test cases, 10 November 2026. Pure configuration logic; no model,
audio or network.
"""
import copy
import dataclasses
import json
import os
import tempfile
import threading
import unittest

from unittest import mock

from services.scorer import config as config_module
from services.scorer.config import (MAX_CONFIG_BYTES, ConfigError, ConfigStore, ScoringConfig,
                                    Thresholds, fingerprint, load_json_config, parse_config)
from tests.fixture_cleanup import cleanup_with_retry

MODEL_WINDOW = 4.0
# Architecture §7.3 document, including sections owned by other components.
AVA_DOCUMENT = {
    'audio': {'window_s': 4.0, 'hop_s': 2.0, 'vad_threshold': 0.5, 'min_speech_fraction': 0.625,
              'max_concealed_fraction': 0.10},
    'scoring': {'ema_alpha': 0.4, 'min_windows': 2,
                'thresholds': {'suspicious_enter': 0.60, 'alert_enter': 0.75, 'alert_exit': 0.55,
                               'suspicious_exit': 0.40},
                'score_directions': ['caller'], 'degraded_max_stride_factor': 4,
                'degraded_recovery_s': 30},
    'capacity': {'max_calls': 8, 'batch_share': 0.25},
    'tap': {'mode': 'ari_snoop', 'silence_timeout_s': 30},
    'retention': {'snippets_enabled': False},
    'inference': {'model_path': '/models/ava-v1.onnx'},
}


def document(**changes):
    """Copy of AVA_DOCUMENT with dotted-path changes, e.g. {'scoring.ema_alpha': 1}."""
    result = copy.deepcopy(AVA_DOCUMENT)
    for path, value in changes.items():
        *parents, leaf = path.split('.')
        target = result
        for parent in parents:
            target = target[parent]
        target[leaf] = value
    return result


def parse(doc, window=MODEL_WINDOW):
    return parse_config(doc, model_window_s=window)


def changed(**kw):
    """Keyword arguments use '__' for '.' so dotted paths can be written."""
    return document(**{k.replace('__', '.'): v for k, v in kw.items()})


class ParseConfigTests(unittest.TestCase):
    def test_architecture_document_matches_defaults(self):
        config = parse(AVA_DOCUMENT)
        self.assertEqual(config, ScoringConfig())
        self.assertEqual(config.thresholds, Thresholds(0.60, 0.75, 0.55, 0.40))
        self.assertEqual((config.window_s, config.hop_s, config.ema_alpha, config.min_windows),
                         (4.0, 2.0, 0.4, 2))
        self.assertEqual(config.score_directions, ('caller',))

    def test_missing_sections_take_defaults(self):
        self.assertEqual(parse({}), ScoringConfig())
        self.assertEqual(parse({'scoring': {'thresholds': {'alert_enter': 0.8}}}).thresholds,
                         Thresholds(alert_enter=0.8))

    def test_threshold_boundaries(self):
        accepted = [
            ('enter_at_one', dict(scoring__thresholds__suspicious_enter=1.0,
                                  scoring__thresholds__alert_enter=1)),
            ('tiny_exit', dict(scoring__thresholds__suspicious_exit=1e-9)),
        ]
        for name, kw in accepted:
            with self.subTest(name):
                parse(changed(**kw))
        refused = [
            ('susp_exit_equals_enter', dict(scoring__thresholds__suspicious_exit=0.60)),
            ('susp_exit_above_enter', dict(scoring__thresholds__suspicious_exit=0.7)),
            ('susp_exit_zero', dict(scoring__thresholds__suspicious_exit=0)),
            ('susp_exit_negative', dict(scoring__thresholds__suspicious_exit=-0.1)),
            ('susp_enter_over_one', dict(scoring__thresholds__suspicious_enter=1.0000001)),
            ('alert_exit_equals_enter', dict(scoring__thresholds__alert_exit=0.75)),
            ('alert_exit_zero', dict(scoring__thresholds__alert_exit=0.0)),
            ('alert_enter_over_one', dict(scoring__thresholds__alert_enter=1.5)),
        ]
        for name, kw in refused:
            with self.subTest(name):
                with self.assertRaisesRegex(ConfigError, 'thresholds requires'):
                    parse(changed(**kw))

    def test_window_hop_and_model_card(self):
        parse(changed(audio__hop_s=4.0))  # hop == window accepted
        parse(changed(audio__hop_s=0.5))
        parse(changed(audio__window_s=4), window=4)  # int and float agree
        for name, kw, pattern in (
                ('hop_zero', dict(audio__hop_s=0), 'hop_s'),
                ('hop_negative', dict(audio__hop_s=-2.0), 'hop_s'),
                ('hop_over_window', dict(audio__hop_s=4.5), 'hop_s'),
                ('window_not_model', dict(audio__window_s=3.0, audio__hop_s=1.5), 'model card'),
                ('window_near_model', dict(audio__window_s=4.000001), 'model card')):
            with self.subTest(name):
                with self.assertRaisesRegex(ConfigError, pattern):
                    parse(changed(**kw))
        with self.assertRaisesRegex(ConfigError, 'model card'):
            parse(AVA_DOCUMENT, window=2.0)

    def test_alpha_and_counts(self):
        self.assertEqual(parse(changed(scoring__ema_alpha=1)).ema_alpha, 1.0)
        self.assertEqual(parse(changed(scoring__min_windows=1)).min_windows, 1)
        for name, kw in (('alpha_zero', dict(scoring__ema_alpha=0)),
                         ('alpha_over', dict(scoring__ema_alpha=1.01)),
                         ('alpha_negative', dict(scoring__ema_alpha=-0.4)),
                         ('min_windows_zero', dict(scoring__min_windows=0)),
                         ('min_windows_float', dict(scoring__min_windows=2.0)),
                         ('min_windows_bool', dict(scoring__min_windows=True)),
                         ('stride_zero', dict(scoring__degraded_max_stride_factor=0)),
                         ('recovery_zero', dict(scoring__degraded_recovery_s=0))):
            with self.subTest(name):
                with self.assertRaises(ConfigError):
                    parse(changed(**kw))

    def test_wrong_types_refused(self):
        for key in ('scoring.thresholds.suspicious_enter', 'audio.hop_s', 'audio.window_s',
                    'scoring.ema_alpha', 'scoring.degraded_recovery_s'):
            for name, value in (('bool', True), ('string', '0.6'), ('nan', float('nan')),
                                ('inf', float('inf')), ('none', None), ('list', [0.6])):
                with self.subTest(key=key, value=name):
                    with self.assertRaisesRegex(ConfigError, f'{key.rsplit(".", 1)[1]} must be a finite number'):
                        parse(document(**{key: value}))
        with self.assertRaisesRegex(ConfigError, 'min_windows must be an integer'):
            parse(changed(scoring__min_windows='2'))
        # Phase-4 QA L1 (Claude-authored, 10 Nov): an integer too large for a float.
        for key in ('scoring.ema_alpha', 'audio.hop_s', 'audio.vad_threshold'):
            with self.subTest(huge=key):
                with self.assertRaises(ConfigError):
                    parse(document(**{key: 10 ** 400}))

    def test_vad_keys_get_sanity_range_only(self):
        parse(changed(audio__vad_threshold=0, audio__min_speech_fraction=1, audio__max_concealed_fraction=0.5))
        for key in ('vad_threshold', 'min_speech_fraction', 'max_concealed_fraction'):
            for value in ('abc', -5, 1.5, float('nan'), True):
                with self.subTest(key=key, value=value):
                    with self.assertRaisesRegex(ConfigError, f'audio.{key} must be a finite number from 0 to 1'):
                        parse(document(**{f'audio.{key}': value}))

    def test_directions(self):
        self.assertEqual(parse(changed(scoring__score_directions=['caller', 'agent'])).score_directions,
                         ('caller', 'agent'))
        for name, value in (('empty', []), ('duplicate', ['caller', 'caller']),
                            ('unknown', ['supervisor']), ('string', 'caller'),
                            ('nested_unhashable', [['caller']]), ('none', None)):
            with self.subTest(name):
                with self.assertRaisesRegex(ConfigError, 'score_directions'):
                    parse(changed(scoring__score_directions=value))

    def test_unknown_keys_and_bad_sections_refused(self):
        for name, doc, pattern in (
                ('audio_typo', changed(audio__hop=2.0), r"audio has unknown keys \['hop'\]"),
                ('scoring_typo', changed(scoring__ema=0.4), 'scoring has unknown keys'),
                ('threshold_typo', changed(scoring__thresholds__alert_clear=0.5), 'thresholds has unknown'),
                ('audio_not_mapping', changed(audio=[1]), 'audio must be a mapping'),
                ('thresholds_not_mapping', changed(scoring__thresholds=0.6), 'thresholds must be a mapping'),
                ('document_list', [AVA_DOCUMENT], 'configuration must be a mapping'),
                ('section_typo', {**AVA_DOCUMENT, 'scorng': {'thresholds': {'alert_enter': 0.9}}},
                 r"unknown top-level sections \['scorng'\]"),
                ('non_string_key', {**AVA_DOCUMENT, 1: {}}, 'unknown top-level sections')):
            with self.subTest(name):
                with self.assertRaisesRegex(ConfigError, pattern):
                    parse(doc)

    def test_all_problems_reported_together(self):
        doc = changed(audio__hop_s=0, scoring__ema_alpha=2, scoring__min_windows=0,
                      scoring__thresholds__alert_exit=0.9, scoring__score_directions=[])
        with self.assertRaises(ConfigError) as caught:
            parse(doc)
        self.assertEqual(len(caught.exception.problems), 5, caught.exception.problems)

    def test_model_window_must_be_valid(self):
        for value in (0, -4.0, float('nan'), None, '4', True):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ConfigError, 'model_window_s'):
                    parse(AVA_DOCUMENT, window=value)

    def test_config_is_immutable_and_input_not_retained(self):
        doc = changed(scoring__score_directions=['caller', 'agent'])
        config = parse(doc)
        doc['scoring']['score_directions'].append('x')
        doc['scoring']['thresholds']['alert_enter'] = 0.9
        self.assertEqual(config.score_directions, ('caller', 'agent'))
        self.assertEqual(config.thresholds.alert_enter, 0.75)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            config.ema_alpha = 0.9
        with self.assertRaises(dataclasses.FrozenInstanceError):
            config.thresholds.alert_enter = 0.9


class ConfigStoreTests(unittest.TestCase):
    def test_reload_applies_only_to_calls_started_after(self):
        store = ConfigStore(parse(AVA_DOCUMENT))
        running_call = store.for_new_call()
        new = store.reload(changed(scoring__thresholds__alert_enter=0.9), model_window_s=MODEL_WINDOW)
        self.assertEqual(running_call.version, 1)
        self.assertEqual(running_call.config.thresholds.alert_enter, 0.75)
        later_call = store.for_new_call()
        self.assertEqual((new.version, later_call.version), (2, 2))
        self.assertEqual(later_call.config.thresholds.alert_enter, 0.9)

    def test_rejected_reload_keeps_current_snapshot(self):
        store = ConfigStore(parse(AVA_DOCUMENT))
        before = store.for_new_call()
        with self.assertRaises(ConfigError):
            store.reload(changed(audio__hop_s=9.0), model_window_s=MODEL_WINDOW)
        with self.assertRaises(ConfigError):
            store.reload(AVA_DOCUMENT, model_window_s=3.0)
        self.assertIs(store.for_new_call(), before)

    def test_store_requires_validated_config(self):
        with self.assertRaises(TypeError):
            ConfigStore(AVA_DOCUMENT)

    def test_fingerprint_identifies_settings_not_process_version(self):
        store = ConfigStore(parse(AVA_DOCUMENT))
        first = store.for_new_call()
        again = store.reload(AVA_DOCUMENT, model_window_s=MODEL_WINDOW)
        self.assertEqual(again.version, 2)
        self.assertEqual(again.fingerprint, first.fingerprint)  # same settings, same fingerprint
        other = store.reload(changed(scoring__ema_alpha=0.5), model_window_s=MODEL_WINDOW)
        self.assertNotEqual(other.fingerprint, first.fingerprint)
        self.assertEqual(ConfigStore(parse(AVA_DOCUMENT)).for_new_call().fingerprint, first.fingerprint)
        self.assertRegex(first.fingerprint, '^[0-9a-f]{64}$')
        self.assertEqual(first.fingerprint, fingerprint(ScoringConfig()))

    def test_later_of_two_overlapping_reloads_wins(self):
        """The first reload is held inside validation; the second must wait for it, so
        the second (newer) document is the one kept. Without the reload lock the first
        would finish last and overwrite the newer settings."""
        store = ConfigStore(parse(AVA_DOCUMENT))
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)  # never leave the held thread blocked on failure
        real_parse = config_module.parse_config

        def slow_first_parse(doc, **kw):
            if doc['scoring']['ema_alpha'] == 0.5:
                entered.set()
                self.assertTrue(release.wait(10))
            return real_parse(doc, **kw)

        with mock.patch.object(config_module, 'parse_config', slow_first_parse):
            older = threading.Thread(target=store.reload, args=(changed(scoring__ema_alpha=0.5),),
                                     kwargs={'model_window_s': MODEL_WINDOW})
            older.start()
            self.assertTrue(entered.wait(10))
            newer = threading.Thread(target=store.reload, args=(changed(scoring__ema_alpha=0.7),),
                                     kwargs={'model_window_s': MODEL_WINDOW})
            newer.start()
            newer.join(0.3)
            self.assertTrue(newer.is_alive())  # waiting for the older reload
            self.assertEqual(store.for_new_call().version, 1)
            release.set()
            older.join(10)
            newer.join(10)
        self.assertEqual((store.for_new_call().version, store.for_new_call().config.ema_alpha), (3, 0.7))

    def test_reload_file_uses_hardened_reader(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_with_retry, temporary)
        store = ConfigStore(parse(AVA_DOCUMENT))
        before = store.for_new_call()
        for name, payload in (('nan.json', b'{"scoring": {"ema_alpha": NaN}}'),
                              ('big.json', b' ' * (MAX_CONFIG_BYTES + 1)),
                              ('bad.json', json.dumps(changed(audio__hop_s=0)).encode())):
            path = os.path.join(temporary.name, name)
            with open(path, 'wb') as handle:
                handle.write(payload)
            with self.subTest(name):
                with self.assertRaises(ConfigError):
                    store.reload_file(path, model_window_s=MODEL_WINDOW)
                self.assertIs(store.for_new_call(), before)
        good = os.path.join(temporary.name, 'good.json')
        with open(good, 'wb') as handle:
            handle.write(json.dumps(changed(scoring__min_windows=3)).encode())
        self.assertEqual(store.reload_file(good, model_window_s=MODEL_WINDOW).config.min_windows, 3)

    def test_concurrent_reloads_give_consistent_snapshots(self):
        """Smoke test only: under the GIL a single attribute swap is atomic anyway, so
        this cannot prove the lock is needed; it checks versions stay monotonic, every
        snapshot is one of the valid configurations and every reload is counted."""
        store = ConfigStore(parse(AVA_DOCUMENT))
        enters = [0.70 + i / 1000 for i in range(50)]
        valid = {parse(AVA_DOCUMENT)} | {parse(changed(scoring__thresholds__alert_enter=e)) for e in enters}
        errors, start = [], threading.Barrier(5)

        def reader():
            start.wait()
            last = 0
            for _ in range(2000):
                snapshot = store.for_new_call()
                if snapshot.version < last or snapshot.config not in valid:
                    errors.append(snapshot)
                last = snapshot.version

        def writer(values):
            start.wait()
            for e in values:
                store.reload(changed(scoring__thresholds__alert_enter=e), model_window_s=MODEL_WINDOW)

        threads = [threading.Thread(target=reader) for _ in range(3)]
        threads += [threading.Thread(target=writer, args=(enters[i::2],)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(store.for_new_call().version, 51)  # every reload counted once


class LoadJsonConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_with_retry, temporary)
        self.root = temporary.name

    def write(self, name, payload):
        path = os.path.join(self.root, name)
        with open(path, 'wb') as handle:
            handle.write(payload)
        return path

    def test_valid_file(self):
        path = self.write('ava.json', json.dumps(AVA_DOCUMENT).encode())
        self.assertEqual(load_json_config(path, model_window_s=MODEL_WINDOW), ScoringConfig())

    def test_malformed_files_refused(self):
        for name, payload, pattern in (
                ('broken.json', b'{"scoring": ', 'not valid UTF-8 JSON'),
                ('nan.json', b'{"scoring": {"ema_alpha": NaN}}', 'not valid UTF-8 JSON'),
                ('latin1.json', b'{"tap": "\xe9"}', 'not valid UTF-8 JSON'),
                ('list.json', b'[]', 'configuration must be a mapping'),
                ('big.json', b' ' * (MAX_CONFIG_BYTES + 1), 'exceeds'),
                ('duplicate.json', b'{"scoring": {"ema_alpha": 0.4, "ema_alpha": 2}}', 'duplicate key'),
                ('deep.json', b'[' * 60000, 'not valid UTF-8 JSON')):
            with self.subTest(name):
                with self.assertRaisesRegex(ConfigError, pattern):
                    load_json_config(self.write(name, payload), model_window_s=MODEL_WINDOW)

    def test_file_of_exactly_the_size_limit_is_accepted(self):
        body = json.dumps(AVA_DOCUMENT).encode()
        payload = body + b' ' * (MAX_CONFIG_BYTES - len(body))
        self.assertEqual(len(payload), MAX_CONFIG_BYTES)
        self.assertEqual(load_json_config(self.write('exact.json', payload), model_window_s=MODEL_WINDOW),
                         ScoringConfig())

    def test_missing_file_is_an_os_error(self):
        with self.assertRaises(FileNotFoundError):
            load_json_config(os.path.join(self.root, 'absent.json'), model_window_s=MODEL_WINDOW)


if __name__ == '__main__':
    unittest.main()
