# AVA Literature Review (2024–2026)

Compiled 2026-09-26 for issue #23. Every source was checked on its live page (arXiv, ACL Anthology, ISCA Archive, IEEE, GitHub, Hugging Face, official sites). Anything marked **UNVERIFIED** still needs a human to check it before it goes into a slide or report.

This replaces the literature in the scope document and the April briefing, which are about a year old. Section 0 lists what the findings change. The later sections give the evidence.

---

## 0. What changes for AVA

| Topic | Current plan (PRD / architecture) | Proposed | Why (section) |
|---|---|---|---|
| Classification head | DuaBiMamba | **SLS head** (learned layer weighting + small classifier). Nes2Net as the one ablation. | Heads differ by under 1% EER. Mamba has no usable ONNX/CPU path. (§1, §2) |
| Front-end | XLS-R 300M, fine-tuned | **AntiDeepfake MMS-300M** (already post-trained for deepfake detection), with plain XLS-R 300M as a control | Post-trained checkpoint, 100+ languages, public weights. (§1) |
| Layers kept | 12 | **Probe 6–12, expect 7–8** | 12 layers likely misses the 8-call CPU budget. 7 layers is measured and works. (§2) |
| Baselines | AASIST, RawNet2 | Add **XLS-R + AASIST + RawBoost** (Tak 2022, public code) | This is the SSL baseline every recent paper compares against. (§1, §3) |
| Generator stack | ElevenLabs, XTTSv2, RVC, MMS-TTS | **OmniVoice, MMS-TTS, Seed-VC (+RVC), Fish Audio S2 Pro (held out)**. ElevenLabs only with written permission. | XTTSv2 has no Urdu or Punjabi. ElevenLabs' terms forbid dataset/testing use. (§6) |
| Punjabi fakes | TTS | **Voice conversion of real Punjabi recordings** as the main source | Almost no TTS supports Pakistani (Shahmukhi) Punjabi. VC keeps the speaker's accent. (§6) |
| Dataset size | ≥20 h genuine + ≥20 h fake per language | **~50k clips of 3–6 s (~50–70 h), ~40% genuine / 60% fake** | Realistic, and matches the professor's 50k. (§5) |
| Metrics | EER, min t-DCF | Add **actDCF / Cllr**, **HTER at a fixed threshold**, **false-alarm rate per dialect** | EER can hide deployment failure. (§3, §5) |
| Regulations | "AVA complies with CTDISR-2025 / PDPB 2023" | CTDISR-2025 binds **telecom licensees**. The PDPB is an **unenacted draft**. | Wording a panel can pick apart. (§7) |
| Ethics | Consent forms | **Written ethics approval or waiver from GIKI**, plus a consent form with separate opt-ins | Required even if the dataset is never published. (§7) |

---

## 1. Detection models

**The key finding:** the front-end (the big pretrained speech model) and in-language training data drive accuracy. The classification head on top barely matters.

- **Tak et al., "Automatic speaker verification spoofing and deepfake detection using wav2vec 2.0 and data augmentation"**, Odyssey 2022. [arXiv 2202.12233](https://arxiv.org/abs/2202.12233) · [code](https://github.com/TakHemlata/SSL_Anti-spoofing)
  - XLS-R 300M + AASIST + RawBoost: EER 0.82% on ASVspoof 2021 LA (baseline 11.47%) and 2.85% on DF (baseline 21.06%).
  - This is the template later work builds on. **Use as the SSL baseline.**
- **Ge, Wang, Liu, Yamagishi, "Post-training for Deepfake Speech Detection" (AntiDeepfake)**, ASRU 2025. [arXiv 2506.21090](https://arxiv.org/abs/2506.21090) · [code](https://github.com/nii-yamagishilab/AntiDeepfake) · [weights, e.g. MMS-1B](https://huggingface.co/nii-yamagishilab/mms-1b-anti-deepfake)
  - SSL models post-trained on 56k h genuine + 18k h artifact speech in 100+ languages.
  - Zero-shot In-the-Wild EER: 1.64% (XLS-R-2B).
  - Checkpoints include MMS-300M. Weight license is CC BY-NC-SA 4.0 or CC BY 4.0 (repo and paper differ); both allow academic use.
  - **Recommended starting point:** fine-tune MMS-300M on AVA data.
- **El Kheir et al., "Comprehensive Layer-wise Analysis of SSL Models for Audio Deepfake Detection"**, NAACL Findings 2025. [arXiv 2502.03559](https://arxiv.org/abs/2502.03559) · [code](https://github.com/Yaselley/SSL_Layerwise_Deepfake)
  - The lower 10–12 layers of large SSL models match the full model at about half the compute.
  - Wav2Vec2-Large on DF21: 4.14% EER with 24 layers, 4.20% with 12, 5.85% with 8.
- **Zhang et al., "Audio Deepfake Detection with Self-Supervised XLS-R and SLS Classifier"**, ACM MM 2024. [paper](https://dl.acm.org/doi/10.1145/3664647.3681345) · [code](https://github.com/QiShanZhang/SLSforASVspoof-2021-DF) · [weights](https://huggingface.co/SpeechAntiSpoofingBenchmarks/XLSR-SLS)
  - Learned weighting of XLS-R layers plus a small classifier. In-the-Wild EER 7.46%.
  - **Proposed default head.**
- **Xiao & Das, "XLSR-Mamba: A Dual-Column Bidirectional State Space Model for Spoofing Attack Detection"**, IEEE SPL 2025. [arXiv 2411.10027](https://arxiv.org/abs/2411.10027) · [code](https://github.com/swagshaw/XLSR-Mamba)
  - This is the source of the "DuaBiMamba" idea in the PRD.
  - EER: 0.93% (21LA), 1.88% (21DF), 6.71% (In-the-Wild).
  - Only 0.75 points better than SLS on In-the-Wild. The code needs CUDA-only `mamba-ssm`.
- **Liu et al., "Nes2Net: A Lightweight Nested Architecture for Foundation Model Driven Speech Anti-spoofing"**, IEEE T-IFS 2025. [arXiv 2504.05657](https://arxiv.org/abs/2504.05657) · [code](https://github.com/Liu-Tianchi/Nes2Net_ASVspoof_ITW)
  - 511k-parameter head: 1.65% EER on 21DF and 5.15% on In-the-Wild. **Ablation candidate.**
- **El Kheir et al., "DeepFense"**, arXiv 2026. [arXiv 2604.08450](https://arxiv.org/abs/2604.08450)
  - Over 400 trained models. AASIST, MLP, Nes2Net and TCM heads differ by only 0.8% macro EER: "the quality of the input representation dominates over the classification head."
  - The claimed checkpoint link was anonymized in the version read (UNVERIFIED).
- **Dowerah et al., "Speech DF Arena"**, arXiv 2025. [arXiv 2509.02859](https://arxiv.org/abs/2509.02859) · [leaderboard](https://huggingface.co/spaces/Speech-Arena-2025/Speech-DF-Arena)
  - Average EER across 14 datasets: XLSR+SLS 13.84% (best open-source), XLSR-Mamba 14.21%, Nes2Net-X 16.11%, W2V2-AASIST 18.02%, AASIST 34.49%.
- **Wang et al., "ASVspoof 5"**, 2024. [arXiv 2408.08739](https://arxiv.org/abs/2408.08739)
  - Best Track 1 result: EER 2.59% (open condition). Top teams used SSL front-ends and ensembles.
- **Xie et al., "AT-ADD Challenge Summary"**, ACM MM 2026. [arXiv 2608.14249](https://arxiv.org/abs/2608.14249)
  - The winner used a w2v-BERT 2.0 (600M) ensemble. It is stronger but twice the size of XLS-R 300M. **Stretch option only.**
- **Audio-LLM detectors** (ALLM4ADD, [arXiv 2505.11079](https://arxiv.org/abs/2505.11079); DFALLM, arXiv 2512.08403): they need a multi-billion-parameter model on a GPU. Mention them as related or future work only.

---

## 2. Running on CPU in real time

**The key finding:** full-depth XLS-R on CPU is not realistic. Truncated to about 7–8 layers, it is. Mamba does not export to ONNX in a usable way.

- **Pascu, Oneata, Cucu, Müller, "Detecting Audio Deepfakes on the Edge: Lightweight SSL-Based Detection in a Browser Plugin"**, arXiv 2026. [arXiv 2606.30780](https://arxiv.org/abs/2606.30780) · [code](https://github.com/OctavianPascu97/Audio-Deepfakes-Browser-Plugin)
  - Frozen XLS-R-300M cut at **layer 7** plus logistic regression. EER: In-the-Wild 6.6%, LA19 0.8%, DF21 4.1%.
  - About 3.4 s per 5 s of audio on one FP32 CPU core; the full model takes about 9 s. Deployed with ONNX Runtime Web.
  - **The closest existing proof of AVA's approach.**
- **Beheshti et al., "Probing-Guided Layer Selection…"**, arXiv 2026. [arXiv 2606.30791](https://arxiv.org/abs/2606.30791)
  - 4 probe-selected XLS-R layers with a 1.34M-parameter head reach 4.94% EER on In-the-Wild.
  - Use it to pick layers cheaply before training.
- **Serrano et al., "Improving Out-of-Domain Audio Deepfake Detection via Layer Selection and Fusion…"**, arXiv 2025. [arXiv 2509.12003](https://arxiv.org/abs/2509.12003)
  - Choosing the single best layer cuts parameters by up to 80%.
- **Saha, Sahidullah, Das, "Exploring Green AI for Audio Deepfake Detection"**, arXiv 2024. [arXiv 2403.14290](https://arxiv.org/abs/2403.14290)
  - Frozen SSL features with a classical classifier (under 1k trainable parameters) reach 0.90% EER on LA19. A trivial head can be enough.
- **Peng et al., "Hybrid Pruning…"**, arXiv 2025. [arXiv 2508.16232](https://arxiv.org/abs/2508.16232)
  - Up to 70% of parameters pruned while holding 3.7% EER on ASVspoof 5. **Fallback** if truncation plus INT8 is still too slow.
- **Hong et al., "StableQuant"**, ICASSP 2025. [arXiv 2504.14915](https://arxiv.org/abs/2504.14915)
  - 8-bit post-training quantization of wav2vec2/HuBERT gives about 2× throughput and a 4× smaller model. Expect about 2× from INT8, not more.
- **ONNX Runtime quantization docs**: [link](https://onnxruntime.ai/docs/performance/model-optimizations/quantization.html)
  - On AVX2/AVX-512 CPUs without VNNI, U8S8 can saturate and lose accuracy. Check the reference box has VNNI, and re-measure EER after quantizing.
- **Mamba ONNX blockers:**
  - [onnxruntime #27796](https://github.com/microsoft/onnxruntime/issues/27796) (Mar 2026): a 9.6M-parameter Mamba model took 1.7 s per 0.1 s of audio on CPU.
  - [state-spaces/mamba #200](https://github.com/state-spaces/mamba/issues/200): the fused scan cannot be exported.
- **Xuan et al., "Fake-Mamba"**, ASRU 2025. [arXiv 2508.09294](https://arxiv.org/abs/2508.09294)
  - EER by clip length: under 3 s 9.75%, 3–4 s 4.45%, 4–5 s 3.60%, over 6 s 2.71%. **Keep 4 s windows.**
  - Its "real-time" claim is measured on a V100 GPU.
- **Shi et al., "Audio Deepfake Detection at the First Greeting: 'Hi!'"**, ICASSP 2026. [arXiv 2601.19573](https://arxiv.org/abs/2601.19573)
  - A 1–2M-parameter MFCC model gets 1.25% EER at 1 s on degraded audio. Possibly a cheap first-stage filter, but generalization is unproven.

**Budget estimate (ours, extrapolated from Pascu 2026; measure it in task 0.8):**
- Per 4 s window: about 2.7 s FP32 at 7 layers, about 4.5 s at 12 layers. INT8 at about 2× brings that to roughly 1.4 s and 2.3 s per core.
- 8 calls with a 2 s hop means 4 windows per second on 8 cores, so each window gets at most about 2 core-seconds.
- So 7–8 layers fit and 12 probably don't. If load is too high, lengthen the hop to 3 s rather than shortening the window.

---

## 3. Phone channels, codecs, and unseen generators

**The key finding:** telephone channels are the main reason detectors fail in practice. GSM and AMR-NB are the hardest codecs, and they are exactly Pakistan's mobile codecs. Better data matters more than bigger models.

- **Delgado et al. (Microsoft/Nuance), "On Deepfake Voice Detection – It's All in the Presentation"**, ICASSP 2026. [arXiv 2509.26471](https://arxiv.org/abs/2509.26471)
  - Training with realistic phone-channel presentation improved accuracy by 39% in the lab and 57% on a real-call benchmark (2,263 segments, 80 participants).
  - **Core motivation citation for AVA.**
- **Liu et al., "ASVspoof 2021: Towards Spoofed and Deepfake Speech Detection in the Wild"**, TASLP 2023. [arXiv 2210.02437](https://arxiv.org/abs/2210.02437)
  - The LA track routed calls through a real Asterisk PBX with a-law, µ-law, GSM-FR, G.722, Opus, and a real mobile-to-PSTN route.
  - Narrowband conditions were worst for every top-10 system; GSM and PSTN were the hardest.
  - It validates AVA's Asterisk lab and codec list.
- **Wang et al., "ASVspoof 5: Evaluation…"**, TASLP 2026. [arXiv 2601.03944](https://arxiv.org/abs/2601.03944) · database [arXiv 2502.08857](https://arxiv.org/abs/2502.08857) · [data](https://doi.org/10.5281/zenodo.14498691)
  - Includes 8 kHz Opus, AMR (4.75–12.2 kbps), Speex, and PSTN conditions. The neural codec (Encodec) was hardest.
  - Many systems were badly calibrated, and logistic-regression calibration fixed it. **Report actDCF and Cllr.**
- **Tak et al., "RawBoost"**, ICASSP 2022. [arXiv 2111.04433](https://arxiv.org/abs/2111.04433) · [code](https://github.com/TakHemlata/RawBoost-antispoofing)
  - Channel and noise augmentation that needs no extra data. 27% relative gain on 21LA. **Use from day one.**
- **Duroselle et al., "Data augmentations for audio deepfake detection for the ASVspoof5 closed condition"**, ASVspoof 2024 Workshop. [PDF](https://www.isca-archive.org/asvspoof_2024/duroselle24_asvspoof.pdf)
  - A light codec mix (AMR-NB, GSM, Opus, and others) helped and improved calibration. An aggressive "everything" mix hurt. **Keep the codec mix moderate.**
- **Wang et al., "Low Pass Filtering and Bandwidth Extension…"**, ISCSLP 2022. [arXiv 2211.06546](https://arxiv.org/abs/2211.06546)
  - Using only the low band improved codec robustness. Supports band-limiting all audio to 8 kHz.
- **CodecFake** (Wu, Tseng, Lee, Interspeech 2024; [paper](https://www.isca-archive.org/interspeech_2024/wu24p_interspeech.html), [code](https://github.com/roger-tseng/CodecFake)), **Codecfake** (Lu et al., Interspeech 2024; [arXiv 2406.08112](https://arxiv.org/abs/2406.08112)), and **CodecFake+** (Chen et al., TASLP 2026; [arXiv 2501.08238](https://arxiv.org/abs/2501.08238))
  - Detectors trained on older vocoder fakes fail on neural-codec / LLM-TTS fakes: 18.9% EER in CodecFake+.
  - Adding a few selected codec-resynthesized samples helps. Pooling all 31 codecs failed (about 50% EER).
- **Müller et al., "Does Audio Deepfake Detection Generalize?"**, Interspeech 2022. [paper](https://www.isca-archive.org/interspeech_2022/muller22_interspeech.html)
  - Introduces In-the-Wild (37.9 h). EER rose by up to 1000% compared with ASVspoof.
- **Müller et al., "Harder or Different?"**, Interspeech 2024. [arXiv 2406.03512](https://arxiv.org/abs/2406.03512)
  - Unseen generators fail because they are *different*, not *harder*. Generator diversity fixes this; model size does not.
- **Zhou & Wang, "When EER Hides Deployment Failure"**, arXiv 2026. [arXiv 2606.21584](https://arxiv.org/abs/2606.21584)
  - A 0.21% in-domain EER turned into 78.7% of real speech rejected when the threshold was carried to new data. **Report HTER at a fixed threshold.**
- **Müller et al., "Replay Attacks Against Audio Deepfake Detection"**, Interspeech 2025. [arXiv 2505.14862](https://arxiv.org/abs/2505.14862)
  - Playing fakes through a speaker and re-recording raised EER from 4.7% to 18.2%. Replay stays out of scope (PRD non-goal), but note it as a limitation.
- **Xue et al., "RTCFake"**, Findings of ACL 2026. [arXiv 2604.23742](https://arxiv.org/abs/2604.23742) · [data](https://huggingface.co/datasets/JunXueTech/RTCFake)
  - About 600 h of fakes re-sent through Zoom-like platforms. Useful as an extra VoIP test set (languages UNVERIFIED).

---

## 4. Datasets

### 4.1 Deepfake datasets

| Dataset | Size | Urdu / Punjabi | License | Link | Use |
|---|---|---|---|---|---|
| **CSALT Urdu Deepfake** (Munir et al., Findings of ACL 2024) | 20,451 genuine + 16,830 fake (Tacotron, VITS); 17 speakers, speaker-disjoint | Urdu | CC BY-NC 4.0 | [paper](https://aclanthology.org/2024.findings-acl.861/) · [HF](https://huggingface.co/datasets/CSALT/deepfake_detection_dataset_urdu) | Extra training data. **Motivation:** AASIST-L scored about 50% EER (chance) on it. Human listeners reached only 0.63 AUC. |
| **IndicSynth** (Sharma et al., ACL 2025, Outstanding Paper) | ~4,000 h synthetic, 12 languages, 989 speakers | Punjabi (Indian) and Urdu | CC BY-NC 4.0 | [paper](https://aclanthology.org/2025.acl-long.1070/) · [HF](https://huggingface.co/datasets/vdivyasharma/IndicSynth) | Ready-made Gurmukhi Punjabi and Urdu fakes. Per-language hours UNVERIFIED. |
| **MLAAD v9** (Müller et al.) | 687 h, 140 TTS models, 51 languages | Urdu 2.1 h; **no Punjabi** | CC BY-NC 4.0 | [arXiv 2401.09512](https://arxiv.org/abs/2401.09512) | Generator diversity. |
| **ASVspoof 5** | 19k genuine / 164k fake (train) | English only | Check LICENSE (sources differ) | [data](https://zenodo.org/records/14498691) | Codec conditions, split design. |
| **In-the-Wild** | 37.9 h (20.7 real / 17.2 fake), 58 speakers | English | CC BY-SA 4.0 | [HF](https://huggingface.co/datasets/mueller91/In-The-Wild) | Standard generalization test. |
| **SpoofCeleb** (Jung et al.) | ~2.5M clips, 1,251 speakers, 23 TTS | Multi | — | [arXiv 2409.17285](https://arxiv.org/abs/2409.17285) | Quality-gate recipe (WhisperX, DNSMOS, UTMOS). |
| **XMAD-Bench** (EACL 2026) | 668.8 h | Multi | CC BY-SA 4.0 | [arXiv 2506.00462](https://arxiv.org/abs/2506.00462) | Shows detectors reaching ~100% in-domain fall to near chance on other domains. |
| **SpeechFake** (ACL 2025) | 3,000+ h, 40 tools, 46 languages | UNVERIFIED | — | [arXiv 2507.21463](https://arxiv.org/abs/2507.21463) | Check for Urdu. |

### 4.2 Genuine Urdu and Punjabi speech

| Corpus | Contents | Pakistani? | License | Link |
|---|---|---|---|---|
| **Common Voice 27.0** | Urdu 295 h (80 h validated, 506 speakers); pa-IN 4.4 h; **Saraiki 6.75 h (60 speakers); Pahari-Potwari 14.1 h (63)**; Hindko 10.5 h | Urdu, Saraiki, Potwari yes | CC0 (via Mozilla Data Collective; do not re-host) | [cv-dataset](https://github.com/common-voice/cv-dataset) |
| **Kathbath / IndicSUPERB** | 83k Punjabi + 49k Urdu train clips | No (Indian, Gurmukhi) | CC0 | [HF](https://huggingface.co/datasets/ai4bharat/Kathbath) |
| **IndicVoices** | 23.7k h, mostly extempore/conversational | No | CC BY 4.0, gated | [arXiv 2403.01926](https://arxiv.org/abs/2403.01926) |
| **FLEURS** | ~12 h/language, read | ur_pk yes; pa_in no | CC BY 4.0 | [arXiv 2205.12446](https://arxiv.org/abs/2205.12446) |
| **UrduSpeech** (NWPU, 2026) | 156 h, 1,000+ speakers; **89.4 h Urdu–English code-switched** | Yes | States CC BY 4.0, but the audio is from YouTube/PTV, so check reuse rights | [arXiv 2605.17846](https://arxiv.org/abs/2605.17846) |
| **LDC2017S14** (South Asian conversational telephone speech) | **Western (Pakistani) Punjabi 38.8 h**, Urdu 22.9 h; real 8 kHz calls | Yes | Paid LDC license; check GIKI membership | [LDC](https://catalog.ldc.upenn.edu/LDC2017S14) |

No open Shahmukhi (Pakistani Punjabi) speech corpus was found. AVA's own recordings fill a real gap.

---

## 5. Building and validating the dataset; accents; code-switching

### What published datasets do
- **Genuine:fake ratio** ranges from about 1.2:1 (CSALT, In-the-Wild) to 1:4–1:8 (ASVspoof 5).
- **Splits:** no speaker in two splits (all of them), and test generators unseen in training (ASVspoof 5, SpoofCeleb, CSALT). XMAD-Bench also keeps the real-audio sources separate.
- **Quality checks:** ASR word error rate on fakes, speaker similarity, DNSMOS/UTMOS quality scores, language ID (IndicSynth), and human listening tests (CSALT: 100 listeners × 30 clips, reporting AUC).
- **Audit of 39 datasets** (Staněk et al., Interspeech 2026, [arXiv 2606.10911](https://arxiv.org/html/2606.10911)): most reuse audiobooks or scraped audio, and 8 of 39 have no license. Self-recorded, consented data is rare, so AVA's approach is better practice.

### Accent and dialect
- **Kwok et al., Interspeech 2025** ([arXiv 2509.09204](https://arxiv.org/abs/2509.09204)): false alarms depend on the *type* of genuine speech, and fast, noisy interview speech is worst. Report the false-alarm rate per genuine-speech type.
- **Marek, Kawa, Syga, "Are audio DeepFake detection models polyglots?"**, SPSC 2025 ([PDF](https://www.isca-archive.org/spsc_2025/marek25_spsc.pdf)): a small amount of target-language data beats a lot of other-language data.
- **Dao et al.**, arXiv 2026 ([arXiv 2606.08669](https://arxiv.org/abs/2606.08669)): 8 h of target-language fine-tuning improves robustness.
- **Kim et al.**, arXiv 2026 ([arXiv 2609.16458](https://arxiv.org/abs/2609.16458)): cross-lingual error grows with language distance.
- **No study measures false alarms on accented Urdu or Punjabi genuine speech.** AVA can claim this as a contribution.

### Shortcuts to avoid
- **Silence:** Müller et al. 2021 ([arXiv 2106.12914](https://arxiv.org/abs/2106.12914)) showed that leading-silence length alone reached 15.1% EER on ASVspoof 2019. Trim and match silence and duration across both classes.
- **Content and language:** Nguyen et al. 2025 ([arXiv 2505.17513](https://arxiv.org/abs/2505.17513)) cut one detector's accuracy from 100% to 32% by changing only the transcript. Code-switching must appear in both classes equally.
- **Channel:** pass genuine and fake audio through the *same* codec chain, or the model learns the channel instead of the fake.

### Recommended plan for AVA
- **Size:** about 50k clips of 3–6 s (about 50–70 h), roughly 40% genuine and 60% fake. Count unique source clips separately from codec-augmented copies.
- **Genuine sources:**
  - Your own consented recordings from about 150–250 speakers, recorded over real phone calls where possible.
  - Common Voice ur, skr and phr.
  - Kathbath ur/pa and FLEURS ur_pk.
  - UrduSpeech code-switched (after a rights check).
  - LDC2017S14 if GIKI can license it.
- **Dialects:**
  - Train on Standard Urdu and **Majhi (Lahore) Punjabi**, the core.
  - Include **Pothwari and Saraiki** as named groups (Common Voice already has seed data).
  - Keep **Jhangvi and Shahpuri as held-out test-only** sets.
  - Doabi and Malwai are mainly Indian dialects, so cover them through Kathbath/IndicSynth rather than by recording.
- **Code-switching:** use prompts and free conversation that mix in English. Label every clip `ur`, `pa`, `ur-en` or `pa-en`, and generate fakes from code-switched text too.
- **Splits:** speaker-disjoint, at least 2 generators held out, plus a held-out-dialect test and a held-out-codec test.
- **Validation for the panel:**
  - Labels are correct by construction.
  - Automatic checks: Whisper/Urdu ASR word error rate on fakes, speaker similarity, a DNSMOS threshold, language ID, duration and silence.
  - A 5% manual listening spot-check.
  - A CSALT-style human listening test.
  - A script that proves the splits are speaker-disjoint.
  - A datasheet.

---

## 6. Voice generators (attack stack)

**Corrections to the current plan:**
- **XTTSv2 does not support Urdu or Punjabi.** It supports 17 languages, including Hindi but not these two. ([model card](https://huggingface.co/coqui/XTTS-v2)) Its license (CPML) is non-commercial and also covers outputs. A maintained fork exists: [idiap/coqui-ai-TTS](https://github.com/idiap/coqui-ai-TTS).
- **ElevenLabs v3 supports Urdu and Punjabi** ([models](https://elevenlabs.io/docs/overview/models)), but its [Prohibited Use Policy](https://elevenlabs.io/use-policy) bans using output "as part of a dataset that may be used for training, fine-tuning, developing, testing… any machine learning", with no research exemption. **You need written permission to use it.**

| Generator | Urdu | Punjabi | Zero-shot clone | Open / API | License | Notes |
|---|---|---|---|---|---|---|
| [OmniVoice](https://github.com/k2-fsa/OmniVoice) (k2-fsa, Mar 2026) | Yes (211 h) | pan 147 h + **pnb (Pakistani) 10 h** | 3–10 s | Open, 0.6B | Code Apache-2.0, weights CC-BY-NC | Best open option |
| [MMS-TTS](https://huggingface.co/facebook/mms-tts-urd-script_arabic) (VITS) | Yes | Gurmukhi only | No | Open | CC-BY-NC | Runs on CPU; classic TTS family |
| [Seed-VC](https://github.com/Plachtaa/seed-vc) | Any language | Any language | 1–30 s ref | Open | GPL-3.0 | Voice conversion; keeps the source accent |
| [RVC](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI) | Any language | Any language | Needs ≥10 min per target | Open | MIT | Second VC option |
| [Fish Audio S2 Pro](https://huggingface.co/fishaudio/s2-pro) | Yes | Yes | 10–30 s | Open weights + API | Research use free | Codec-LM family; 4B, VRAM needs UNVERIFIED |
| [IndicF5](https://huggingface.co/ai4bharat/IndicF5) | No | Indian | Yes | Open | MIT | Extra Gurmukhi Punjabi |
| [Indic Parler-TTS](https://huggingface.co/ai4bharat/indic-parler-tts) | Yes | Unofficial | No | Open | Apache-2.0 | Described voices, not cloning |
| [Uplift AI](https://upliftai.org/) | Yes, Pakistani accent | Listed | UNVERIFIED | API | UNVERIFIED | Pakistani vendor |
| ElevenLabs v3 | Yes | Yes | Yes | API | **Blocked without permission** | What real attackers use |

Not suitable: CosyVoice 3, Qwen3-TTS, Chatterbox (no Urdu/Punjabi; Chatterbox also watermarks its output, which would give the detector a shortcut), Sesame CSM (English only).

**Proposed stack:**
1. OmniVoice (open zero-shot cloning)
2. MMS-TTS (classic TTS)
3. Seed-VC, plus RVC (voice conversion; the main source of Majhi Punjabi fakes)
4. ElevenLabs v3 if permission is granted (commercial)
5. **Fish Audio S2 Pro, held out** as the unseen attack. It is the only codec-LM family, so it is a genuinely different architecture.

**Code-switching support is UNVERIFIED for every tool.** Test OmniVoice, Fish S2 and ElevenLabs with mixed Urdu-English sentences before committing.

---

## 7. Ethics, consent and regulation

### CTDISR-2025
- The name is correct: "Critical Telecom Data and Infrastructure Security Regulations 2025", a PTA notification dated 29 Oct 2025. [PTA PDF](https://www.pta.gov.pk/assets/media/2025-10-29-Critical-Telecom-Data-and-Infrastructure-Security-Regulations-2025.pdf)
- **It applies to PTA licensees** (s.3): telcos and ISPs. It does not bind a student project or an AI vendor.
- The localization clauses:
  - s.83(1): Critical Information Infrastructure data "shall be stored and processed within the territory of Pakistan".
  - s.83(2): cross-border transfer needs PTA's prior written approval.
  - s.65(1): cloud-held data, including backups, stays in Pakistan.
- The gazette S.R.O. number is blank in the PDF, so enforcement status is UNVERIFIED. The safe citation is "PTA notification dated 29 Oct 2025".
- **Say:** "Telecom licensees must keep call data in-country (CTDISR-2025 s.83), so a detector they adopt must run on-premises. AVA does."
- **Don't say:** "AVA must comply with CTDISR-2025."

### Personal Data Protection Bill
- **Not enacted.** The 2023 bill was approved by cabinet but not passed by Parliament ([Chambers 2026 guide](https://practiceguides.chambers.com/practice-guides/data-protection-privacy-2026/pakistan)). A revised 2025 draft is reported, but its text is UNVERIFIED.
- Always call it "the draft Personal Data Protection Bill". The law actually in force is PECA 2016 plus Article 14 of the Constitution.

### Ethics approval
- The [HEC ORIC Policy 2021](https://www.hec.gov.pk/english/services/universities/ORICs/Documents/ORICs%20Policy%202021.pdf), item 7, requires each university to have an Ethical IRB for human-subjects research.
- **No public GIKI ethics committee was found** (UNVERIFIED). Ask GIKI ORIC or the FYP coordinator who approves human-participant FYPs.
- **Approval is needed even if the dataset is never published.** Collecting identifiable voice data is what triggers it, and cloning adds impersonation risk.

### The consent form must include
- Team, supervisor and contact details
- Purpose
- What is recorded, including metadata (age, gender, dialect)
- **A separate opt-in for voice cloning**, naming the tool and saying audio is uploaded to it, and that clones are deleted after generation
- Storage, access and encryption
- Retention period and deletion date
- The right to withdraw, with a deadline, and what happens to fakes already generated
- **Separate tick-boxes:** internal use only / share with examiners or researchers / public release
- Pseudonymous IDs
- Risks
- "Voluntary; no effect on grades", since volunteers are classmates
- Adults only; signature and date

### Norms from published datasets
- ASVspoof 5 and MLAAD are built from public-domain or licensed audiobooks.
- In-the-Wild used celebrity audio without consent.
- MLAAD uses gated, non-commercial access, which is a good model if AVA ever releases data.

---

## 8. Corrections needed in existing documents

| Document | Claim | Correction |
|---|---|---|
| Scope doc §1; PRD §1.1 | "ElevenLabs and Coqui XTTSv2… natively support Urdu and Punjabi" | XTTSv2 supports neither. ElevenLabs does (v3 only). |
| Scope doc ref [10] | CSALT at LUMS, URL `csalt.itu.edu.pk` | CSALT is at LUMS. Use the ACL Anthology paper and `github.com/CSALT-LUMS`. |
| Scope doc, PRD §6.4, architecture §8 | AVA must comply with CTDISR-2025 | CTDISR-2025 binds telecom licensees. Rephrase as motivation (see §7). |
| Scope doc ref [8], PRD | "PDPB 2023" as a requirement | A draft bill, not enacted. |
| PRD D4, ADR-10 | Fixed stack including XTTSv2 and ElevenLabs | See §6 stack. ElevenLabs is conditional on permission. |
| PRD FR-M2, ADR-2, architecture §4.2–4.3 | DuaBiMamba head, exported to ONNX | SLS head. Mamba has no usable ONNX/CPU path. |
| Architecture §4.1 | Keep 12 layers | Layer probe; likely 7–8 for the CPU budget. |
| ADR-2 | "BiGRU within a point or two of Mamba in published comparisons" (uncited) | Replace with DeepFense / Speech DF Arena evidence. |
| Briefing (Apr) | <150 ms, GPU, layers 1–6, 15 months | Superseded by the PRD. Do not quote it. |

---

## 9. Open items to verify
- Whether Urdu and Punjabi are in XLS-R / MMS pre-training data (supports ADR-1's main claim).
- GIKI's ethics body and process.
- Whether GIKI is an LDC member (for LDC2017S14).
- Fish Audio, Google, Azure and Uplift terms on using outputs in ML datasets.
- ElevenLabs research permission (email them).
- Code-switching quality of each generator.
- IndicSynth per-language hours; RTCFake and SpeechFake language lists.
- Whether the reference CPU supports VNNI (for INT8).

---

# Part B: Novelty, product, live calls and Punjabi data (round 2, 2026-09-26)

## 10. Answering "you just fine-tuned a model"
- The ASVspoof 5 organisers write that architectural innovation "may be reaching a bottleneck" and that progress now comes from data design. [arXiv 2601.03944](https://arxiv.org/abs/2601.03944)
- Delgado et al. (ICASSP 2026) find that better data helps detection more than bigger models. [arXiv 2509.26471](https://arxiv.org/abs/2509.26471)
- So AVA's contribution is **the dataset, evaluation under real deployment conditions, the decision layer, and the live system**. The pretrained model is a component, not the contribution.

## 11. Novelty angles (ranked)

| # | Angle | Evidence it is an open gap | Effort | Deliverable |
|---|---|---|---|---|
| 1 | **Calibrated early-decision detection on narrowband calls.** Score as the call grows, alert only when confident, report error at a threshold fixed before testing. | The closest work is a non-peer-reviewed English wideband preprint (Semjonovs 2026, [doi](https://doi.org/10.21203/rs.3.rs-10257178/v1)): 0.8–29% EER across codecs, and a clean-fit calibrator gave 3–3.5× the error on harsh channels. RTCFake ([2604.23742](https://arxiv.org/abs/2604.23742)) has no streaming decisions. Zhou & Wang ([2606.21584](https://arxiv.org/abs/2606.21584)) show fixed thresholds failing badly on new data. | Low–medium | Error vs seconds-to-decision curve, median time-to-alert per codec, HTER and Cllr at a fixed threshold |
| 2 | **Per-dialect false-alarm audit on genuine speech** (Majhi, other Punjabi dialects, Urdu, code-switched) | Kwok et al. ([2509.09204](https://arxiv.org/abs/2509.09204)): error swings with the type of genuine speech. ParlaSpoof-BR ([2607.28770](https://arxiv.org/abs/2607.28770)): inconsistent decisions across a population. IndicSynth is Indian and synthetic-only; CSALT uses only Tacotron and VITS. | Low | False-alarm rate per dialect, and the worst/best group ratio |
| 3 | **Analyst timeline with spliced-fake localisation** | Roy ([2609.10051](https://arxiv.org/abs/2609.10051)): training-free localisation, not tested on phone calls. Firc et al. ([2608.17585](https://arxiv.org/abs/2608.17585)): real inputs are sometimes partly fake, and raw scores mean nothing to users. Šalko et al. ([2608.19959](https://arxiv.org/abs/2608.19959)): listeners missed a single fake sentence 77% of the time. | Medium | Localisation score on codec'd Urdu/Punjabi calls; dashboard timeline |
| 4 | **Human vs AVA vs human+AVA listening study** | Müller & Choong ([2605.26136](https://arxiv.org/abs/2605.26136)): humans 64–69% accurate vs detector 94.5%. Mai et al. ([PLOS ONE 2023](https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0285333)): 73%. CSALT Urdu: human AUC 0.63. Nothing exists for codec'd Urdu/Punjabi calls. | Low | ~30–60 listeners, 40 clips, accuracy and time per decision |
| 5 | **Speaker-aware detection (SASV):** "is this the enrolled customer AND a real human?" | SASV 2022 ([2203.14732](https://arxiv.org/abs/2203.14732)): SASV-EER 23.83% → 0.13%. Multiplying the two probabilities needs no training ([2202.05253](https://arxiv.org/abs/2202.05253)). ECAPA model: [speechbrain/spkrec-ecapa-voxceleb](https://huggingface.co/speechbrain/spkrec-ecapa-voxceleb) (Apache-2.0). No Urdu/Punjabi phone-call results exist. | Low–medium | SASV-EER with and without enrollment on clones of enrolled speakers |

Skip continual learning for new generators (high effort; future work). Before claiming "first Urdu", read "Deepfake Audio Detection in Low-Resource Languages: A Case Study of Urdu" ([IEEE 11355476](https://ieeexplore.ieee.org/document/11355476/), UNVERIFIED). Only claim "first" for the **combination** of Pakistani Punjabi, telephony and dialect-level results.

## 12. Pakistan motivation (updated figures)

| Claim | Figure | Source |
|---|---|---|
| Phone-fraud losses | **Rs 8.32bn lost by 227,757 victims, 2024–2026**; most cases were WhatsApp hacking or calls posing as bank/institution staff (NCCIA) | [Express Tribune, 11 Sep 2026](https://tribune.com.pk/story/2628638/pakistanis-lose-rs832b-to-cyber-fraud) |
| Call centres | 1,000+ PSEB-registered, plus ~500 outside PSEB | [Business Recorder, 7 Jul 2026](https://www.brecorder.com/news/amp/40428933) |
| Call-centre exports | $328M in FY25 (SBP) | [ProPakistani, 19 Aug 2025](https://propakistani.pk/2025/08/19/call-centers-in-pakistan-fetch-over-320-million-export-earnings-in-fy25/) |
| Scam losses | $9.3B, 2.5% of GDP (a **survey estimate**, GASA/Feedzai 2025) | [Profit, 21 Oct 2025](https://profit.pakistantoday.com.pk/2025/10/21/pakistan-loses-over-9-billion-to-financial-scams-annually-2-5-of-gdp-says-global-anti-scam-report/) |
| Voice cloning of Pakistani figures | PTI cloned Imran Khan's voice with ElevenLabs (Dec 2023) | [Dawn](https://www.dawn.com/news/1798919) |
| Regulation trend | Punjab's draft Performers' Digital Identity and AI Protection Act 2026 covers unauthorised voice cloning | [ARY, 12 Jun 2026](https://arynews.tv/ai-voice-face-cloning-of-artists-ban-in-punjab) |

**No documented Pakistani voice-clone fraud case was found.** Frame it this way: phone impersonation is already the main fraud method, and voice cloning makes it scale. Don't claim known cases.

## 13. Industry products
- **Pindrop Pulse:** SaaS; integrates with Amazon Connect, Genesys (SIPREC/premises options), Five9 and others; about 2 s to a liveness score.
- **ValidSoft Voice Verity:** on-prem option, 8 kHz G.711, at least 2 s of speech.
- **Reality Defender:** on-prem on NVIDIA.
- **Resemble Detect:** explanations of which artifacts drove the score; air-gapped option.
- **Modulate Velma** (Mar 2026): segment-level scores.
- **AWS ended Amazon Connect Voice ID** on 20 May 2026 ([docs](https://docs.aws.amazon.com/connect/latest/adminguide/amazonconnect-voiceid-end-of-support.html)). Voice biometrics alone is being abandoned, and deepfake detection is becoming its own layer.
- **No vendor publishes Urdu or Punjabi results.**
- **For AVA:** a rolling score, a first verdict within seconds, segment-level evidence, CPU-only on-prem, and published per-language and per-codec numbers.

## 14. Live-call integration (simplified)
- **Asterisk has no native SIPREC client** ([community thread](https://community.asterisk.org/t/about-a-siprec-implementation/104937)).
- **Use ARI instead.** A `snoopChannel` (passive tap) feeds a mixing bridge, which feeds `externalMedia` ([Channels REST API](https://docs.asterisk.org/Latest_API/API_Documentation/Asterisk_REST_Interface/Channels_REST_API/)) over [AudioSocket](https://docs.asterisk.org/Configuration/Channel-Drivers/AudioSocket/) (TCP) or WebSocket. **Asterisk decodes the codec and Python receives plain 16-bit PCM**, so there is no RTP or codec decoding in Python.
- **Reusable code:** [hkjarral/Asterisk-AI-Voice-Agent](https://github.com/hkjarral/Asterisk-AI-Voice-Agent) (MIT, Python 3.11, ARI + AudioSocket/ExternalMedia/WebSocket). Reuse its transport layer only; it answers calls rather than tapping them.
- **AMR-NB cannot be transcoded live in stock Asterisk.** Evaluate AMR-NB offline via FFmpeg, and demo live calls with G.711/GSM.
- **SIPREC becomes a documented stretch path:** jambonz as the SRS ([guide](https://docs.jambonz.org/guides/features/siprec-server)), with its `listen` verb sending the same PCM over WebSocket to the same Python receiver. Kamailio+rtpengine and FreeSWITCH mod_siprec are young or buggy.
- **Python 3.13 removed `audioop`.** Use `audioop-lts` or PyAV/FFmpeg for offline decoding.

## 15. Genuine Pakistani Punjabi and Urdu data
- **Meta Omnilingual ASR corpus `pnb_Arab`** (Western Punjabi, Shahmukhi, CC-BY-4.0), the best open find: [HF](https://huggingface.co/datasets/facebook/omnilingual-asr-corpus), [paper](https://arxiv.org/pdf/2511.09690). Designed as ~10 speakers × 1 h of spontaneous speech; actual hours and dialect UNVERIFIED. Also has `phr_Arab` (Potwari) and `hno_Arab` (Hindko).
- **Pakistan Multilingual Speech Corpus** (Habib University, 2026, CC-BY-4.0): ~35k clips in 7 languages including Punjabi and Saraiki ([Zenodo](https://zenodo.org/records/19323537)). Punjabi share UNVERIFIED.
- **Common Voice has no Shahmukhi Punjabi.** The pnb request has been blocked since 2022 ([issue](https://github.com/common-voice/common-voice/issues/3734)).
- **LDC2017S14:** Western Punjabi 38.8 h (207 calls), Urdu 22.9 h, 8 kHz ([catalog](https://catalog.ldc.upenn.edu/LDC2017S14)). The fee is UNVERIFIED. It is **free through the [LDC Data Scholarship](https://www.ldc.upenn.edu/language-resources/data/data-scholarships)**: deadline 15 Jan 2027, needs a 2-page proposal and a supervisor letter.
- **ELRA Urdu corpora cost €15,600–18,000.** Not feasible.
- **Crowdsourcing norms:**
  - Kathbath paid INR 500–1000 per recorded hour and scored every clip on accuracy, volume and noise ([2208.11761](https://ar5iv.labs.arxiv.org/html/2208.11761)).
  - Vaani capped each speaker at 15 min and used image and question prompts ([2603.28714](https://arxiv.org/html/2603.28714v1)).
  - One well-connected community promoter raised Pashto Common Voice participation about 108× ([2603.27021](https://arxiv.org/html/2603.27021v1)).
- **Speaker-identity shortcut:** detectors use speaker identity as a cue; misclassified clips show 29–52× higher identity sensitivity ([2607.21820](https://arxiv.org/html/2607.21820v1)). Generate fakes **from the same genuine speakers**, and keep splits speaker-disjoint.
- **Voice conversion cannot add genuine speakers**, because its output is fake by definition. Use it only to vary the fake side.
- **WhatsApp voice notes:** compression only on genuine clips teaches "compressed = real". Record WAV, or pass fakes through the same codec.
- **Proposed numbers** (~50k clips × 4.5 s ≈ 62 h; ~45% genuine):
  - Urdu genuine: ~11k clips from Common Voice (capped at 30 clips per speaker) plus UrduSpeech after a rights check.
  - Punjabi genuine: ~11k clips from Omnilingual pnb plus **your own 60–80 speakers × 10–12 min**. Paying about PKR 1,000–1,500 per session totals roughly PKR 60–120k (an estimate).
  - LDC2017S14 as a separate telephone test set.
