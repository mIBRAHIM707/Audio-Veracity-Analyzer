# AVA title defence deck, as presented (29 September 2026)

Text extracted from `AVA-title-defense.pptx` (12 slides with speaker notes) and the workflow slide of `AVA_Title_Defence_Workflow_v2.pptx`, both downloaded 28 Sep 2026. This is what the panel saw and heard. Questions in the Q&A follow from these claims.

## Slide 1. Title
AVA, Acoustic Veracity Analyzer. Catching AI-cloned voices in live Urdu and Punjabi phone calls. Team Ayesha, Ibrahim, Ilsa, Tughral. Supervisor Prof. Dr. Ghulam Abbas, co-supervisor Dr. Khurram Khan Jadoon. Title defense, Tuesday 29 September 2026.

Notes (Speaker 1, ~15 s): Assalam-o-alaikum. We are Ayesha, Ibrahim, Ilsa and Tughral, supervised by Prof. Dr. Ghulam Abbas and Dr. Khurram Khan Jadoon. Our project is AVA, the Acoustic Veracity Analyzer. In one line: AVA catches AI-cloned voices in live Urdu and Punjabi phone calls.

## Slide 2. The threat: Pakistan trusts the voice on the phone
- Rs 8.32 bn lost to cyber fraud by 227,757 victims in 2024–26, mostly through calls from people posing as bank staff.
- +1,337% rise in deepfake calls into contact centres, 2023 to 2024.
- Cloning turns a bank officer into your customer. About ten seconds of audio is enough. AVA protects the bank's side of that call.
- Sources: NCCIA via Express Tribune, 11 Sep 2026; Pindrop 2025 Voice Intelligence Report (industry data).

Notes (Speaker 1, ~30 s): In Pakistan, the phone is already the main route for fraud: NCCIA reports 8.32 billion rupees lost since 2024, mostly through calls from people posing as bank staff. Today those scammers use their own voices. With voice cloning, ten seconds of audio lets a fraudster sound exactly like a specific customer, and deepfake calls into contact centres rose over 1,300 percent in a year. AVA protects the bank's side of that call.

## Slide 3. The problem: why today's detectors fail here
- 50% error on Urdu fakes: chance. Language: models trained on English mistake our phonetics and code-switching for AI artefacts.
- 8 kHz, GSM and AMR-NB codecs. Phone channel: Pakistan's mobile codecs are the hardest conditions in the benchmarks, and the main reason detectors fail on real calls.
- 0 vendors publishing Urdu or Punjabi results. Deployment: commercial tools are cloud-first, and telecom call data must stay inside Pakistan.
- Sources: Munir et al., Findings of ACL 2024; ASVspoof 2021 and 5; Delgado et al., ICASSP 2026; PTA CTDISR-2025 s.83.

Notes (Speaker 1, ~25 s): Why not use an existing detector? Three reasons. Language: a standard detector scored 50 percent error on Urdu fakes, pure chance. Phone channel: GSM and AMR, our mobile codecs, are the hardest conditions in the benchmarks. Deployment: commercial tools are cloud-first and publish no Urdu or Punjabi results.

## Slide 4. Literature: what exists, and what is missing
Table columns: Urdu, Pakistani Punjabi, Phone audio, Live on-prem CPU.
- ASVspoof 5 (2024): largest benchmark; English only.
- CSALT Urdu deepfake set (ACL 2024): read speech, 17 speakers, older generators.
- IndicSynth (ACL 2025): Indian Urdu and Gurmukhi Punjabi; synthetic only.
- MLAAD v9 (2026): 51 languages; 2 h of Urdu, no Punjabi.
- Newest detectors, XLSR-Mamba and AntiDeepfake: strong, but built for GPUs.
- Commercial tools, Pindrop and ValidSoft: live calls, no Urdu or Punjabi results.
- AVA: built for Pakistani calls. The gap: no Pakistani Punjabi deepfake data, no phone-quality Urdu set, no detector tested on live calls.

Notes (Speaker 2, ~25 s): We reviewed work from 2024 to 2026. ASVspoof 5 is English. The CSALT set from LUMS covers Urdu, but read speech and older generators. IndicSynth is Indian and synthetic only. The newest detectors need GPUs, and vendors publish nothing for our languages. So the gap is clear, and AVA is the row that fills it.

## Slide 5. Problem statement and objectives
"During a live call, Pakistani banks and call centres cannot tell whether an Urdu or Punjabi caller's voice is human or AI-generated. Existing detectors fail on our languages and phone networks, and cannot run locally."
CCP: maps to conflicting requirements, depth of analysis and requirement identification (full mapping on slide 11).
- BO-2 Dataset: ~50k Urdu and Pakistani Punjabi clips, real and AI-generated, in phone quality.
- BO-1 Accuracy: EER ≤ 5% on our phone test set; ≤ 12% on a voice tool it never saw.
- BO-3 Speed: first verdict within ~6 s of speech; 8+ calls at once on one CPU server.
- BO-4 Live calls: tested on live calls through a lab phone system.
- BO-6 Fairness: false-alarm rate measured and reported for every dialect.
- BO-5, 7 Deployment: fully on-premises, no internet; Docker and Kubernetes.
- Targets are provisional until the first baseline run.

Notes (Speaker 2, ~25 s): Our problem: during a live call, Pakistani banks cannot tell whether an Urdu or Punjabi voice is human or AI-generated. Our objectives follow the approved scope: a fifty-thousand-clip dataset, at most 5 percent error, a verdict within six seconds on a CPU, live-call testing, per-dialect fairness, and on-premises deployment. This maps to three CCP characteristics, shown in full at the end.

## Slide 6. The solution: how AVA listens
1. Live call: a customer calls the bank.
2. Passive tap: the phone system sends AVA a copy of the audio.
3. 4-second windows: every 2 s, the latest 4 s of speech.
4. Detector on CPU: pretrained deepfake model, fine-tuned on our data.
5. Early decision: alerts only once it is confident.
6. Analyst dashboard: a live alert and a timeline of suspicious seconds.
Never in the call path: if AVA fails, the call simply carries on. No internet: audio never leaves the building. A human decides: AVA advises; the analyst acts.

Workflow slide (from the Workflow_v2 deck): only a copy of the caller audio enters AVA. Live call path (caller, Asterisk phone system with passive tap, bank agent) stays separate from analysis. AVA analysis: 1 prepare audio (decode, resample 8 to 16 kHz, keep speech only, 4 s windows with 2 s updates); 2 detect on CPU (AntiDeepfake MMS-300M plus SLS classifier, scores each window, output a synthetic score); 3 combine scores (EMA plus hysteresis, do scores stay suspicious?); 4 alert analyst (suspected synthetic speech on the dashboard, a person reviews the call). No: keep checking the next speech window. Passive, on-prem, CPU-first.

Notes (Speaker 3, ~30 s): Here is how AVA listens. The bank's phone system quietly sends AVA a copy of the call; AVA is never in the call path. Every two seconds, the latest four seconds of speech go through our detector, a pretrained deepfake model fine-tuned on our data and compressed for a normal CPU. It alerts only once it is confident, and the analyst sees which seconds looked fake. A human always decides.

Workflow slide notes: the model in the current slides is AntiDeepfake MMS-300M with SLS. This is a proposed workflow, not measured evidence of accuracy, runtime or non-disruption. No alert confirms fraudulent intent, and no-alert does not verify the caller.

## Slide 7. Modules: four modules, four workstreams
- i Dataset pipeline: record Urdu and Punjabi speakers with consent; generate fakes with four voice tools; phone codecs, quality checks, one manifest.
- ii Detection model: pretrained deepfake detector, fine-tuned on our data; trimmed and compressed to run on a CPU; calibrated, tested on unseen voice tools.
- iii Live call pipeline: Asterisk phone lab with softphones; passive tap streams 4-second windows; smoothing and alert logic.
- iv Analyst dashboard: live call board, alerts, file upload; Docker and Kubernetes, on-premises; no internet access; audit log.

Notes (Speaker 3, ~15 s): That gives four modules, one per team member: the dataset pipeline, the detection model, the live call pipeline on our Asterisk lab, and the analyst dashboard with on-premises deployment.

## Slide 8. Dataset: the core contribution
- 50,000 short clips of 3–6 seconds, about 60 hours. Urdu and Pakistani Punjabi. Real speech ~11k per language; AI-generated ~13.5k per language.
- Where it comes from: Common Voice Urdu, Meta's Pakistani Punjabi (Shahmukhi) set, and 60–80 speakers we record ourselves.
- Accents are not a blocker: AVA listens for AI traces, not accent. Every accent appears in both real and fake clips, and false alarms are measured per dialect.
- Ethics built in: signed consent with a separate voice-cloning opt-in, ethics approval, anonymous IDs, no public release by default.
- Fakes are made from the same people we record, so the model learns human vs AI, not whose voice.

Notes (Speaker 3, ~30 s): The dataset is our core contribution: about fifty thousand short clips, split evenly between Urdu and Pakistani Punjabi, real and AI-generated, all in phone quality. Real speech comes from Common Voice, Meta's Pakistani Punjabi set, and sixty to eighty speakers we record with signed consent. Accents are not a blocker: AVA listens for AI traces, not accent, every accent appears on both sides, and we measure false alarms per dialect.

## Slide 9. Contribution: three open questions
- A. How fast can we be sure? Time-to-alert vs accuracy on narrowband GSM and AMR calls. Not yet studied for these codecs.
- B. Does it wrongly accuse real speakers? False alarms per dialect: Majhi, Pothwari, Saraiki, Urdu and code-switched speech. Never measured for Urdu or Punjabi.
- C. Humans vs AVA: Pakistani listeners, AVA, and listeners helped by AVA, judged on the same clips.
- The model is one component. The contribution is the data, the live system and the measurements.

Notes (Speaker 4, ~25 s): What is new? Three questions nobody has answered for our setting. A: how fast can we be sure over GSM and AMR phone audio? B: does it wrongly accuse real speakers of any dialect? C: how do Pakistani listeners compare with AVA? The model is one component; the contribution is the data, the live system and these measurements.

## Slide 10. Scope, deliverables and timeline
- In scope: real vs AI-generated voice; Urdu and Majhi Punjabi, with Pothwari and Saraiki measured; live calls through a lab phone system; on-premises, CPU-only, no internet.
- Out of scope: video, image or text deepfakes; naming which tool made a fake; live production telecom networks; other languages.
- Timeline Sep to May: 0 Setup, 1 Dataset, 2 Model, 3 Live pipeline, 4 Deploy and harden, 5 Evaluate and report.
- Deliverables: dataset and datasheet; trained CPU detector; live-call demo; evaluation report.

Notes (Speaker 4, ~20 s): In scope: real versus AI voice in Urdu and Majhi Punjabi, on live lab calls, fully on-premises. Out of scope: video or text deepfakes and production telecom networks. Six phases take us to May, ending with the dataset, a CPU detector, a live demo and an evaluation report.

## Slide 11. Why AVA is a complex computing problem (all nine CCP attributes)
1. Range of conflicting requirements: accuracy vs real-time speed on a CPU; catching fakes vs never accusing genuine callers; privacy vs keeping evidence.
2. Depth of analysis required: no obvious solution; models, layer cuts, codecs and thresholds must be compared, with no Pakistani Punjabi data to start from.
3. Depth of knowledge required: speech signal processing, telephony codecs, self-supervised deep learning, SIP/RTP, real-time systems, on-prem deployment.
4. Familiarity of issues: narrowband codec artefacts, shortcut learning (silence, speaker, language), passive call tapping.
5. Extent of applicable codes: SIP/RTP telephony standards, PTA CTDISR-2025 data localisation for telcos, the draft data protection bill, research-ethics consent.
6. Stakeholder involvement: fraud analysts, bank IT, compliance officers, callers, regulators and volunteer speakers, often with conflicting needs.
7. Consequences: a missed fake means financial fraud; a false alarm wrongs an honest, often accented, customer; leaked voices enable impersonation.
8. Interdependence: dataset, model, live pipeline and dashboard are tightly coupled; codec choices shape training, and model size sets call capacity.
9. Requirement identification: open-ended; accuracy, latency and dialect-coverage targets were not given; we define, measure and justify them ourselves.

Notes (Speaker 4, ~35 s): Finally, why AVA is a complex computing problem. It meets all nine CCP attributes. Three stand out. Conflicting requirements: accuracy against real-time speed on a CPU, and catching fakes against never accusing an honest caller. Depth of analysis: there is no obvious solution and no Pakistani Punjabi data to start from. Requirement identification: no targets were given; we define and justify them ourselves.

## Slide 12. Thank you, questions
AVA: catching AI-cloned voices in live Urdu and Punjabi phone calls.
