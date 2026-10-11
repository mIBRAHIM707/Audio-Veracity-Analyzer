# CPU model source verification — todo6a

Verified4 October2026 using [the official NII model card](https://huggingface.co/nii-yamagishilab/mms-300m-anti-deepfake)
at revision `7928e119e09f616b2ce698892e00ca22dbaa40bf`.
Licence: CC BY-NC-SA4.0, research/noncommercial scope.

Official checkpoint size:1,269,622,944bytes. Read its safetensors header via bounded
HTTP range, finding431tensor entries, including trained `proj_fc.weight[2,1024]`
and `proj_fc.bias[2]`. Card specifies fake at index0, real at index1,16kHz audio,
whole-input layer normalization, temporal average and linear classifier.
This baseline is not a new AVA SLS head.

Official inference uses fairseq. The CPU adapter uses the equivalent torchaudio
wav2vec2 construction and its official fairseq-key conversion, with strict load
coverage and explicit pretraining-only exclusions. Original fairseq numerical
parity is still unmeasured; do not imply it merely from matching tensor shapes.
Sources: [torchaudio importer](https://docs.pytorch.org/audio/stable/_modules/torchaudio/models/wav2vec2/utils/import_fairseq.html),
[constructor](https://docs.pytorch.org/audio/stable/generated/torchaudio.models.wav2vec2_model.html).

Pinned CPU environment resolved and installed for Python3.11: torch2.8.0+cpu,
torchaudio2.8.0+cpu, safetensors0.6.2, psutil7.1.0 and audio dependencies. With uv,
provide `--extra-index-url https://download.pytorch.org/whl/cpu` explicitly; this
uv invocation ignores that setting when it appears only in the requirements file.
These are install-time downloads, not inference-time network access.

Next: full-file publisher-hash verification and12-file local smoke run with6genuine,
3VITS,3Tacotron inputs (55.98275seconds total). Selected deterministically by
hash with distinct publisher folders within each class/family. This is execution
evidence, not representative accuracy, calibration or telephone-domain evaluation.
