"""RawNet2 baseline adapter (backlog 9.2; PRD FR-M4).

Claude-authored test design, 12 November 2026. Random weights and generated
signals; structure and contract checks only, no baseline accuracy.
"""
import math
import unittest

import numpy as np
import torch

from models.ava import train
from models.ava.model import WINDOW_SAMPLES
from models.baselines import rawnet2 as rn
from tests.model.test_ava_sls import separable_batches

TINY = dict(sinc_channels=4, sinc_kernel=31, blocks=((4, 4), (4, 8), (8, 8)),
            gru_hidden=8, gru_layers=1, fc_hidden=16)


def gain_at(taps, hz, rate=16000):
    return abs(np.sum(taps * np.exp(-2j * math.pi * hz / rate * np.arange(len(taps)))))


class SincFilterbankTests(unittest.TestCase):
    def test_mel_spaced_band_pass_filters_cover_every_band(self):
        bank, freq = rn.sinc_filterbank(20, 1024)
        self.assertEqual(tuple(bank.shape), (20, 1, 1025))
        self.assertEqual(freq[0], 0.0)
        edges = np.append(freq, 8000.0)
        mel = rn.to_mel(edges)
        self.assertTrue(np.allclose(np.diff(mel), mel[1] - mel[0]))
        self.assertAlmostEqual(float(rn.to_hz(rn.to_mel(np.array([1234.5])))[0]), 1234.5, places=6)
        taps = bank[:, 0].numpy().astype(np.float64)
        self.assertTrue(np.allclose(taps, taps[:, ::-1], atol=1e-7))
        for i in (3, 10, 18, 19):
            centre = (edges[i] + edges[i + 1]) / 2
            self.assertAlmostEqual(gain_at(taps[i], centre), 1.0, delta=0.02)
            if i < 19:
                self.assertLess(gain_at(taps[i], edges[i + 1] + 400), 0.02)
        self.assertLess(gain_at(taps[19], edges[19] - 400), 0.02)

    def test_filterbank_refusals_and_odd_kernel(self):
        self.assertEqual(rn.sinc_filterbank(4, 30)[0].shape[2], 31)
        for args in ((1, 31), (4, 2), (True, 31), (4, 31.0)):
            with self.assertRaises(ValueError):
                rn.sinc_filterbank(*args)

    def test_filterbank_shapes_for_other_sizes(self):
        bank, freq = rn.sinc_filterbank(4, 30)
        self.assertEqual((tuple(bank.shape), len(freq)), ((4, 1, 31), 4))
        self.assertTrue(all(bank[i].abs().sum() > 0 for i in range(4)))

    def test_sinc_front_end_is_fixed(self):
        model = rn.RawNet2(TINY)
        self.assertEqual(list(model.backbone.feature_encoder.parameters()), [])
        self.assertIn('backbone.sinc.filters', model.state_dict())


class BlockTests(unittest.TestCase):
    def test_residual_block_shapes_and_downsample(self):
        same = rn.ResidualBlock(4, 4)
        wider = rn.ResidualBlock(4, 8)
        self.assertIsNone(same.downsample)
        self.assertIsNotNone(wider.downsample)
        x = torch.randn(2, 4, 99)
        self.assertEqual(tuple(same(x).shape), (2, 4, 33))
        self.assertEqual(tuple(wider(x).shape), (2, 8, 33))
        self.assertFalse(hasattr(rn.ResidualBlock(4, 4, first=True), 'bn1'))

    def test_reference_quirk_discards_the_pre_activation(self):
        torch.manual_seed(0)
        x = torch.randn(2, 4, 30)
        for quirks, changes in ((True, False), (False, True)):
            block = rn.ResidualBlock(4, 4, reference_quirks=quirks).eval()
            before = block(x)
            with torch.no_grad():
                block.bn1.weight.mul_(3.0)
                block.bn1.bias.add_(1.0)
            self.assertEqual(not torch.allclose(before, block(x)), changes)

    def test_feature_map_scaling_is_x_times_s_plus_s(self):
        fms = rn.FeatureMapScaling(3)
        with torch.no_grad():
            fms.fc.weight.zero_()
            fms.fc.bias.copy_(torch.tensor([0.0, 100.0, -100.0]))
        x = torch.randn(2, 3, 5)
        y = fms(x)
        self.assertTrue(torch.allclose(y[:, 0], x[:, 0] * 0.5 + 0.5))
        self.assertTrue(torch.allclose(y[:, 1], x[:, 1] + 1.0))
        self.assertTrue(torch.allclose(y[:, 2], torch.zeros(2, 5), atol=1e-6))


class RawNet2ContractTests(unittest.TestCase):
    def test_reference_size_contract_and_gru_length(self):
        torch.manual_seed(1)
        model = rn.RawNet2().eval()
        self.assertEqual(rn.frames_after_pooling(), 28)
        with torch.inference_mode():
            logits, embedding = model(torch.randn(2, WINDOW_SAMPLES) * 0.1)
        self.assertEqual((tuple(logits.shape), tuple(embedding.shape)), ((2, 2), (2, 1024)))
        self.assertTrue(torch.isfinite(logits).all())
        seen = []

        def record_gru_input(module, inputs, output):
            seen.append(inputs[0].shape)

        handle = model.backbone.gru.register_forward_hook(record_gru_input)
        with torch.inference_mode():
            model(torch.randn(1, WINDOW_SAMPLES) * 0.1)
        handle.remove()
        self.assertEqual(tuple(seen[0]), (1, 28, 128))

    def test_rows_are_independent_in_eval_mode(self):
        torch.manual_seed(2)
        model = rn.RawNet2(TINY).eval()
        batch = torch.randn(3, WINDOW_SAMPLES) * 0.1
        with torch.inference_mode():
            together = model(batch)[0]
            alone = model(batch[2:])[0]
        self.assertTrue(torch.allclose(together[2:], alone, atol=1e-5))

    def test_refusals(self):
        model = rn.RawNet2(TINY)
        nan = torch.zeros(1, WINDOW_SAMPLES)
        nan[0, 3] = float('nan')
        for bad in (torch.zeros(WINDOW_SAMPLES), torch.zeros(1, 64600), torch.zeros(0, WINDOW_SAMPLES),
                    torch.zeros(1, WINDOW_SAMPLES, dtype=torch.float64), nan):
            with self.assertRaises(ValueError):
                model(bad)
        for config in ({'gru_hidden': 0}, {'layers': 3}, {'blocks': ((8, 8),)},
                       {'blocks': ((4, 8), (4, 8))}, {'fc_hidden': True}, {'blocks': ((4, 0),)},
                       {'blocks': ((4, 4.0),)}, {'blocks': ((4,),)}, {'blocks': 4},
                       {'sinc_kernel': 70_000}, {'blocks': ((4, 4),) * 12}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                rn.RawNet2(dict(TINY, **config))

    def test_forward_follows_the_reference_operation_order(self):
        torch.manual_seed(4)
        model = rn.RawNet2(TINY).eval()
        body = model.backbone
        waveform = torch.randn(2, WINDOW_SAMPLES) * 0.1
        with torch.inference_mode():
            x = torch.nn.functional.conv1d(waveform.unsqueeze(1), body.sinc.filters)
            x = torch.nn.functional.selu(body.first_bn(torch.nn.functional.max_pool1d(x.abs(), 3)))
            for block, scaling in zip(body.blocks, body.scalings):
                x = block(x)
                s = torch.sigmoid(scaling.fc(x.mean(dim=2))).unsqueeze(2)
                x = x * s + s
            x = torch.nn.functional.selu(body.bn_before_gru(x)).permute(0, 2, 1)
            sequence, _ = body.gru(x)
            embedding = model.head.fc1(sequence[:, -1])
            expected = model.head.fc2(embedding)
            logits, got_embedding = model(waveform)
        self.assertTrue(torch.allclose(logits, expected, atol=1e-6))
        self.assertTrue(torch.allclose(got_embedding, embedding, atol=1e-6))
        self.assertFalse(torch.allclose(sequence[:, -1], sequence[:, 0]))

    def test_reference_training_configuration(self):
        config = rn.RAWNET2_TRAIN_CONFIG
        self.assertEqual((config.backbone_lr, config.head_lr, config.weight_decay), (1e-4, 1e-4, 1e-4))
        self.assertEqual((config.freeze_feature_encoder_epochs, config.class_weights), (0, (0.1, 0.9)))

    def test_trains_through_the_shared_harness(self):
        torch.manual_seed(3)
        model = rn.RawNet2(TINY)
        config = train.TrainConfig(epochs=3, backbone_lr=1e-3, head_lr=1e-3, freeze_feature_encoder_epochs=1)
        optimizer = train.build_optimizer(model, config)
        covered = sum(len(g['params']) for g in optimizer.param_groups)
        self.assertEqual(covered, len(list(model.parameters())))
        batches = separable_batches(5)
        history = train.run_training(model, config, lambda epoch: batches)
        self.assertTrue(all(math.isfinite(epoch['mean_loss']) for epoch in history))
        self.assertLess(history[-1]['mean_loss'], history[0]['mean_loss'])
        evaluation = train.evaluate(model, batches, config)
        self.assertEqual(evaluation['windows'], 8)


if __name__ == '__main__':
    unittest.main()
