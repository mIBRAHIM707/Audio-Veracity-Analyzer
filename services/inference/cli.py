"""Analyse one local WAV with the pinned CPU baseline and print the result (no downloads).

Usage: python -m services.inference.cli --model-root <local model dir> --wav <file> [--threads 4]
Needs requirements-model.txt. Prints verdict, scores and timings; never prints the path.
"""
import argparse
import json
from pathlib import Path
import sys

from services.inference.analysis import (AnalysisError, AnalysisService, MAX_ASYNC_SECONDS, MAX_BYTES,
                                         MAX_SYNC_SECONDS, NeedsAsyncJob)
from services.inference.baseline import baseline_factory, model_id


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--model-root', required=True, type=Path)
    parser.add_argument('--wav', required=True, type=Path)
    parser.add_argument('--threads', type=int, default=4)
    parser.add_argument('--long', action='store_true', help='allow files up to 10 minutes (job mode)')
    args = parser.parse_args(argv)
    try:
        with args.wav.open('rb') as stream:
            payload = stream.read(MAX_BYTES + 1)
        service = AnalysisService(baseline_factory(args.model_root, args.threads), model_id=model_id())
        result = service.analyze_wav(payload, MAX_ASYNC_SECONDS if args.long else MAX_SYNC_SECONDS)
    except NeedsAsyncJob:
        print('File is longer than 60 s: rerun with --long (the API will return a job)', file=sys.stderr)
        return 2
    except (OSError, ValueError) as error:
        print(f'Analysis failed: {type(error).__name__}', file=sys.stderr)
        return 1
    except AnalysisError as error:
        print(f'Analysis failed ({error.status}): {error}', file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
