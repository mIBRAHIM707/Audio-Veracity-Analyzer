"""Backlog 9.1 SLS head, truncated backbone and training harness smoke tests.

Claude-authored test design, 12 November 2026. Tiny randomly initialized models
only; never loads the 1.27 GB checkpoint. These are contract and mechanics tests,
not evidence of detection accuracy or CPU capacity.
"""
import math
import unittest

import torch

from models.ava import backbone as bb
from models.ava.model import WINDOW_SAMPLES, AvaDetector, build_detector
from models.ava.sls import CLASS_ORDER, AttentiveStatsPool, MeanPoolHead, SLSHead
from models.ava import train
from models.smoke.antideepfake import BACKBONE_CONFIG

TINY = dict(BACKBONE_CONFIG,
            extractor_conv_layer_config=[(8, 10, 5), (8, 3, 2), (8, 3, 2), (8, 3, 2),
                                         (8, 3, 2), (8, 2, 2), (8, 2, 2)],
            encoder_embed_dim=16, encoder_pos_conv_kernel=16, encoder_pos_conv_groups=4,
            encoder_num_layers=4, encoder_num_heads=2, encoder_ff_interm_features=32)


def tiny_detector(kept=2, head='sls'):
    return build_detector(kept, TINY, head)


def separable_batches(seed, batches=2, size=4):
    """Class 0: a 440 Hz tone with small noise; class 1: white noise. Fixed per seed."""
    generator = torch.Generator().manual_seed(seed)
    time_axis = torch.arange(WINDOW_SAMPLES, dtype=torch.float32) / 16000
    out = []
    for _ in range(batches):
        labels = torch.tensor([i % 2 for i in range(size)], dtype=torch.int64)
        rows = []
        for label in labels.tolist():
            noise = torch.randn(WINDOW_SAMPLES, generator=generator)
            rows.append(noise if label else torch.sin(2 * math.pi * 440 * time_axis) + 0.01 * noise)
        out.append((torch.stack(rows), labels))
    return out


class SLSHeadTests(unittest.TestCase):
    def setUp(self):
        torch.manual_seed(0)

    def test_output_contract_and_uniform_initial_layer_weighting(self):
        head = SLSHead(3, feature_dim=16)
        logits, embedding = head(torch.randn(3, 2, 7, 16))
        self.assertEqual(tuple(logits.shape), (2, len(CLASS_ORDER)))
        self.assertEqual(tuple(embedding.shape), (2, 256))
        self.assertEqual(CLASS_ORDER, ('synthetic', 'genuine'))
        self.assertTrue(torch.allclose(head.layer_weights(), torch.full((3,), 1 / 3)))

    def test_weighting_selects_a_dominant_layer(self):
        head = SLSHead(3, feature_dim=16).eval()
        hidden = torch.randn(3, 2, 5, 16)
        with torch.no_grad():
            head.layer_logits.copy_(torch.tensor([0., 60., 0.]))
            selected = head(hidden)[0]
            only_middle = head(hidden[1:2].expand(3, -1, -1, -1))[0]
        self.assertTrue(torch.allclose(selected, only_middle, atol=1e-5))
        self.assertAlmostEqual(float(head.layer_weights().detach().sum()), 1.0, places=6)

    def test_weighting_is_a_convex_combination(self):
        head = SLSHead(2, feature_dim=4).eval()
        with torch.no_grad():
            head.layer_logits.copy_(torch.tensor([math.log(3.), 0.]))  # weights 0.75 / 0.25
        hidden = torch.randn(2, 1, 3, 4)
        captured = {}

        def capture(module, inputs, output):
            captured['in'] = inputs[0]  # returns None, so the output is unchanged

        handle = head.projection.register_forward_hook(capture)
        head(hidden)
        handle.remove()
        self.assertTrue(torch.allclose(captured['in'], 0.75 * hidden[0] + 0.25 * hidden[1], atol=1e-6))

    def test_attentive_pool_with_constant_scores_is_mean_and_population_std(self):
        pool = AttentiveStatsPool(4, attention_dim=3)
        with torch.no_grad():
            pool.score.weight.zero_()
            pool.score.bias.zero_()
        frames = torch.randn(2, 9, 4) * 3 + 1
        pooled = pool(frames)
        self.assertTrue(torch.allclose(pooled[:, :4], frames.mean(1), atol=1e-5))
        self.assertTrue(torch.allclose(pooled[:, 4:], frames.std(1, unbiased=False), atol=1e-4))

    def test_attentive_pool_matches_explicit_weighted_statistics(self):
        pool = AttentiveStatsPool(2, attention_dim=2)
        with torch.no_grad():
            pool.attention.weight.copy_(torch.eye(2))
            pool.attention.bias.zero_()
            pool.score.weight.copy_(torch.tensor([[3.0, 0.0]]))
            pool.score.bias.zero_()
        # Large DC offset on channel 1 exercises the centred variance (float32).
        frames = torch.tensor([[[0.0, 1000.0], [1.0, 1001.0], [-1.0, 1003.0]]])
        weights = torch.softmax(3.0 * torch.tanh(frames[0, :, 0]), dim=0)
        self.assertFalse(torch.allclose(weights, torch.full((3,), 1 / 3), atol=0.05))
        mean = (weights[:, None] * frames[0]).sum(0)
        std = torch.sqrt((weights[:, None] * (frames[0] - mean) ** 2).sum(0))
        pooled = pool(frames)[0]
        self.assertTrue(torch.allclose(pooled[:2], mean, atol=1e-4))
        self.assertTrue(torch.allclose(pooled[2:], std, rtol=1e-3))
        unweighted = frames[0].std(0, unbiased=False)
        self.assertFalse(torch.allclose(pooled[2:].detach(), unweighted, rtol=0.1))

    def test_attentive_pool_std_is_finite_for_constant_frames(self):
        pool = AttentiveStatsPool(4)
        pooled = pool(torch.ones(1, 5, 4))
        self.assertTrue(torch.isfinite(pooled).all())
        pooled.sum().backward()
        self.assertTrue(all(torch.isfinite(p.grad).all() for p in pool.parameters()))

    def test_heads_refuse_mismatched_hidden_states_and_bad_dimensions(self):
        for head in (SLSHead(3, feature_dim=16), MeanPoolHead(3, feature_dim=16)):
            for bad in (torch.randn(2, 2, 5, 16), torch.randn(3, 2, 5, 8),
                        torch.randn(3, 5, 16), torch.randn(3, 0, 5, 16), torch.randn(3, 2, 0, 16)):
                with self.assertRaises(ValueError):
                    head(bad)
        for bad in (0, -1, True, 2.0):
            with self.assertRaises(ValueError):
                SLSHead(bad)
            with self.assertRaises(ValueError):
                MeanPoolHead(bad)

    def test_mean_pool_ablation_uses_only_the_last_retained_layer(self):
        head = MeanPoolHead(3, feature_dim=16).eval()
        hidden = torch.randn(3, 2, 5, 16)
        changed = hidden.clone()
        changed[:2] = torch.randn(2, 2, 5, 16)
        logits, embedding = head(hidden)
        self.assertEqual(tuple(embedding.shape), (2, 256))
        self.assertTrue(torch.equal(logits, head(changed)[0]))


class TruncationTests(unittest.TestCase):
    def state(self, layers):
        state = {'feature_extractor.x': torch.zeros(1), 'encoder.transformer.layer_norm.weight': torch.ones(1)}
        for index in range(layers):
            state[f'encoder.transformer.layers.{index}.attention.w'] = torch.full((1,), float(index))
        return state

    def test_truncate_state_keeps_shared_modules_and_first_layers(self):
        kept, dropped = bb.truncate_state(self.state(4), 2, 4)
        self.assertEqual(sorted(kept), ['encoder.transformer.layer_norm.weight',
                                        'encoder.transformer.layers.0.attention.w',
                                        'encoder.transformer.layers.1.attention.w', 'feature_extractor.x'])
        self.assertEqual(dropped, ['encoder.transformer.layers.2.attention.w',
                                   'encoder.transformer.layers.3.attention.w'])
        self.assertEqual(bb.truncate_state(self.state(4), 4, 4)[1], [])

    def test_truncate_state_refuses_bad_cuts_and_incomplete_states(self):
        for kept in (0, 5, True, 2.0):
            with self.assertRaises(ValueError):
                bb.truncate_state(self.state(4), kept, 4)
        gap = self.state(4)
        del gap['encoder.transformer.layers.1.attention.w']
        for state, total in ((gap, 4), (self.state(3), 4), (self.state(5), 4)):
            with self.assertRaises(ValueError):
                bb.truncate_state(state, 2, total)

    def test_backbone_config_and_sweep_values(self):
        self.assertEqual(bb.SWEEP_LAYERS, (4, 6, 7, 8, 10, 12))
        self.assertEqual(bb.backbone_config(7)['encoder_num_layers'], 7)
        self.assertEqual(BACKBONE_CONFIG['encoder_num_layers'], 24)
        for bad in (0, 25, True, '7'):
            with self.assertRaises(ValueError):
                bb.backbone_config(bad)

    def test_truncated_backbone_reproduces_the_full_models_first_layers(self):
        import torchaudio
        torch.manual_seed(1)
        full = torchaudio.models.wav2vec2_model(**TINY).eval()
        truncated = bb.truncate_backbone(full, 2, TINY).eval()
        self.assertEqual(truncated.kept_layers, 2)
        self.assertTrue(any(k.startswith('encoder.transformer.layers.3.') for k in truncated.dropped_keys))
        waveform = torch.randn(2, WINDOW_SAMPLES)
        with torch.inference_mode():
            reference, _ = full.extract_features(waveform)
            hidden = truncated(waveform)
        self.assertEqual(tuple(hidden.shape), (2, 2, 199, 16))
        for index in range(2):
            self.assertTrue(torch.equal(hidden[index], reference[index]))
        self.assertFalse(torch.equal(hidden[1], reference[3]))

    def test_truncate_backbone_refuses_a_mismatched_full_model(self):
        import torchaudio
        full = torchaudio.models.wav2vec2_model(**dict(TINY, encoder_num_layers=3))
        with self.assertRaises(ValueError):
            bb.truncate_backbone(full, 2, TINY)


class DetectorContractTests(unittest.TestCase):
    def setUp(self):
        torch.manual_seed(2)
        self.model = tiny_detector().eval()

    def test_window_contract_shapes(self):
        with torch.inference_mode():
            logits, embedding = self.model(torch.randn(3, WINDOW_SAMPLES))
        self.assertEqual(tuple(logits.shape), (3, 2))
        self.assertEqual(tuple(embedding.shape), (3, 256))
        self.assertTrue(torch.isfinite(logits).all())

    def test_rows_are_scored_independently_and_gain_does_not_matter(self):
        batch = torch.randn(3, WINDOW_SAMPLES)
        with torch.inference_mode():
            together = self.model(batch)[0]
            alone = self.model(batch[1:2])[0]
            louder = self.model(batch * 4)[0]
        self.assertTrue(torch.allclose(together[1:2], alone, atol=1e-5))
        self.assertTrue(torch.allclose(together, louder, atol=1e-4))

    def test_backbone_receives_per_window_zero_mean_unit_variance_input(self):
        seen = []
        handle = self.model.backbone.register_forward_pre_hook(lambda m, args: seen.append(args[0].clone()) and None)
        batch = torch.randn(2, WINDOW_SAMPLES) * torch.tensor([[0.1], [5.0]]) + torch.tensor([[0.3], [-2.0]])
        with torch.inference_mode():
            shifted = self.model(batch)[0]
            centred = self.model(batch - batch.mean(dim=1, keepdim=True))[0]
        handle.remove()
        self.assertTrue(torch.allclose(seen[0].mean(dim=1), torch.zeros(2), atol=1e-4))
        self.assertTrue(torch.allclose(seen[0].var(dim=1, unbiased=False), torch.ones(2), atol=1e-3))
        self.assertTrue(torch.allclose(shifted, centred, atol=1e-4))

    def test_malformed_windows_are_refused(self):
        nan = torch.zeros(1, WINDOW_SAMPLES)
        nan[0, 5] = float('nan')
        for bad in (torch.zeros(WINDOW_SAMPLES), torch.zeros(1, WINDOW_SAMPLES - 1),
                    torch.zeros(0, WINDOW_SAMPLES), torch.zeros(1, WINDOW_SAMPLES, dtype=torch.float64),
                    nan, [0.0] * WINDOW_SAMPLES):
            with self.assertRaises(ValueError):
                self.model(bad)

    def test_head_must_match_backbone(self):
        backbone = bb.build_backbone(2, TINY)
        for head in (SLSHead(3, feature_dim=16), SLSHead(2, feature_dim=32)):
            with self.assertRaises(ValueError):
                AvaDetector(backbone, head)
        with self.assertRaises(ValueError):
            build_detector(2, TINY, head='bigru')


class TrainingHarnessTests(unittest.TestCase):
    def test_config_lists_every_problem(self):
        self.assertEqual((train.TrainConfig().backbone_lr, train.TrainConfig().head_lr), (2e-5, 1e-3))
        with self.assertRaises(ValueError) as caught:
            train.TrainConfig(epochs=0, backbone_lr=float('nan'), head_lr=-1,
                              freeze_feature_encoder_epochs=-1, class_weights=(1.0,),
                              weight_decay=-0.1, max_grad_norm=0, seed=-1)
        for name in ('epochs', 'backbone_lr', 'head_lr', 'freeze_feature_encoder_epochs',
                     'class_weights', 'weight_decay', 'max_grad_norm', 'seed'):
            self.assertIn(name, str(caught.exception))
        for weights in ((1.0, True), (1.0, float('inf')), [1.0, 1.0], (1.0, 0.0)):
            with self.assertRaises(ValueError):
                train.TrainConfig(class_weights=weights)

    def test_optimizer_groups_cover_all_parameters_with_separate_rates(self):
        model = tiny_detector()
        optimizer = train.build_optimizer(model, train.TrainConfig(weight_decay=0.01))
        groups = {group['name']: group for group in optimizer.param_groups}
        self.assertEqual(sorted(groups), ['backbone', 'backbone_no_decay', 'head', 'head_no_decay'])
        for name in ('backbone', 'backbone_no_decay'):
            self.assertEqual(groups[name]['lr'], 2e-5)
        for name in ('head', 'head_no_decay'):
            self.assertEqual(groups[name]['lr'], 1e-3)
        self.assertEqual((groups['head']['weight_decay'], groups['head_no_decay']['weight_decay']), (0.01, 0.0))
        head_ids = {id(p) for name in ('head', 'head_no_decay') for p in groups[name]['params']}
        self.assertEqual(head_ids, {id(p) for p in model.head.parameters()})
        no_decay = {id(p) for p in groups['head_no_decay']['params']}
        self.assertIn(id(model.head.layer_logits), no_decay)
        self.assertIn(id(model.head.classifier.bias), no_decay)
        self.assertNotIn(id(model.head.classifier.weight), no_decay)
        covered = [id(p) for group in optimizer.param_groups for p in group['params']]
        self.assertEqual(sorted(covered), sorted(id(p) for p in model.parameters()))

    def test_layer_weighting_survives_weight_decay_without_gradient(self):
        model = tiny_detector()
        with torch.no_grad():
            model.head.layer_logits.copy_(torch.tensor([2.0, -2.0]))
        optimizer = train.build_optimizer(model, train.TrainConfig(weight_decay=0.5, head_lr=0.1))
        model.head.layer_logits.grad = torch.zeros(2)
        optimizer.step()
        self.assertTrue(torch.equal(model.head.layer_logits.detach(), torch.tensor([2.0, -2.0])))

    def test_evaluation_weights_every_window_not_every_batch(self):
        torch.manual_seed(5)
        model = tiny_detector().eval()
        waveform, labels = separable_batches(4, batches=1, size=5)[0]
        config = train.TrainConfig(class_weights=(2.0, 1.0))
        whole = train.evaluate(model, [(waveform, labels)], config)
        split = train.evaluate(model, [(waveform[:1], labels[:1]), (waveform[1:], labels[1:])], config)
        self.assertAlmostEqual(whole['mean_loss'], split['mean_loss'], places=5)
        self.assertEqual(split['windows'], 5)

    def test_training_leaves_the_feature_encoder_trainable(self):
        model = tiny_detector()
        config = train.TrainConfig(epochs=1, freeze_feature_encoder_epochs=3)
        history = train.run_training(model, config, lambda epoch: separable_batches(0, batches=1))
        self.assertFalse(history[0]['feature_encoder_trainable'])
        self.assertTrue(all(p.requires_grad for p in model.backbone.feature_encoder.parameters()))

    def test_feature_encoder_is_frozen_only_for_the_configured_epochs(self):
        torch.manual_seed(3)
        model = tiny_detector()
        config = train.TrainConfig(epochs=2, freeze_feature_encoder_epochs=1, backbone_lr=1e-2, head_lr=1e-2)
        snapshots = []
        encoder = lambda: [p.detach().clone() for p in model.backbone.feature_encoder.parameters()]
        transformer = lambda: [p.detach().clone() for p in model.backbone.model.encoder.parameters()]
        batches = separable_batches(0, batches=1)
        optimizer = train.build_optimizer(model, config)
        before = (encoder(), transformer())
        first = train.train_epoch(model, optimizer, batches, config, 0)
        after_first = (encoder(), transformer())
        second = train.train_epoch(model, optimizer, batches, config, 1)
        after_second = encoder()
        self.assertFalse(first['feature_encoder_trainable'])
        self.assertTrue(second['feature_encoder_trainable'])
        self.assertTrue(all(torch.equal(a, b) for a, b in zip(before[0], after_first[0])))
        self.assertFalse(all(torch.equal(a, b) for a, b in zip(before[1], after_first[1])))
        self.assertFalse(all(torch.equal(a, b) for a, b in zip(after_first[0], after_second)))

    def test_loss_is_class_weighted_cross_entropy(self):
        torch.manual_seed(4)
        model = tiny_detector().eval()
        waveform, labels = separable_batches(1, batches=1)[0]
        config = train.TrainConfig(class_weights=(3.0, 1.0))
        result = train.evaluate(model, [(waveform, labels)], config)
        with torch.inference_mode():
            logits = model(waveform)[0]
        log_probs = torch.log_softmax(logits, dim=1)
        weights = torch.tensor([3.0, 1.0])[labels]
        manual = -(weights * log_probs[torch.arange(4), labels]).sum() / weights.sum()
        self.assertAlmostEqual(result['mean_loss'], manual.item(), places=5)
        self.assertEqual(result['windows'], 4)

    def test_smoke_training_reduces_loss_and_is_deterministic(self):
        config = train.TrainConfig(epochs=4, freeze_feature_encoder_epochs=1,
                                   backbone_lr=1e-3, head_lr=1e-2, seed=7)
        batches = separable_batches(2)
        histories = []
        for _ in range(2):
            torch.manual_seed(7)
            model = tiny_detector()
            histories.append(train.run_training(model, config, lambda epoch: batches))
        losses = [epoch['mean_loss'] for epoch in histories[0]]
        self.assertEqual([e['step_losses'] for e in histories[0]], [e['step_losses'] for e in histories[1]])
        self.assertLess(losses[-1], losses[0] * 0.8)

    def test_bad_batches_and_non_finite_loss_are_refused(self):
        model = tiny_detector()
        config = train.TrainConfig()
        optimizer = train.build_optimizer(model, config)
        waveform = torch.randn(2, WINDOW_SAMPLES)
        for labels in (torch.tensor([0, 2]), torch.tensor([0.0, 1.0]), torch.tensor([0]), torch.tensor([[0, 1]])):
            with self.assertRaises(ValueError):
                train.train_epoch(model, optimizer, [(waveform, labels)], config, 0)
        with self.assertRaises(ValueError):
            train.train_epoch(model, optimizer, [], config, 0)
        with self.assertRaises(ValueError):
            train.evaluate(model, [], config)
        with torch.no_grad():
            model.head.classifier.bias.fill_(float('inf'))
        with self.assertRaises(FloatingPointError):
            train.train_epoch(model, optimizer, [(waveform, torch.tensor([0, 1]))], config, 0)

    def test_layer_sweep_reports_each_cut(self):
        config = train.TrainConfig(epochs=1, freeze_feature_encoder_epochs=1, head_lr=1e-2)
        batches = separable_batches(3, batches=1)
        report = train.layer_sweep([1, 3], lambda kept: tiny_detector(kept), config,
                                   lambda epoch: batches, lambda: batches)
        self.assertEqual([row['kept_layers'] for row in report['rows']], [1, 3])
        self.assertLess(report['rows'][0]['parameters'], report['rows'][1]['parameters'])
        for row in report['rows']:
            self.assertEqual(len(row['layer_weights']), row['kept_layers'])
            self.assertTrue(math.isfinite(row['eval']['mean_loss']))
            self.assertGreater(row['forward_seconds_one_window'], 0)
        self.assertIn('no accuracy', report['limitations'])
        ablation = train.layer_sweep([2], lambda kept: tiny_detector(kept, 'mean_pool'), config,
                                     lambda epoch: batches, lambda: batches)
        self.assertIsNone(ablation['rows'][0]['layer_weights'])
        for bad in ([], [2, 2], [0], (True,), 2):
            with self.assertRaises(ValueError):
                train.layer_sweep(bad, tiny_detector, config, lambda e: batches, lambda: batches)
        with self.assertRaises(ValueError):
            train.layer_sweep([2], lambda kept: tiny_detector(1), config, lambda e: batches, lambda: batches)


if __name__ == '__main__':
    unittest.main()
