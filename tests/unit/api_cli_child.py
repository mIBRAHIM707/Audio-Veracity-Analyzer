"""Child-process entry for tests/unit/test_api_cli.py (Claude-authored, 16 November
2026). Runs the real `services.api.__main__.main` (argument checks, bind, Redis relay,
uvicorn, signal handling, records) with only the detector replaced by the API test
fixture, so the child needs neither torch nor model weights. Not a test module.

    python -m tests.unit.api_cli_child --model-root DIR --port 0 [...]
"""
import sys

from services.api.__main__ import main
from services.inference import analysis
from tests.unit import test_analysis_api as api_fixture


def fixture_service(args):
    return analysis.AnalysisService(lambda: api_fixture.Detector(), model_id='fx-child-model')


if __name__ == '__main__':
    sys.exit(main(made={'service': fixture_service}))
