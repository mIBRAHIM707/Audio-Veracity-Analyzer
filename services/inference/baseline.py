"""Local factory for the pinned AntiDeepfake MMS-300M CPU baseline (no downloads).

Reuses the strict, hash-verified loader from models/smoke/antideepfake.py. Needs the
separate model environment (requirements-model.txt); importing this module alone
does not import torch.
"""
from pathlib import Path


def model_id():
    from models.smoke import antideepfake
    return f'{antideepfake.MODEL_REPO}@{antideepfake.MODEL_REVISION}'


def baseline_factory(model_root, threads=4):
    """Return a zero-argument factory that verifies every model file, then loads on CPU."""
    if type(threads) is not int or not 1 <= threads <= 64:
        raise ValueError('threads must be an integer in 1..64')
    root = Path(model_root).resolve()

    def load():
        from models.smoke import antideepfake
        import torch
        antideepfake._verify_model(root)
        torch.set_num_threads(threads)
        return antideepfake.CpuDetector(root / 'model.safetensors')
    return load
