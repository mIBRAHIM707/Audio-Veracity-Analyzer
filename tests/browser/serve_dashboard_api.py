"""Browser-test server: the real analysis API serving the built dashboard (backlog 2.3).

Claude-authored test support (14 November 2026). The API, decoding, conditioning,
windowing, aggregation and job path are the production code; only the detector is a
deterministic fake, so Playwright can drive known verdicts without model weights.
Infrastructure evidence only: it says nothing about detection accuracy.

Fake detector: logits [fake, real] = [40 * (zero-crossing rate - 0.1), 0], so a 300 Hz
tone scores genuine and a 3000 Hz tone scores synthetic after 16 kHz conditioning. A
window whose zero-crossing rate is between 0.11 and 0.14 (a 1000 Hz tone) raises, to
exercise the detector-failure path.

Run from the repository root (Playwright's spec starts and stops it):
uv run --no-project --python 3.11 --with-requirements requirements-api.txt \
    python -m tests.browser.serve_dashboard_api --port 8765 --dashboard dashboard/dist
"""
import argparse

import numpy as np
import uvicorn

from services.api.app import create_app
from services.inference.analysis import AnalysisService


def zero_crossing_rate(samples):
    signs = np.signbit(samples)
    return float(np.count_nonzero(signs[1:] != signs[:-1])) / max(len(samples) - 1, 1)


def fake_detector(samples):
    rate = zero_crossing_rate(np.asarray(samples, dtype=np.float32))
    if 0.11 <= rate <= 0.14:
        raise RuntimeError('fake detector fault (1000 Hz fixture)')
    return [40.0 * (rate - 0.1), 0.0]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--dashboard', required=True)
    args = parser.parse_args()
    service = AnalysisService(lambda: fake_detector, model_id='browser-fixture-fake-detector')
    app = create_app(service, dashboard_dir=args.dashboard)
    uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
