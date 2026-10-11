# AVA recording session script (template)

Template prepared 4 October 2026 for PRD FR-D4. Every bracketed field must be
replaced with approved text before use; approved Urdu and Shahmukhi Punjabi
versions are required alongside English. Do not read an unapproved sheet to a
participant. This script does not supply approval or consent.

## Before recording (about 10 minutes)

1. Confirm the approval reference `[approval reference]`, form versions
   `[form version]`, your own eligibility as operator, and the participant's
   eligibility under the approved rule.
2. Give the participant the approved information sheet in their preferred
   language. Explain the study, payment terms `[approved payment terms or none]`,
   withdrawal contact `[approved contact]` and the stop signal.
3. Walk through the separate choices and obtain signed answers: recording and
   research use; voice cloning; internal access; examiner sharing; listening
   stimulus; off-site processing; future public release (consider or do not
   request). An unanswered box gives no permission. If recording is declined,
   thank them and stop.
4. Issue the coded ID from the shared counter and complete the approved short
   demographic form: age band, gender, home district, dialect, languages spoken,
   phone model, and the capture settings you are about to use. No exact birth
   date, national ID or banking details.
5. Test the capture route with your own voice for five seconds and check the
   file saves as WAV at the native settings. Confirm nobody else is in range of
   the microphone.

## Part A: read speech (about 3 minutes)

Hand the participant the approved sentence list. Ask them to read at a natural
pace and to skip any sentence they prefer not to read.

- 10 to 15 sentences in Shahmukhi Punjabi: `[approved Punjabi sentence set, version]`
- 10 to 15 sentences in Urdu: `[approved Urdu sentence set, version]`
- At least a quarter of the sentences contain embedded English words (FR-D5),
  for example everyday terms such as phone, bank, card, online, meeting, class.

Record Part A as one file.

## Part B: free speech (about 5 minutes)

Ask two or three of the approved open prompts and let the participant talk.
Do not correct their language choice; natural mixing of Punjabi, Urdu and
English is wanted. Suggested prompt topics, to be replaced by the approved set:

- Tell me about your mobile phone plan and how you use your phone in a day.
- Describe how you usually travel to the university or to work.
- Describe what is in this picture `[approved picture]`.
- What do you cook or eat on a normal weekday?

If the participant drifts toward private details about themselves or others,
gently steer back to the topic. Record Part B as one file.

## Part C: fictional bank call (about 3 minutes)

Explain: "This is a pretend call. Please invent every detail. Do not say a
real name, account number, card number, PIN, password, one-time code, address
or phone number." The operator plays the bank agent using the approved agent
script `[approved agent script, version]`. Scenario: the participant's card was
blocked while paying for something and they want it unblocked. If any real
detail is spoken, stop, note the timestamp, and follow the approved deletion
and re-record step. Record Part C as one file.

## After recording (about 3 minutes)

1. Save the three files as `Pnnn_A.wav`, `Pnnn_B.wav`, `Pnnn_C.wav` and the form
   as `Pnnn_form.json` in the recorder's working folder. No names anywhere.
2. Compute SHA-256 for each file and transfer originals plus hashes to the
   approved storage and backup; verify the hashes after transfer.
3. Add the intake rows and permissions-inventory entries per
   `collection-plan.md` §5. Clear the recorder copies under the approved policy.
4. Give the participant their copy of the information sheet with the coded ID
   and the withdrawal contact. Thank them.

## Do not

- Record before the signed choices are complete.
- Use WhatsApp, voice notes, or any cloud recorder.
- Promise that a voice can be made anonymous or erased from a trained model.
- Ask for a voice sample as proof of identity during a later withdrawal.
- Put any recording, form or coded-ID link into the Git repository.
