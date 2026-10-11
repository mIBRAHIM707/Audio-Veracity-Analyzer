# AVA title defence: 20 most probable panel questions (Anki mode)

Import the companion file `docs/viva/ava-viva-anki.txt` into Anki via File, Import, choose the file, set the field separator to Tab, map field 1 to Front, field 2 to Back, field 3 to Tags, and tick "Allow HTML in fields".

Sources used, in priority order: research brief (26 Sep 2026, `C:/Users/tughralhussain/Downloads/AVA-research-brief.pdf`), PRD v0.1 and architecture v0.1 (`_bmad-output/planning-artifacts/`). `docs/literature-review.md` and `CLAUDE.md` are not on the `develop` branch and were not read. Where the brief and PRD disagree, the brief's answer is used and the card is marked "team decision pending" (Q1, Q2, Q6, Q12, Q14). Cards are ordered by likelihood.

---

### Q1. What exactly will you have delivered by May 2027?
**Tags:** scope · deliverables · very likely
**Answer (say this):** Four things. One, the first Pakistani phone-call deepfake dataset: about 50,000 Urdu and Punjabi clips, roughly 60 hours, real and AI-made, each also in phone-codec versions, with a datasheet and consent records. Two, a detector that runs on an ordinary CPU (central processing unit) with no internet, tapped into a lab phone system, scoring live calls and raising alerts on an analyst dashboard. Three, an evaluation report: Equal Error Rate (EER, the point where false alarms equal missed fakes) against the AASIST and RawNet2 baselines retrained on our data, plus seconds-to-alert and per-dialect false-alarm results. Four, a listening study of 30 to 60 Pakistani listeners against AVA (Acoustic Veracity Analyzer) on the same clips.
**Source:** brief §3, §4, §6.1; PRD §7 (BO-1 to BO-7)
**Trap:** Do not recite the whole PRD (Kubernetes, Grafana, webhooks, single sign-on). Lead with dataset, live detector, evaluation, human study. The research-question set (A + B headline, C added, D in dashboard, E stretch) is the brief's recommendation; team decision pending.
**Follow-up they may ask:** Which of those is the minimum you would still call a pass?

### Q2. Aren't you just fine-tuning someone else's model? Where is your work?
**Tags:** complex-problem attributes · novelty · very likely
**Answer (say this):** Partly yes, and that is the right approach: nobody trains a large speech model from scratch, and every winning system in recent deepfake challenges started from a pretrained one. The field itself says progress now comes from data, not architecture: the ASVspoof 5 organisers wrote in 2026 that new model designs "may be reaching a bottleneck", and a Microsoft/Nuance ICASSP 2026 paper found that realistic phone-call training audio improved real-call accuracy by 57%. So the model is one component. Our contributions are the dataset (no Pakistani Punjabi deepfake set exists, and no phone-quality Urdu one), a live CPU system on phone calls rather than saved files, two measurements nobody has published for Urdu or Punjabi (seconds-to-alert over phone codecs, false alarms per dialect), and a human-versus-AVA study.
**Source:** brief §1, §3, §4, §12
**Trap:** Do not get defensive or claim a new architecture. Do not say "we designed DuaBiMamba"; the brief drops it. The contribution list (A + B + C) is the brief's recommendation; team decision pending.
**Follow-up they may ask:** What is the single most novel result you expect to publish?

### Q3. How big is this problem in Pakistan? Has a voice-clone fraud actually happened here?
**Tags:** motivation · problem statement · very likely
**Answer (say this):** Phone impersonation is already Pakistan's leading fraud route: the NCCIA (National Cyber Crime Investigation Agency) reports 227,757 victims lost Rs 8.32 billion in 2024 to 2026, mostly WhatsApp hacking and callers pretending to be bank staff. We have not found a documented Pakistani voice-clone case, and we will not claim one. What cloning changes is that a scammer with a 10-second sample stops pretending to be a role and becomes a specific person, including the customer calling the bank to reset a PIN. That is the direction AVA protects: the institution side of the call. Industry data from Pindrop shows deepfake attacks on contact centres rose from one every two days in 2023 to seven per day in 2024, though that is US vendor data.
**Source:** brief §8
**Trap:** Do not say AVA stops the Rs 8.32 billion scams; most are human voices calling citizens, and AVA sits at the bank, so it never hears them. Label Pindrop as industry data and the $9.3 billion / 2.5% of GDP figure as a survey estimate.
**Follow-up they may ask:** If the fraudster uses his own real voice, AVA does nothing. So what is the value?

### Q4. Can a 300-million-parameter model really run in real time on a CPU, with no GPU?
**Tags:** feasibility · CPU real-time · very likely
**Answer (say this):** Yes, if we keep only part of it. A 2026 study ran a 7-layer version of this kind of model on a single CPU core inside a browser plugin. We keep about 7 to 8 of the 24 transformer layers, because earlier work shows deepfake cues sit in the lower layers, and compress the weights to INT8 (8-bit integers), which typically gives a 2 to 3 times speed-up. Our target is one 4-second window scored in under 1.5 seconds on an 8-core, 32 GB server, and 8 simultaneous calls per box, with no GPU (graphics processing unit). We benchmark this at a hard gate in mid-January 2027; if it fails, we fall back to a smaller backbone that is already named in the plan.
**Source:** brief §5, §12, §13 ref 7; PRD §6.1, A1, A3, R1, Phase 2 benchmark gate; ADR-3
**Trap:** Never quote the old briefing: "under 150 ms", "GPU", "layers 1 to 6", "15 months" (wrong claim 6). Do not say 12 layers either; the brief says 12 is too slow for 8 calls on one CPU.
**Follow-up they may ask:** What if the 7-layer model loses too much accuracy?

### Q5. Is there enough Pakistani Punjabi data? Common Voice has none.
**Tags:** feasibility · Punjabi data · very likely
**Answer (say this):** Correct, Common Voice has no Pakistani Punjabi, so we combine three sources. Meta's open Omnilingual pnb_Arab set gives about 3,000 Shahmukhi clips; we record our own 60 to 80 Majhi Punjabi speakers, 10 to 12 minutes each, about 8,000 clips; and we apply for LDC2017S14, 38.8 hours of real Pakistani Punjabi phone calls, free through the LDC (Linguistic Data Consortium) Data Scholarship, deadline 15 January 2027. Research shows a little target-language data beats a lot of other-language data, and about 8 hours of target-language fine-tuning already improves robustness. If the Punjabi partition ends up smaller than Urdu, we report that gap honestly.
**Source:** brief §1, §6.2, §6.3, §7, §12; PRD R3
**Trap:** Do not name "Common Voice pa-PK" as a source; the PRD does, the brief says it has no Pakistani Punjabi. Do not cite csalt.itu.edu.pk; CSALT is at LUMS (wrong claim 5).
**Follow-up they may ask:** What happens if fewer than 60 speakers turn up?

### Q6. How do you test on live phone calls without a real telecom network?
**Tags:** feasibility · telephony testing · very likely
**Answer (say this):** We build a free phone lab: an Asterisk PBX (private branch exchange, a software phone switch) in a container, softphones on each end, and a call generator that plays real and fake recordings into calls. Asterisk's "snoop" feature quietly copies the call audio and hands plain audio to our Python code, which is how a bank's phone system already feeds its call recorders; the tap also decodes the codecs for us. We test G.711 and GSM live in the lab, and AMR-NB (Adaptive Multi-Rate Narrowband) on recorded files, because stock Asterisk cannot decode AMR-NB. SIPREC (the standard SIP Recording protocol, RFC 7865/7866) via jambonz is a stretch goal, not the base plan.
**Source:** brief §1, §5, §12; PRD FR-T1, FR-T3, §2.2; ADR-4
**Trap:** Do not claim integration with PTCL or any live network; that is an explicit non-goal. The PRD says "our own SIPREC server"; the brief changes this to the Asterisk tap. Team decision pending; use the brief's answer.
**Follow-up they may ask:** How is a lab call different from a real network call, and does that hurt your results?

### Q7. You are recording people and cloning their voices. What about consent and ethics?
**Tags:** ethics · consent · very likely
**Answer (say this):** Every speaker signs a consent form before recording, with a separate tick-box for cloning their voice, and separate tick-boxes for internal use, sharing with examiners, and public release; it states "voluntary, no effect on grades". Speakers get an anonymous ID such as P001; real names never enter the dataset, and consent forms are stored offline. Anyone can withdraw, and then we delete their clips and every fake made from them. We request ethics approval through GIKI ORIC (Office of Research, Innovation and Commercialization) in early October, because HEC (Higher Education Commission) requires review of human-subjects research even if we never publish. Most published deepfake datasets scraped audiobooks or celebrity audio without consent; ours is consented.
**Source:** brief §6.3, §6.6, §6.9, §10
**Trap:** Do not say "we don't need ethics approval, it's a student project". Do not say the dataset will be public; public release is off by default and needs both a team decision and matching consent.
**Follow-up they may ask:** Who at GIKI approves it, and what if approval arrives late?

### Q8. Four students, nine months, a dataset, a model, a phone lab and a dashboard. Is that realistic?
**Tags:** feasibility · timeline · very likely
**Answer (say this):** Yes, because the phases overlap and each has an exit gate. October to November is data: ethics request, public data, our recording sessions, fakes generated as recordings arrive, ava-data-v1 by end of November. Model work starts in October on ava-data-v0 (public data only) so it is not blocked, and runs to mid-February with the CPU benchmark gate in mid-January. The phone lab and dashboard run January to March in parallel, deployment and hardening March to mid-April, final evaluation and report April to May. Four workstreams, one per member: data, model, telephony, app. Batch file analysis is in scope so the model can be demonstrated even if the phone lab slips.
**Source:** brief §6.9, §11; PRD §8 (phases 0 to 5), D2, R8
**Trap:** Do not say "15 months" (wrong claim 6). Do not promise the P1/P2 extras (history views, webhooks, single sign-on); they are cut first if time runs short.
**Follow-up they may ask:** What gets cut first if you fall behind?

### Q9. Why is this a Complex Computing Problem and not just a software build?
**Tags:** complex-problem attributes · CCP · very likely
**Answer (say this):** It has conflicting requirements with no textbook answer: detection accuracy against CPU-only real-time speed against zero internet, on 8 kHz phone audio that destroys the high-frequency cues most detectors rely on. It needs depth of analysis, because we must measure trade-offs nobody has published for Urdu or Punjabi: seconds-to-alert versus accuracy, and false alarms per dialect. It involves unfamiliar issues: no Pakistani Punjabi deepfake data exists, so we must build it under consent and ethics rules. It applies standards: phone codecs (G.711, GSM 06.10, AMR-NB), the SIPREC recording protocol, and ASVspoof evaluation protocols. And the parts are interdependent, dataset feeds model feeds streaming pipeline feeds dashboard, with stakeholders from fraud analysts to compliance officers to the speakers we record.
**Source:** brief §3, §4, §7, §12 (speaker 4); PRD §1.1, §3, §6.4, §8
**Trap:** Do not recite the CCP (Complex Computing Problem) attribute list without tying each to something concrete in AVA. Do not say "it is complex because it uses AI".
**Follow-up they may ask:** Which of those attributes is the hardest, and how do you know?

### Q10. Deepfake detectors already exist. What is new here?
**Tags:** literature review · novelty · likely
**Answer (say this):** Three gaps. The only published Urdu deepfake detection work (Munir et al., Findings of ACL 2024, from CSALT at LUMS) reports detection at about 50% error, which is chance. No Pakistani Punjabi deepfake dataset exists at all, and no phone-quality Urdu one. Commercial products such as Pindrop, ValidSoft and Resemble publish no Urdu or Punjabi results. Almost all research tests saved files; ours runs on live calls on a CPU. And nobody has measured seconds-to-alert over GSM and AMR codecs or per-dialect false alarms for these languages; only one unreviewed English paper tried seconds-to-alert at all.
**Source:** brief §1, §3, §4 (A, B), §8, §12 (speaker 2), §13 ref 6
**Trap:** Do not cite csalt.itu.edu.pk (wrong claim 5); cite the ACL 2024 paper and github.com/CSALT-LUMS. Do not claim a new model architecture as the novelty.
**Follow-up they may ask:** What happens if the ACL 2024 Urdu model, retrained on your data, does just as well?

### Q11. (Trap) Your scope document says AVA complies with CTDISR-2025 and PDPB 2023. How does a student project comply with telecom regulations?
**Tags:** ethics · regulation · likely
**Answer (say this):** It does not, and that wording is being corrected. CTDISR-2025 (the PTA's Critical Telecom Data and Infrastructure Security Regulations, notified 29 October 2025) binds telecom companies, not us. We use it as motivation: telecoms must keep call data inside Pakistan, so a detector that sends audio to a foreign cloud cannot be deployed here, and AVA runs fully on-site with no internet. PDPB 2023 is an unpassed draft, so we call it the draft Personal Data Protection Bill and design to its principles: no raw audio stored by default, only scores and metadata. We prove the no-internet claim with a test that runs the system with all outbound traffic blocked.
**Source:** brief §8, §9 (claims 3 and 4); PRD §6.4, FR-O4, FR-O6
**Trap:** Do not say "AVA is CTDISR-compliant" or "PDPB 2023 requires" (wrong claims 3 and 4). PRD §6.4 and G5 still carry the old wording.
**Follow-up they may ask:** If CTDISR does not bind you, why mention it at all?

### Q12. (Trap) You wrote that Coqui XTTSv2 supports Urdu and Punjabi, and that you will use ElevenLabs to make fakes. Is that right?
**Tags:** scope · generators · likely
**Answer (say this):** No on both, and the list has been corrected. XTTSv2 supports neither Urdu nor Punjabi, so it is removed. ElevenLabs' terms ban using its output to train or test detectors, so we use it only if they grant written permission, which we are requesting. The current plan is four free tools: OmniVoice (2026) for voice cloning, which supports Urdu and Pakistani Punjabi; Seed-VC with RVC for voice conversion, our main source of Punjabi fakes because it keeps the speaker's accent; MMS-TTS for basic Urdu text-to-speech; and Fish Audio S2, held out of training entirely as the unseen-tool test.
**Source:** brief §5 (fake-voice generators), §6.4, §9 (claims 1 and 2), §11
**Trap:** PRD D4 and ADR-10 still list ElevenLabs, XTTSv2, RVC, MMS-TTS. Use the brief's list. Team decision pending until PRD/SRS are updated.
**Follow-up they may ask:** How do you know these tools produce convincing Punjabi?

### Q13. Punjabi has many accents. Won't the model flag an unfamiliar accent as fake?
**Tags:** engineering knowledge · dialect false alarms · likely
**Answer (say this):** Accent is not what AVA listens for. The model looks for traces AI generators leave behind (unnatural timing, phase and texture), which are properties of the generator; accent is a property of the human speaker. It can only learn "accent equals fake" if an accent appears on one side only, so every fake is made from the same people we recorded, and voice conversion keeps their original accent, meaning every accent sits in both real and fake. The real risk is a false alarm on an accent we never recorded, so we do not guess: we hold out test-only dialects (Jhangvi, Shahpuri) and report the false-alarm rate per dialect. Nobody has published that for Urdu or Punjabi, so the measurement itself is a contribution.
**Source:** brief §2, §4 (B), §7
**Trap:** Do not claim "we cover all Punjabi accents". Majhi (Lahore region) is the agreed scope; Pothwari and Saraiki come cheaply from open data; the rest is future work.
**Follow-up they may ask:** What false-alarm rate per dialect would you consider acceptable?

### Q14. Which model will you use, and why that one?
**Tags:** engineering knowledge · model choice · likely
**Answer (say this):** The proposed base is AntiDeepfake MMS-300M, a free model already trained to catch deepfakes on 74,000 hours of audio in over 100 languages, so we start from a detector rather than a general speech model. We cut it to about 7 to 8 layers for CPU speed, chosen by a quick layer test, and put a simple classifier on top, the SLS head from Zhang et al. (ACM MM 2024), which the brief describes as a simple layer mix plus a small classifier. We compare against the standard public baseline, XLS-R plus AASIST with RawBoost augmentation. A multilingual base matters because a model trained only on English can mistake normal Urdu sounds for fakery.
**Source:** brief §1, §5, §13 refs 2 and 3; PRD A3, FR-M1; ADR-1
**Trap:** The PRD says XLS-R 300M with a DuaBiMamba head; the brief replaces both (Mamba is only about 1 point better and cannot run on CPU). Team decision pending. Do not say "we designed the model".
**Follow-up they may ask:** What if AntiDeepfake was never trained on phone-quality audio?

### Q15. Why a 4-second window? A fraudster can say "reset my PIN" in less than that.
**Tags:** engineering knowledge · windowing · likely
**Answer (say this):** Because shorter windows roughly double the error rate, and 4 seconds is what these detectors are trained and benchmarked on. We score the last 4 seconds every 2 seconds, so the score updates continuously, and we wait for two windows, about 6 seconds of speech, before the first verdict, which avoids false alarms on "hello, how are you". Fraud is not one sentence; the caller has to get through identity checks, so a first verdict within 6 seconds and updates every 2 seconds is in time. How fast we can be confident is research question A: we plot seconds-to-alert against accuracy over GSM and AMR phone audio, and the dashboard highlights which seconds of a call were fake, because listeners miss a single fake sentence 77% of the time.
**Source:** brief §2, §4 (A, D), §5; PRD FR-I4 to FR-I6, §4.1; ADR-11
**Trap:** Do not say "under 150 ms" (wrong claim 6). Do not claim we alert on the first word.
**Follow-up they may ask:** What does the alert look like, and can it flip on and off?

### Q16. How do you make sure the model is not just memorising voices?
**Tags:** engineering knowledge · speaker-disjoint splits · likely
**Answer (say this):** Speaker-disjoint splits: 70% train, 10% dev, 20% test, split by person, so no speaker appears in two splits, and a script checks that before every run. Otherwise, with few speakers, the model can pass by learning "this voice is always fake", and the number collapses on new callers, exactly the people the product faces. The same logic is why every consenting speaker appears on both the real and the fake side: the model cannot use "whose voice is this" as a shortcut. It is also why we match silence and length across real and fake clips, since one published model cheated by measuring silence, and why we never use WhatsApp voice notes for real clips only, or the model learns "compressed equals real".
**Source:** brief §6.4, §6.6, §6.8; PRD A4, FR-D7, §4.1
**Trap:** Do not say "we use a random split".
**Follow-up they may ask:** How many speakers end up in the test set?

### Q17. Fraudsters will use tools you never trained on. What then?
**Tags:** engineering knowledge · held-out generator · likely
**Answer (say this):** We test for exactly that. Fish Audio S2, the newest LLM-style text-to-speech, is held out of training entirely and used only in the final test, about 3,000 clips, so we report an unseen-tool number separately. We also hold out test-only dialects and the LDC real phone calls. The PRD sets a looser provisional target for the unseen tool, EER of 12% or under, against 5% on the full test set, because a detector tested only on tools it saw learns generator fingerprints, not synthesis in general. That unseen-tool number is the most honest generalisation measure we can give.
**Source:** brief §5, §6.1, §6.4, §6.6; PRD FR-D8, §6.2, R6; ADR-10
**Trap:** Do not promise it generalises to every future tool. Targets are provisional until the first baseline run.
**Follow-up they may ask:** What do you do if the unseen-tool EER is far worse than 12%?

### Q18. How will you measure success, and against what?
**Tags:** scope · evaluation · likely
**Answer (say this):** The main metric is Equal Error Rate on a held-out phone-quality Urdu and Punjabi test set, compared with two standard baselines, AASIST and RawNet2, retrained on our own data, because English benchmark numbers do not transfer. We report EER per language, per codec, on the unseen tool, and on mixed-language clips. Provisional targets from the PRD: 5% EER overall, 12% or under on the unseen tool, an Urdu-Punjabi gap of 2 points or less, and 3% or fewer real calls flagged in the lab. On the system side: under 1.5 seconds per window, first verdict within 6 seconds, 8 concurrent calls on an 8-core box. Plus the human study: people score 64 to 73% in published studies; we test whether AVA, and humans helped by AVA, beat that.
**Source:** PRD §6.1, §6.2, FR-M4, FR-M5, §4.1; brief §4 (C), §5 (baseline)
**Trap:** Targets are provisional (PRD §6.2 says so); do not present them as promises. Do not quote published English AASIST numbers as our comparison.
**Follow-up they may ask:** What happens if you miss 5% EER?

### Q19. What happens when AVA flags a real customer as fake?
**Tags:** ethics · false alarms · likely
**Answer (say this):** Nothing happens to the call. AVA is passive and advisory: it listens to a copy of the audio, never sits in the call path, and never blocks or ends a call; a human analyst sees the alert and decides. We smooth the scores with a moving average and use separate on and off thresholds, for example enter "alert" at 0.75 and leave it at 0.55, so one noisy 4-second window cannot flip an alarm on and off. Thresholds are configurable because the right trade-off between a missed fraud and an annoyed customer differs between a bank and a call centre. And we measure the false-alarm rate per dialect, so an operator knows where the model is weakest.
**Source:** PRD §2.2 (non-goals), FR-T5, FR-I7, §4.1; architecture §2.4, ADR-11; brief §4 (B), §12
**Trap:** Do not say "AVA is accurate enough that this won't happen". Concede that false alarms will occur; the design assumes them.
**Follow-up they may ask:** Who is accountable if the analyst acts on a false alarm?

### Q20. Your GitHub repository is public. Where do the recordings and consent forms live?
**Tags:** ethics · data handling · possible
**Answer (say this):** Never in GitHub. Audio lives on the university GPU server or an encrypted drive with one backup, and the repository's .gitignore already blocks audio files. Consent forms are stored offline and separately, linked to recordings only by the anonymous speaker ID. The repo holds scripts and the manifest, a CSV with one row per clip (speaker ID, language, dialect, real or fake, generator, codec, split, licence), so anyone on the team can rebuild the dataset from scratch. Only synthetic audio and team-consented recordings may leave the premises for training on a rented GPU; licensed third-party corpora stay on team-controlled machines.
**Source:** brief §6.6, §10; PRD FR-D10; architecture §5 (compute)
**Trap:** Do not say "the dataset is on GitHub" or "we will open-source the data". Public release is a separate decision that needs matching consent.
**Follow-up they may ask:** How will you decide whether to publish the dataset or a paper?
