"""AASIST baseline adapter (backlog 9.2; PRD FR-M4).

Claude-authored test design, 20 November 2026. Random weights and generated signals;
structure, contract and reference-fidelity checks only, no baseline accuracy.

The parity cases compare against the upstream reference implementation when
``AVA_AASIST_UPSTREAM`` names a clone of clovaai/aasist (outside this repository,
checked out at UPSTREAM_COMMIT; another commit fails). They skip without the variable,
unless ``AVA_REQUIRE_UPSTREAM=1``, which turns the skip into a failure so a run meant
as parity evidence cannot pass silently.
"""
import importlib.util
import math
import os
from pathlib import Path
import unittest

import numpy as np
import torch

from models.ava import train
from models.ava.model import WINDOW_SAMPLES
from models.baselines import aasist as aa
from tests.model.test_ava_sls import separable_batches

TINY = dict(first_conv=16, filts=(9, (1, 4), (4, 4), (4, 8), (8, 8)), gat_dims=(8, 4),
            pool_ratios=(0.5, 0.7, 0.5, 0.5), temperatures=(2.0, 2.0, 100.0, 100.0))


UPSTREAM_COMMIT = 'a04c9863f63d44471dde8a6abcb3b082b07cd1d1'


def upstream_root():
    root = os.environ.get('AVA_AASIST_UPSTREAM')
    return Path(root) if root else None


def checked_out_commit(root):
    """The clone's HEAD commit, read from the .git files (loose or packed ref)."""
    git = root / '.git'
    head = (git / 'HEAD').read_text(encoding='ascii').strip()
    if not head.startswith('ref: '):
        return head
    ref = head[len('ref: '):]
    loose = git / ref
    if loose.is_file():
        return loose.read_text(encoding='ascii').strip()
    for line in (git / 'packed-refs').read_text(encoding='ascii').splitlines():
        if line.endswith(' ' + ref):
            return line.split(' ', 1)[0]
    return None


def load_upstream():
    """The reference ``models/AASIST.py`` module, loaded by path (never imported from
    inside the clone, so nothing else there runs)."""
    path = upstream_root() / 'models' / 'AASIST.py'
    spec = importlib.util.spec_from_file_location('aasist_upstream_reference', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def upstream_config(config):
    return {'first_conv': config['first_conv'], 'filts': [config['filts'][0]] + [list(p) for p in config['filts'][1:]],
            'gat_dims': list(config['gat_dims']), 'pool_ratios': list(config['pool_ratios']),
            'temperatures': list(config['temperatures'])}


class SincTests(unittest.TestCase):
    def test_filters_are_fixed_symmetric_and_outside_the_state_dict(self):
        model = aa.AASIST(TINY)
        self.assertEqual(list(model.backbone.feature_encoder.parameters()), [])
        self.assertFalse(any('conv_time' in key for key in model.state_dict()))
        bank, edges = aa.sinc_filterbank(70, 128)
        self.assertEqual(tuple(bank.shape), (70, 129))
        taps = bank.numpy().astype(np.float64)
        self.assertTrue(np.allclose(taps, taps[:, ::-1], atol=1e-7))
        self.assertTrue(np.allclose(np.diff(aa.to_mel(edges)), aa.to_mel(edges)[1]))
        self.assertEqual((edges[0], round(float(edges[-1]), 6)), (0.0, 8000.0))
        self.assertEqual(tuple(model.backbone.conv_time.filters.shape), (9, 1, 17))

    def test_filterbank_refusals(self):
        for args in ((1, 31), (4, 1), (True, 31), (4, 31.0)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                aa.sinc_filterbank(*args)


class LayerTests(unittest.TestCase):
    def test_graph_pool_keeps_top_scored_nodes_scaled_by_score(self):
        pool = aa.GraphPool(0.5, 2).eval()
        with torch.no_grad():
            pool.proj.weight.copy_(torch.tensor([[1.0, 0.0]]))
            pool.proj.bias.zero_()
        h = torch.tensor([[[1.0, 5.0], [3.0, 6.0], [2.0, 7.0], [4.0, 8.0]]])
        out = pool(h)
        s = torch.sigmoid
        expected = torch.tensor([[[4.0, 8.0], [3.0, 6.0]]]) * torch.stack([s(torch.tensor(4.0)),
                                                                            s(torch.tensor(3.0))]).view(1, 2, 1)
        self.assertTrue(torch.allclose(out, expected))
        self.assertEqual(aa.GraphPool(0.1, 2).eval()(h).shape[1], 1)  # never fewer than one node

    def test_heterogeneous_attention_matches_an_elementwise_derivation(self):
        torch.manual_seed(5)
        layer = aa.HtrgGraphAttentionLayer(3, 2, temperature=2.0).eval()
        x1, x2, master = torch.randn(1, 2, 3), torch.randn(1, 3, 3), torch.randn(1, 1, 3)
        got1, got2, got_master = layer(x1, x2, master)
        with torch.no_grad():
            x = torch.cat([layer.proj_type1(x1), layer.proj_type2(x2)], dim=1)[0]
            n = x.shape[0]
            score = torch.empty(n, n)
            for i in range(n):
                for j in range(n):
                    weight = (layer.att_weight11 if i < 2 and j < 2 else layer.att_weight22 if i >= 2 and j >= 2
                              else layer.att_weight12)  # both cross blocks use 12
                    score[i, j] = (torch.tanh(layer.att_proj(x[i] * x[j])) @ weight)[0] / 2.0
            att = torch.softmax(score, dim=1)  # each node's weights over its neighbours sum to 1
            nodes = layer.proj_with_att(att @ x) + layer.proj_without_att(x)
            nodes = torch.nn.functional.selu(layer.bn(nodes))
            m_score = torch.stack([(torch.tanh(layer.att_projM(x[i] * master[0, 0])) @ layer.att_weightM)[0] / 2.0
                                   for i in range(n)])
            m_att = torch.softmax(m_score, dim=0)
            new_master = layer.proj_with_attM(m_att @ x) + layer.proj_without_attM(master[0, 0])
        self.assertTrue(torch.allclose(got1[0], nodes[:2], atol=1e-5))
        self.assertTrue(torch.allclose(got2[0], nodes[2:], atol=1e-5))
        self.assertTrue(torch.allclose(got_master[0, 0], new_master, atol=1e-5))
        self.assertFalse(any('att_weight21' in name for name, _ in layer.named_parameters()))

    def test_residual_block_quirk_and_shapes(self):
        torch.manual_seed(0)
        block = aa.ResidualBlock(4, 8).eval()
        x = torch.randn(2, 4, 5, 30)
        before = block(x)
        self.assertEqual(tuple(before.shape), (2, 8, 5, 10))
        with torch.no_grad():
            block.bn1.weight.mul_(3.0)
            block.bn1.bias.add_(1.0)
        self.assertTrue(torch.equal(before, block(x)))  # the pre-activation is unused
        stats = block.bn1.running_mean.clone()
        trained = block.train()(x)
        self.assertFalse(torch.equal(stats, block.bn1.running_mean))  # but its statistics move, as upstream
        with torch.no_grad():
            block.bn1.weight.mul_(5.0)
        self.assertTrue(torch.equal(trained, block(x)))  # unused in training mode too
        self.assertFalse(hasattr(aa.ResidualBlock(1, 4, first=True), 'bn1'))
        self.assertFalse(aa.ResidualBlock(4, 4).downsample)


class ContractTests(unittest.TestCase):
    def test_reference_sizes_and_known_parameter_counts(self):
        reference, light = aa.AASIST(), aa.AASIST(aa.LIGHT_CONFIG)
        # 297,866 = the paper's "297K"; 85,306 = the upstream README's AASIST-L count.
        self.assertEqual(sum(p.numel() for p in reference.parameters()), 297_866)
        self.assertEqual(sum(p.numel() for p in light.parameters()), 85_306)
        self.assertEqual((aa.temporal_nodes(), aa.temporal_nodes(samples=64_600)), (29, 29))
        seen = {}

        def record(name):
            return lambda module, inputs, output: seen.__setitem__(name, tuple(inputs[0].shape))

        hooks = [reference.backbone.GAT_layer_S.register_forward_hook(record('S')),
                 reference.backbone.GAT_layer_T.register_forward_hook(record('T')),
                 reference.head.out_layer.register_forward_hook(record('readout'))]
        torch.manual_seed(1)
        with torch.inference_mode():
            logits, embedding = reference.eval()(torch.randn(2, WINDOW_SAMPLES) * 0.1)
        for hook in hooks:
            hook.remove()
        self.assertEqual(seen, {'S': (2, 23, 64), 'T': (2, 29, 64), 'readout': (2, 160)})
        self.assertEqual((tuple(logits.shape), tuple(embedding.shape)), ((2, 2), (2, 160)))
        self.assertTrue(torch.isfinite(logits).all())

    def test_rows_are_independent_in_eval_mode(self):
        torch.manual_seed(2)
        model = aa.AASIST(TINY).eval()
        batch = torch.randn(3, WINDOW_SAMPLES) * 0.1
        with torch.inference_mode():
            together = model(batch)
            alone = model(batch[2:])
        self.assertTrue(torch.allclose(together[0][2:], alone[0], atol=1e-5))
        self.assertTrue(torch.allclose(together[1][2:], alone[1], atol=1e-5))

    def test_embedding_is_the_readout_and_logits_its_projection(self):
        torch.manual_seed(3)
        model = aa.AASIST(TINY).eval()
        with torch.inference_mode():
            waveform = torch.randn(2, WINDOW_SAMPLES) * 0.1
            logits, embedding = model(waveform)
            self.assertTrue(torch.equal(embedding, model.backbone(waveform)))
            self.assertTrue(torch.allclose(logits, model.head.out_layer(embedding)))

    def test_last_pool_ratio_and_temperature_are_unused_as_upstream(self):
        torch.manual_seed(4)
        model = aa.AASIST(TINY).eval()
        waveform = torch.randn(1, WINDOW_SAMPLES) * 0.1

        def logits_with(**change):
            other = aa.AASIST(dict(TINY, **change)).eval()
            other.load_state_dict(model.state_dict())
            with torch.inference_mode():
                return other(waveform)[0]

        with torch.inference_mode():
            base = model(waveform)[0]
        self.assertTrue(torch.equal(base, logits_with(pool_ratios=(0.5, 0.7, 0.5, 0.9))))
        self.assertTrue(torch.equal(base, logits_with(temperatures=(2.0, 2.0, 100.0, 0.5))))
        # Controls: the entries that are used do change the output.
        self.assertFalse(torch.equal(base, logits_with(pool_ratios=(0.5, 0.7, 0.9, 0.5))))
        self.assertFalse(torch.equal(base, logits_with(temperatures=(2.0, 2.0, 0.5, 100.0))))

    def test_input_and_configuration_refusals(self):
        model = aa.AASIST(TINY)
        nan = torch.zeros(1, WINDOW_SAMPLES)
        nan[0, 3] = float('nan')
        for bad in (torch.zeros(WINDOW_SAMPLES), torch.zeros(1, 64_600), torch.zeros(0, WINDOW_SAMPLES),
                    torch.zeros(1, WINDOW_SAMPLES, dtype=torch.float64), nan, [0.0] * WINDOW_SAMPLES):
            with self.assertRaises(ValueError):
                model(bad)
        for change in ({'nb_samp': 64_600}, {'first_conv': 1}, {'first_conv': True}, {'filts': (9,)},
                       {'filts': (2, (1, 4), (4, 4), (4, 8), (8, 8))},
                       {'filts': (9, (2, 4), (4, 4), (4, 8), (8, 8))},
                       {'filts': (9, (1, 4), (4, 4), (4, 8), (8, 9))},
                       {'filts': (9, (1, 4), (5, 4), (4, 8), (8, 8))},
                       {'filts': (9, (1, 4), (4, 4), (4, 8.0), (8, 8))},
                       {'gat_dims': (8,)}, {'gat_dims': (8, 0)}, {'pool_ratios': (0.5, 0.7, 0.5)},
                       {'pool_ratios': (0.5, 0.7, 1.5, 0.5)}, {'pool_ratios': (0.5, 0.0, 0.5, 0.5)},
                       {'temperatures': (2.0, 2.0, float('inf'), 100.0)}, {'temperatures': (2.0, True, 1.0, 1.0)},
                       {'first_conv': 70_000}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                aa.AASIST(dict(TINY, **change))
        for config in ([('first_conv', 16)], {1: 16}, 'AASIST'):
            with self.subTest(config=config), self.assertRaises(ValueError):
                aa.AASIST(config)

    def test_reference_training_configuration(self):
        config = aa.AASIST_TRAIN_CONFIG
        self.assertEqual((config.backbone_lr, config.head_lr, config.weight_decay), (1e-4, 1e-4, 1e-4))
        self.assertEqual((config.freeze_feature_encoder_epochs, config.class_weights), (0, (0.1, 0.9)))
        self.assertEqual((aa.REFERENCE_TRAINING['batch_size'], aa.REFERENCE_TRAINING['num_epochs']), (24, 100))

    def test_trains_through_the_shared_harness(self):
        torch.manual_seed(3)
        model = aa.AASIST(TINY)
        config = train.TrainConfig(epochs=4, backbone_lr=3e-3, head_lr=3e-3, freeze_feature_encoder_epochs=1)
        optimizer = train.build_optimizer(model, config)
        self.assertEqual(sum(len(g['params']) for g in optimizer.param_groups), len(list(model.parameters())))
        batches = separable_batches(5)
        history = train.run_training(model, config, lambda epoch: batches)
        self.assertTrue(all(math.isfinite(epoch['mean_loss']) for epoch in history))
        self.assertLess(history[-1]['mean_loss'], history[0]['mean_loss'])
        self.assertEqual(train.evaluate(model, batches, config)['windows'], 8)


class ReferenceStateTests(unittest.TestCase):
    def test_key_mapping_round_trip(self):
        torch.manual_seed(6)
        source, target = aa.AASIST(TINY), aa.AASIST(TINY)
        reference_state = {aa.reference_key(k): v for k, v in source.state_dict().items()}
        self.assertIn('out_layer.weight', reference_state)
        self.assertIn('encoder.0.0.conv1.weight', reference_state)
        aa.load_reference_state_dict(target, reference_state)
        for key, value in source.state_dict().items():
            self.assertTrue(torch.equal(value, target.state_dict()[key]))
        with self.assertRaises(ValueError):
            aa.reference_key('pos_S')

    def test_refusals_load_nothing(self):
        torch.manual_seed(7)
        source, target = aa.AASIST(TINY), aa.AASIST(TINY)
        before = {k: v.clone() for k, v in target.state_dict().items()}
        good = {aa.reference_key(k): v for k, v in source.state_dict().items()}
        wrong_shape = dict(good, pos_S=torch.zeros(1, 4, 8))
        missing = {k: v for k, v in good.items() if k != 'master1'}
        extra = dict(good, **{'conv_time.band_pass': torch.zeros(9, 17)})
        for bad in (wrong_shape, missing, extra):
            with self.assertRaises(ValueError):
                aa.load_reference_state_dict(target, bad)
            for key, value in before.items():
                self.assertTrue(torch.equal(value, target.state_dict()[key]))


class UpstreamParityTests(unittest.TestCase):
    """Same weights, same input: the adapter must reproduce the reference ``Model``."""

    @classmethod
    def setUpClass(cls):
        root = upstream_root()
        if root is None:
            if os.environ.get('AVA_REQUIRE_UPSTREAM') == '1':
                raise AssertionError('AVA_REQUIRE_UPSTREAM=1 but AVA_AASIST_UPSTREAM is not set')
            raise unittest.SkipTest('AVA_AASIST_UPSTREAM (a clovaai/aasist clone) not set')
        commit = checked_out_commit(root)
        if commit != UPSTREAM_COMMIT:
            raise AssertionError(f'The clone is at {commit}, not {UPSTREAM_COMMIT}; check out that commit')
        cls.upstream = load_upstream()

    def pair(self, config, seed):
        torch.manual_seed(seed)
        reference = self.upstream.Model(upstream_config(config))
        ours = aa.AASIST(config)
        aa.load_reference_state_dict(ours, reference.state_dict())
        return reference, ours

    def assert_close(self, got, expected, atol=1e-6):
        # Tight on purpose: at temperature 100 a wrong heterogeneous attention weight
        # moves random-weight logits by only about 2e-5 (mutant run, 20 Nov).
        diff = (got - expected).abs().max().item()
        self.assertLessEqual(diff, atol, f'max |difference| {diff}')

    def test_sinc_filters_equal_the_reference(self):
        for config in (aa.REFERENCE_CONFIG, aa.LIGHT_CONFIG, TINY):
            reference = self.upstream.CONV(out_channels=config['filts'][0], kernel_size=config['first_conv'])
            ours = aa.sinc_filterbank(config['filts'][0], config['first_conv'])[0]
            self.assertTrue(torch.equal(ours, reference.band_pass))

    def test_parameter_names_shapes_and_counts_match(self):
        for config in (aa.REFERENCE_CONFIG, aa.LIGHT_CONFIG):
            reference, ours = self.pair(config, 0)
            self.assertEqual({k: tuple(v.shape) for k, v in reference.state_dict().items()},
                             {aa.reference_key(k): tuple(v.shape) for k, v in ours.state_dict().items()})
            self.assertEqual(sum(p.numel() for p in reference.parameters()),
                             sum(p.numel() for p in ours.parameters()))

    def test_eval_outputs_match_on_random_weights(self):
        # Not TINY: the reference hard-codes 23 spectral positions (70 sinc filters).
        for config, seed in ((aa.REFERENCE_CONFIG, 10), (aa.LIGHT_CONFIG, 11)):
            reference, ours = self.pair(config, seed)
            reference.eval(), ours.eval()
            waveform = torch.randn(3, WINDOW_SAMPLES, generator=torch.Generator().manual_seed(seed)) * 0.1
            with torch.inference_mode():
                hidden, expected = reference(waveform.clone())
                logits, embedding = ours(waveform)
            with self.subTest(config=config['filts'][3]):
                self.assert_close(logits, expected)
                self.assert_close(embedding, hidden)

    def test_training_mode_matches_including_dropout_order_and_statistics(self):
        for config, seed in ((aa.REFERENCE_CONFIG, 13), (aa.LIGHT_CONFIG, 15)):
            reference, ours = self.pair(config, seed)
            reference.train(), ours.train()
            waveform = torch.randn(2, WINDOW_SAMPLES, generator=torch.Generator().manual_seed(seed)) * 0.1
            torch.manual_seed(99)
            _, expected = reference(waveform.clone())
            torch.manual_seed(99)
            logits, _ = ours(waveform)
            with self.subTest(config=config['filts'][3]):
                self.assert_close(logits, expected)
                mapped = {aa.reference_key(k): v for k, v in ours.state_dict().items()}
                for key, value in reference.state_dict().items():  # BatchNorm statistics, bn1 included
                    # Relative: running variances can be large, and summation order may
                    # differ with thread count (review L, 20 Nov).
                    torch.testing.assert_close(mapped[key].float(), value.float(), rtol=1e-5, atol=1e-6,
                                               msg=lambda text, key=key: f'{key}: {text}')

    def published_weights_case(self, config, name):
        # A load-and-score check. It does not catch every structural error: with these
        # weights a wrong cross-type attention weight changes no logit by 1e-6 (mutant
        # run, 20 Nov); the random-weight case above and the element-wise test do.
        path = upstream_root() / 'models' / 'weights' / name
        if not path.is_file():
            self.skipTest(f'{name} not in the clone')
        state = torch.load(path, map_location='cpu', weights_only=True)
        reference = self.upstream.Model(upstream_config(config))
        reference.load_state_dict(state)
        ours = aa.load_reference_state_dict(aa.AASIST(config), state)
        reference.eval(), ours.eval()
        waveform = torch.randn(4, WINDOW_SAMPLES, generator=torch.Generator().manual_seed(14)) * 0.1
        with torch.inference_mode():
            _, expected = reference(waveform.clone())
            logits, _ = ours(waveform)
        self.assert_close(logits, expected)

    def test_published_aasist_weights_load_and_score_identically(self):
        self.published_weights_case(aa.REFERENCE_CONFIG, 'AASIST.pth')

    def test_published_aasist_l_weights_load_and_score_identically(self):
        self.published_weights_case(aa.LIGHT_CONFIG, 'AASIST-L.pth')


if __name__ == '__main__':
    unittest.main()
