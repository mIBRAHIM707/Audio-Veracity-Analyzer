# Existing-synthetic source verification — todo5a

Checked4 October2026 against the official publisher repository and Hugging Face
metadata at revision `eb8f16623108324867e4424a646937409c64b82b`.

- [Publisher repository](https://github.com/CSALT-LUMS/urdu-deepfake-dataset)
  identifies Tacotron and VITS TTS attacks and CC BY-NC4.0 for hosted data.
- [Official dataset](https://huggingface.co/datasets/CSALT/deepfake_detection_dataset_urdu)
  pinned tree contains `Spoofed_TTS` and `Spoofed_Tacotron`, each with17 published
  speaker folders. Enumerated3396WAV entries, 1698 per family, total1,077,344,612bytes.
  Every candidate exposes a published LFS SHA256 and byte count.
- Use a small deterministic sample:2 per family/folder,68 originals total. This
  is separate noncommercial detector smoke-test material, not AVA training data.
- Exact generator checkpoints/versions, prompts, reference recordings and consent
  linkage have not been established. Retain explicit unknowns. Folder IDs alone
  do not prove target identity or a genuine/synthetic pairing.
- No synthetic audio has been downloaded at this verification checkpoint. A
  machine-local catalog is under the external source-metadata directory. The
  metadata script completed catalog writing; console printing of the publisher
  README hit a Windows Unicode encoding error afterwards. Catalog contents were
  obtained independently of that printing failure.

Next: bounded importer, Astra tests, then68-file intake and shared preparation.
