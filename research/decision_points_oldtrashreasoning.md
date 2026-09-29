# Iteration-2: What to Decide Before Coding

> **Status:** discussion document, 2026-09-26. It evaluates the team's initial ideas against the research in [initial_research.md](initial_research.md) and the team's own summary (`A2/research/Boxing Research Critical Information.docx` + local PDFs). Nothing here is final.
>
> **Structure:**
> - §1 lists the decisions in the order they block each other.
> - §2 evaluates each of the current ideas.
> - §3 lists what to borrow from which paper.
> - §4 covers problems not raised yet.
> - §5 is a strawman pipeline to argue about.
> - §6 has corrections to the team docx.

---

## 1. Decision order (what blocks what)

Almost every coding task depends on one of these. Roughly, each one constrains the ones below it.

| # | Decision | Why it blocks things | Urgency |
|---|---|---|---|
| D1 | **Application context & class list** (which punches, air/pads/bag, stance(s), intensity range, include a no-punch class) | Decides what data to record and what "acceptable accuracy" means. The rubric's 10% "problem definition" section is written from this | **Now** |
| D2 | **Sensor placement & count** (e.g. wrist + upper arm ×2) | Decides mounting hardware, BLE load, and the model input shape | **Now** |
| D3 | **Node data format**: sample rate, ranges, packet layout (batching, sequence number, timestamp) | Firmware and the Pi receiver both depend on it. Every recording made before this is fixed is wasted | **Now**, needs a hardware spike to confirm |
| D4 | **Collection & labelling protocol** (cued reps? continuous stream? how punches get cut out) | Decides the collection script and how many sessions/participants we need | Before any real data |
| D5 | **Storage path** (local-first vs straight to Atlas, document schema) | Decides the Pi code structure. The Atlas free tier has a size limit (§2.4) | Before real data |
| D6 | **Evaluation plan** (leave-one-subject-out, what ablations) | Decides how many participants and sessions are *needed* | Before the collection schedule |
| D7 | Model families to compare, feature sets, real-time detection logic | Can be explored *after* data exists, as long as raw data was stored | Later |

**The most important rule:** *record raw data from all 4 sensors, always.* You can drop sensors, change windows or recompute features offline, but you can never add information you didn't record. With raw data stored, D7 and parts of D2 (the ablation) become offline experiments rather than up-front bets.

---

## 2. Evaluation of the current ideas

### 2.1 Four sensors: 2 wrists + 2 upper arms (just above the elbow)

**Verdict: doable, and more defensible than it first looks. It needs an ablation to justify it, because no paper has used this placement for punches.**

Why it's a good idea (your reasoning, plus some physics the research supports):

- **The upper-arm sensor mostly measures *orientation*, and orientation doesn't depend on speed.**
  - The accelerometer always feels gravity (~1 g). Where that 1 g appears on the board's axes tells you roughly which way the humerus points.
  - **Hook**: elbow lifted, upper arm roughly horizontal and out to the side.
  - **Uppercut**: elbow low, upper arm pointing down.
  - **Jab/cross**: upper arm swings forward and the elbow straightens.
  - That's exactly the hook-vs-uppercut distinction the literature finds hardest (Hykso merges them; RD α's weakest class was hook, F1 0.82). A slow and a fast hook still have the elbow at the same height. That's a strong answer to your slow-punch worry (§2.6).
- **Wrist vs upper-arm difference approximates elbow motion.** Straight punches extend the elbow; hooks keep it roughly locked. The wrist sensor alone can't separate "the arm straightened" from "the whole arm swung".
- **Novelty with a built-in experiment.** Train with {wrist}, {upper arm}, {wrist + upper arm} and compare. That is exactly the "sensor placement strongly justified" / "design trade-offs critically analysed" language in the HD column, and it costs nothing extra if we always record all 4.

Costs and risks:

- **No torso sensor.** BoxerSense and Worsey argue that trunk rotation carries punch information. Worsey found no significant gain *on top of* wrists, but that study had only 1 participant (§6). Stance detection and spinning moves (if in scope) would benefit from a torso sensor.
  - Alternative configuration to keep in mind: 2 wrists + 1 upper back + 1 spare.
  - Because we record everything raw, we could even do one pilot session with the 4th board on the back and compare.
- **Mounting on the upper arm is harder than on the wrist.** The biceps and triceps bulge when you punch, which makes straps slide.
  - The flattest, least-muscular spot is the **lateral side of the arm just above the elbow**, over the lower humerus.
  - An elastic Velcro strap (a phone armband style) is the usual method. It must be tight, and the board's orientation must be marked.
- **It doubles the BLE and sync problem per arm.** Two boards on the same arm must be time-aligned to be combined in one window (§2.3).
- **Four batteries and four mounts** to build and keep charged.

### 2.2 Treat each arm independently

**Verdict: practical, and the research supports it:**

- Manoharan trains per hand.
- SensiML uses separate left and right models.
- RD α treats each glove's strikes as separate samples. They add "hand" and "stance" as *features* rather than separate models (see §6).

Benefits:

- The guard hand's position doesn't pollute the punching arm's input (your point).
- **Combos across hands aren't blocked.** Worsey's detector has a 0.5 s "cooldown" after each punch. With per-arm detection, a jab-cross (two different arms, ~0.2–0.3 s apart) is two independent events, so neither cooldown blocks the other. Only same-arm doubles (double jab) still hit the cooldown.
- **Half the input size**, and the arms can be processed independently.
- **Mirroring becomes an option.** If the left-arm data is flipped to look like right-arm data, one "arm model" gets twice the training data (initial_research §4.16). Or keep two role-specific models (lead arm / rear arm). Both are valid, and comparing them is another report experiment.

Consequences to design for:

- **The non-punching arm moves too.** When you throw a cross, shoulder rotation drags the lead hand. That arm's detector may fire.
  - The neat fix: **record the guard arm's motion during the other arm's punches and label it "no-punch" for that arm.** It's free null-class data, and it teaches each arm model "moving but not punching".
  - As a safety net, add a tie-break rule: if both arms fire within ~100 ms, keep the one with higher energy or higher confidence.
- **Lead/rear vs left/right.** For an orthodox stance, left = lead.
  - If we collect orthodox only, that's a scope limitation to state.
  - Supporting southpaw means either recording southpaw sessions, or mirroring, which is imperfect because the hips and footwork differ too.

### 2.3 Clock sync and missing samples

This is simpler than it sounds if we stop trying to sync hardware clocks and instead **make the data self-describing.**

1. **Every packet carries a sequence counter and the board's own timestamp** (`micros()` of the first sample in the batch).
   - The sequence counter shows gaps exactly: packet 41 then 43 means one was lost.
   - The board timestamp gives true sample spacing, which is immune to BLE jitter.
2. **Align each board's clock to the Pi's clock in software.**
   - For every packet, compute `offset = pi_receive_time − board_time`. BLE latency can only *add* delay, never subtract, so the **smallest** offset over the last few seconds is the best estimate of the true offset (the "minimum-delay filter" idea behind NTP).
   - Clocks drift slowly, so re-estimate continuously.
   - Expect roughly ±10 ms alignment. That's plenty for 1 s punch windows at 100 Hz.
3. **Physical sync marker for recorded sessions** (the Earable paper did something similar with a "marker move").
   - At session start, clap both arms or tap all boards together, so all 4 boards record a sharp spike.
   - Offline, cross-correlate the spikes to align boards very precisely.
   - It's cheap, and it's a nice validation of (2) for the report.
4. **Missing samples.**
   - In principle, BLE's link layer retransmits lost packets automatically. Real losses mostly come from (a) the Arduino overwriting the characteristic before the previous value was sent, or (b) disconnects.
   - Batching fixes (a).
   - Policy: interpolate gaps of 1–2 samples, and **discard or flag windows with bigger gaps** (and count them: that's a "system reliability" metric for the evaluation section).
5. **Batching is required anyway.** At ~100 Hz × 4 boards, one sample per notification is ~400 notifications/s, which BlueZ won't sustain reliably (bleak #1858).
   - Proposal: ~5–10 samples per notification.
   - Layout: 6 × int16 = 12 bytes per sample; 10 samples + 2-byte sequence + 4-byte timestamp = 126 bytes.
   - **Unknown to verify:** whether ArduinoBLE on the Nano 33 IoT negotiates an MTU larger than the 23-byte default. If not, only ~20 bytes fit per notification (1 sample + header), and we'd need a lower rate or on-node event detection.
   - **This must be the first hardware experiment.**

### 2.4 Upload to MongoDB immediately, or store locally first?

**Local-first, upload asynchronously. This is the standard IoT edge pattern, and it gives you something good to write in the architecture section.**

- The Pi writes the raw stream to local files (CSV/Parquet per session, or SQLite). This is the source of truth for that session.
- A separate uploader task pushes completed chunks to Atlas, with retry. If the network drops, nothing is lost; it just uploads later.
- **The real-time recognition loop never waits on the cloud.** An Atlas round-trip is tens to hundreds of ms and unpredictable. Classification happens on the Pi; results are logged to the cloud afterwards.
- **Watch the free-tier size.** An Atlas M0 (free) cluster has 512 MB of storage.
  - One document per sample (~100+ bytes of BSON with field names) at 100 Hz × 4 boards is ~1.4 M documents/hour, i.e. very roughly 150 MB/hour before indexes.
  - Use the **bucket pattern** instead: one document per board per second (or per packet batch), holding arrays of samples. Or use a MongoDB time-series collection.
- Suggested collections:
  - `participants`: anonymised ID, height, experience, stance, handedness, consent.
  - `sessions`: who, when, condition, sensor placement, firmware version.
  - `raw_chunks`: session, board, sequence range, timestamps, sample arrays.
  - `cues`: session, time, prompted label.
  - `predictions`: live-demo log.
- This gives a clear answer to the brief's "managing data collected in the cloud" requirement.

### 2.5 Accel and gyro ranges

- **Accelerometer:** the LSM6DS3 hardware max is **±16 g**. It's reachable with the SparkFun library at I²C address 0x6A, or by writing `CTRL1_XL` directly.
- **Gyroscope:** the hardware max is **±2000 °/s**, and **the official library already sets 2000**. There's nothing to unlock.
  - Every boxing study cited used ±2000 °/s successfully.
  - It *might* clip on whippy or spinning techniques. The first hardware test should look for flat-topped gyro peaks too.
- Worsey's view on over-range: clipped peaks matter for *measuring* punch force/speed, but "may not have a drastic effect on classification" because the other channels still carry the shape. So even occasional clipping isn't fatal for our goal. We should still note it as a limitation.
- **Resolution:** encode as milli-g (×1000 in int16 → ±16 g = ±16000) rather than ×8.

### 2.6 The team's proposal: fixed 1 s capture windows + DBA/DTW

**Split it into two separate ideas, because one is clearly good and the other is a design choice.**

**(a) Cued, fixed-rhythm collection ("punch now … rest …"): yes.**

- The label comes *free* from the cue, so no manual labelling is needed. This is the answer to "manual labelling of unequal-length data is out of scope".
- Refinements:
  - **Don't stop/start the BLE stream per rep.** The current code does `start_notify`/`stop_notify` each time, which is slow and fragile. Stream continuously and log cue times.
  - **Don't trust the 1 s window to contain the punch.** Reaction time varies, and a slow punch may spill over the edge.
    - Instead, search a generous region after each cue (e.g. cue → cue + 1.5 s).
    - Find the punch automatically: peak of acceleration or gyro magnitude.
    - Cut a **fixed window aligned on that peak** (e.g. −0.4 s / +0.4 s; Worsey used ±0.6 s).
    - The window length is fixed, but its position adapts to the punch. That removes the "unequal length" problem entirely.
  - **Randomise the order of cued punches**, so fatigue and "getting into a rhythm" don't correlate with class.
  - Keep some **free-form rounds** too (unscripted shadow-boxing, with a phone video to label later if time permits). They form a small, realistic test set that shows whether the scripted-data model generalises. That is great evaluation material.
  - **Spot-check**: plot a random ~5% of cut windows to verify the detector found the right thing. In the report this is "semi-automatic labelling with manual verification".

**(b) DBA templates + DTW matching as *the* classifier: good as one option, risky as the only one.**

- **What it is.**
  - DTW = a distance between two sequences that allows stretching in time.
  - DBA = averaging many examples into one representative "template" per class.
  - Classification = "which class template is closest". In ML terms this is **nearest-neighbour classification with an elastic distance**, a classic, strong time-series baseline.
- **Why it worked in the Earable paper.** Their classical-ML failure was with *blind sliding windows*: the dodge could start anywhere in the window, so the features varied wildly. That is a windowing problem, not proof that features fail.
  - The punch papers that *peak-align* their windows get good person-independent results with plain features: RD α 89.5% on new participants, Khasanshin 87–95%, Manoharan 91–95%.
  - So the fair comparison is **same event detector + same peak-aligned windows → {features + RF/SVM} vs {DTW}**.
- **Strengths.**
  - Speed-tolerant by design.
  - Needs little data.
  - Easy to explain in the demo Q&A.
- **Weaknesses.**
  - Sensitive to amplitude and offset. You must **z-normalise each channel per window**, which, conveniently, also removes the "slow punch = small values" issue.
  - Unconstrained DTW can over-warp and make different punches look alike. Use a warping-band limit.
  - One average template per class may under-represent people with different styles. Use a few templates per class (e.g. per participant), or k-NN over all training examples.
  - Cost is O(n²) per comparison. It's fine on a Pi for ~80-sample windows and tens of templates.
- **Rubric angle.** The ML criterion (25%) asks for *multiple approaches compared* and justified. "DTW template matching vs feature-based RF/SVM vs (optionally) a small 1D-CNN, all on the same windows, evaluated by leave-one-subject-out" is a textbook HD-style ML section. Choosing DTW alone would leave that on the table.

### 2.7 "Will a slow punch have a small max and be missed by RF/SVM?"

**Yes, that's a real risk, and your intuition is right.**

- RF and SVM don't see shapes. They see the numbers you give them.
- If every training jab has `max(ax) ≈ 5 g` and a slow jab gives 1.5 g, the model has never seen that region of the feature space. It will guess, probably "no-punch" or the wrong class.
- Two separate failure points:
  - **Detection:** a slow punch may not even cross the event-detection threshold.
  - **Classification:** it gets detected but misclassified.

Ways to handle it (they combine well):

1. **Put slow punches in the training data.** Record every class at, say, 50%, 75% and 100% effort. This is the simplest and most honest fix. It also creates a "condition" dimension for the dataset table and the robustness evaluation ("accuracy by intensity").
2. **Use amplitude-invariant features:**
   - **Correlations between axes** (RD α used pairwise Pearson correlations per sensor, which are unaffected by scale).
   - **Ratios**: e.g. share of energy on each axis.
   - **Timing and order**: which axis peaks first, sign patterns of the gyro.
   - **Features computed after normalising the window by its own peak.**
   - **Orientation (gravity direction) features**, especially from the upper-arm sensor (§2.1).
3. **Normalise the window in time and amplitude** before DTW or a CNN (z-normalise, and optionally resample the detected punch to a fixed length).
4. **Augmentation:** add time-stretched and magnitude-scaled copies of training windows.
5. **Scope it explicitly.** "Shadow-boxing at moderate-to-full intensity; slow technical drilling is out of scope" is a legitimate context statement, *if* it matches the stated application. The detector threshold can then be justified from data (e.g. the lowest punch peak observed at 50% effort).

---

## 3. What to borrow from which paper

| From | Borrow | Watch out |
|---|---|---|
| **Worsey 2020** (local PDF) | Peak-triggered fixed window (±0.6 s around the peak of the punch-direction axis, peak >60% of max, ≥0.5 s apart). Zeroing orientation at the window start. 132 time+frequency features → PCA. Comparing 6 scikit-learn models | **Only 1 participant, pad work, 5-fold CV**, so it's person-dependent. The "60% of max" threshold assumes you know the person's max and hit a target |
| **RD α 2023** (local PDF) | Feature set: min/max/mean/std/kurtosis/skew per axis + **pairwise axis correlations** + duration. Stance and hand added as *features*. **Separate evaluation group of new participants** (13 train / 8 new test). Baseline table of 8 classifiers with default settings. Per-class report (hook weakest, F1 0.82). **They also classified backfist (F1 0.89) and ridge hand**, evidence that some "advanced" strikes are learnable | Their segmentation relies on a force sensor we don't have, and on target impact |
| **Manoharan 2025** | Continuous real-time loop: rolling window + smoothing rule (merge flickers ≤0.2 s). "No punch" class. Lead/rear role labelling | Detection lags until ~60% of the punch is in the window. Active learning is probably out of scope |
| **Earable (Sepanosian & Incel 2024)** | Idle-vs-motion "anomaly" start/end detection. DBA templates + DTW. Marker movement for sync | Head dodges, 1 sensor, 3 classes. Different problem, so the 96% figure doesn't transfer directly |
| **Khasanshin 2021** | Magnitude-only inputs (orientation- and hand-free). "Movement without punch" class. Beginners vs experts analysis | Loses direction information, which hook-vs-uppercut needs |
| **SensiML** | Threshold segmentation → ~16 selected features → tiny model. Explicit Unknown class | Proprietary tooling |
| **Dehghani 2019** | Leave-one-subject-out evaluation. Avoid overlapping-window leakage | — |

---

## 4. Problems not raised yet

1. **Every group in the room runs Nano 33 IoTs, many with the same example UUIDs** (`19B10000-E8F2-…` comes from the Arduino examples).
   - Connect by our own unique names or MAC addresses, never by "first device advertising this service".
   - The Week 13 demo room will also be congested at 2.4 GHz, so test reconnection behaviour.
2. **Real-time latency budget.** A peak-centred window needs data *after* the peak (e.g. +0.4 s), plus batching delay (50–100 ms), plus BLE latency and compute.
   - Expect ~0.5–0.8 s from punch to display. That's fine for a trainer display, but it should be measured and reported ("real-time performance" is an explicit evaluation item).
3. **`Serial.println` in the sampling loop** slows it and adds jitter. Remove it from the hot path, or put it behind a debug flag.
   - Also, `IMU.accelerationAvailable()` polling means the loop timing sets the sample timing. Timestamp every sample (or batch), or use the IMU's FIFO.
4. **The null class needs a collection plan of its own:** guard/bouncing/footwork, walking around, guard-arm motion during the other arm's punches, gesturing and talking, blocks and parries. Otherwise the live demo will "see" punches everywhere.
5. **Participants.**
   - Group members' skill levels differ. Khasanshin showed beginners are the most variable.
   - Decide: teach a standard technique (short video + your coaching), or record "natural" technique and discuss it.
   - Recruiting 2–4 extra people makes leave-one-subject-out much more convincing.
   - Handle consent and anonymised IDs (ULO2 ethics/privacy).
   - Record any left-handed members' natural stance.
6. **Orientation consistency.**
   - Mark "this edge towards the fist" on each mount.
   - Assign each board a fixed body position (label boards L-wrist, L-arm, etc.) so board identity never swaps.
   - Consider a 2 s "guard pose" at the start of each session as a reference.
7. **Class and sample-count balance.** Plan the repetitions per class × participant × intensity × session up front. This fills the rubric's dataset table directly (participants / sessions / duration / raw readings / ML windows).
8. **Session effects.** Mounts are re-worn each day, which is a real source of error. Record ≥2 sessions per person on different days if possible, and report cross-session accuracy.
9. **Hardware durability.** Loose header pins and jumper wires fail under impact (Motion Tape had solder joints break). Use a solid enclosure (3D-printed or a small project box), strain relief on the battery lead, and ideally solder rather than breadboard.
10. **Timeline.** Part A (the report) needs results, results need data, and data needs working firmware + mounts + the collection script. Firmware and data collection are the critical path. D1–D6 should be settled within days, not weeks.

---

## 5. A strawman end-to-end design (to argue about, not a decision)

- **Node (×4):**
  - Sample at ~100 Hz (104 Hz ODR), ±16 g, ±2000 °/s.
  - Batch ~5–10 samples per notification, with a `uint16` sequence number and a `uint32` board timestamp.
  - Unique BLE name per board (`BOX-L-WRIST`, …).
  - Battery powered, in an enclosure, on marked Velcro mounts.
- **Edge (Pi):**
  - One asyncio process holds all 4 connections.
  - Per board: gap detection and clock-offset estimation.
  - Raw stream goes to a local session file.
  - Per arm:
    - Merge wrist + upper-arm streams into a ring buffer.
    - Event detector (motion-energy threshold with hysteresis, plus a per-arm cooldown).
    - Peak-aligned window.
    - Classifier → label + confidence.
    - Cross-arm tie-break rule.
    - Terminal display: "RIGHT HOOK (0.93)".
  - Async uploader to Atlas.
- **Cloud (Atlas):** participants, sessions, bucketed raw chunks, cues, prediction logs.
- **Collection protocol:**
  - Sync clap.
  - Guard pose.
  - Cued randomised reps: each class × 3 intensities.
  - Null-class blocks.
  - A free-form round (video optional).
  - Repeated on 2 days where possible.
- **ML (offline, Python):** same detected windows fed to
  - (A) features → RF / SVM / k-NN,
  - (B) z-normalised raw → DTW-kNN / DBA templates,
  - (C) optional small 1D-CNN.
- **Evaluation:**
  - Leave-one-subject-out.
  - Confusion matrices, macro-F1.
  - Ablations: sensor sets {wrist, upper arm, both}; per-role vs mirrored single model; by intensity.
  - Live test: latency, detection recall/false positives in the free-form round, packet-loss rate.

---

## 6. Corrections to the team docx (checked against the local PDFs)

- **Worsey 2020:**
  - **One participant only (n = 1)**, with Muay Thai/boxing experience, 250 punches total, 5-fold CV. All results are person-dependent.
  - The glove sensors were on the **inside of each glove's Velcro strap**.
  - The third sensor was in a **sports harness at T3 (upper back)**, not on the neck.
  - Sensors: 250 Hz, ±16 g, ±2000 °/s.
- **RD α 2023:**
  - Stance and hand were **added as two input features** to one model, not trained as separate models.
  - 13 participants for training, **8 new participants** for evaluation: 89.55% technique accuracy on unseen people, 1951 strikes.
  - Classes included **backfist and ridge hand** alongside straight/hook/uppercut. That's directly relevant to your "advanced moves" question.
  - Participants chose their own stance and distance, and struck 4 targets (bag, pads, gloves, concrete wall).
- **Earable paper:** the local PDF has no text layer (it's scanned images), so I couldn't verify the docx summary of it.
