# Iteration-1 Research: Wearable Boxing-Punch Recognition

> **Status:** first-pass research, written 2026-09-25. Nothing here is a final decision. The aim is to show what other people did, how they did it, and which parts matter for our open questions.
>
> **How to read this:**
> 1. §1 turns the assignment into a list of things we must deliver.
> 2. §2 reviews the prototype code already in the repo (it has some blocking bugs).
> 3. §3 is a short ML glossary. Read it first if terms like "window", "feature" or "LOSO" are new.
> 4. §4 has one entry per source I read: link, summary, and what it tells us.
> 5. §5 goes back to each open question and lists what the research says.
> 6. §6 lists problems nobody asked about yet but that will hurt later.
> 7. §7 gives suggested next steps.

---

## 1. What the assignment actually requires (Part A focus)

**Hard requirements from the brief:**

| Requirement | Implication for us |
|---|---|
| Node = Arduino Nano 33 IoT, Edge = Raspberry Pi, Cloud = MongoDB Atlas | 3 tiers must all exist and appear in the architecture diagram |
| "Collect IMU data using the Arduino" | Accelerometer + gyroscope (the LSM6DS3 chip, no magnetometer) |
| Pi relays data to MongoDB; data managed in cloud | Raw or windowed data must be stored in Atlas, not just local CSV |
| Extract features, train ML, **≥3 non-trivial classes** | Boxing is fine. 6 classes (3 punches × 2 hands) is already well above the minimum |
| **Real-time** recognition, **displayed on the Pi** | Needs a live pipeline: stream → detect punch → classify → print "right hook" |
| "Acceptable accuracy" | No fixed number. The accuracy we need comes from the application context we define |

**What the rubric rewards (Part A report, 15%):**

- **Problem & context (10%)**: HD wants "multiple users, activities, environments and/or deployment constraints" plus *success criteria*. We should write concrete targets, e.g. "≥85% macro-F1 on unseen users, <500 ms latency".
- **Architecture (20%)**: HD wants *justified sensor placement*, critically analysed protocols and trade-offs, and attention to power and scalability. This is where the 4-sensor-vs-2-sensor debate and BLE bandwidth analysis go.
- **Data collection (20%)**: HD wants "multiple activities, users, sessions, environments and operating conditions", balance, and a critical discussion of bias. **This is where boxing projects usually lose marks.** The report template even asks for a table with *participants / sessions / total duration / raw readings / ML windows* per activity. We need to plan for that table from day 1.
- **ML (25%)**: HD wants *multiple approaches compared*, plus preprocessing, feature engineering and optimisation, all justified by deployment constraints (edge vs cloud).
- **Evaluation (25%)**: HD wants "rigorous validation … robustness across users, sessions or conditions". In practice that means **testing on people the model never saw in training** (see LOSO in §3), plus confusion matrices, precision/recall/F1, and real-time system tests.

The brief's "Note to Students" lists *"sports performance analysis"* and *"gesture recognition"* as examples of stronger projects, so boxing is a well-aligned choice. It also says "multi-sensor wearable designs **where justified**", so using 4 sensors is only worth marks if we can argue *why* (§5.2).

---

## 2. Review of the existing prototype code

These notes are factual, from reading the two files. I haven't fixed anything, per your instructions.

### `arduino_data_collection.ino`

1. **Sampling rate is ~10 Hz.** `IMU_READ_INTERVAL = 100` ms. The research below uses 100–1000 Hz. A jab's forward phase lasts roughly **0.15 s** (Kimm & Thiel, §4.8), so at 10 Hz a whole punch is only 1–2 samples. This is the biggest technical issue.
2. **Accelerometer range is ±4 g (library default, not changeable through its API).** The official `Arduino_LSM6DS3` library hard-codes `CTRL1_XL = 0x4A` ("104 Hz, 4 g") and gyro to 2000 dps at 104 Hz ([library source](https://github.com/arduino-libraries/Arduino_LSM6DS3/blob/master/src/LSM6DS3.cpp)). A jab in the air reaches **~6 g** on the forward axis at the wrist (Kimm & Thiel), and the deceleration "jolt" at full extension looks even larger in their plot. So **±4 g will clip (saturate) punch peaks.** The chip itself supports ±16 g. We can get it by writing the register ourselves or by using the SparkFun LSM6DS3 library at I²C address `0x6A` ([Nano 33 IoT Ultimate Guide](https://github.com/ostaquet/Arduino-Nano-33-IoT-Ultimate-Guide/blob/master/README.md)).
3. **Max IMU rate is 104 Hz with the official library.** That is fine for classification (SensiML's boxing tutorial used 104 Hz, §4.6), but it is the ceiling unless we reconfigure the chip.
4. `convertToFixed(val * 8)` gives accel resolution of 1/8 g = 0.125 g. That's coarse. Scaling by e.g. ×1000 (milli-g) would still fit in `int16` at ±16 g (±16000).
5. There is **no timestamp or sequence number** in the BLE packet, so the Pi can't detect lost packets or line up the 4 boards.

### `data_collection.py`

These would stop it running as-is:

- `struct.unpack('<12h', data)` expects 24 bytes, but the Arduino sends `sizeof(IMU_3D)` = 6 × int16 = **12 bytes** → the call raises `struct.error`. The bare `except: pass` hides this, so **no sample would ever be stored**. It should be `'<6h'`.
- `TARGET_NAME_FRAGMENT` (singular) is used but only `..._1.._4` / `TARGET_NAME_FRAGMENTS` are defined → `NameError`.
- `window_id += 1` inside `read_IMU` without `global` → `UnboundLocalError`.
- `COLLECTION_PERIOD = 1000` is compared against `time.time()` (seconds) → a 16-minute recording.
- `read_temp` / `read_humidity` don't exist (leftovers from a template), and `read_IMU(client)` is called without its `label` argument.
- `collateData` computes mean/stdev/min/max/range and then **throws them away** (nothing is saved or returned), and it also clears the raw arrays. The raw data is lost.

The general idea (connect → command characteristic to start → notify stream → label → compute stats) matches the pipeline used in the papers. The details just need rework, and the research in §5 affects *what* the rework should be (sample rate, windowing, storing raw data).

---

## 3. Minimal ML glossary (read once, refer back)

- **Sample**: one reading from the IMU at one instant: `ax, ay, az, gx, gy, gz` (6 numbers). At 100 Hz you get 100 samples per second per board.
- **Channel / axis**: one of those 6 streams (e.g. `gz` over time).
- **Window / segment**: a chunk of consecutive samples cut out of the stream, e.g. 0.8 s = 80 samples at 100 Hz. The model classifies *windows*, not single samples.
  - **Sliding window**: cut windows at fixed steps (e.g. every 0.1 s) whether or not anything is happening. Simple, but most windows contain "nothing".
  - **Event-based segmentation**: first *detect* that a punch happened (e.g. acceleration magnitude crosses a threshold), then cut a window around it. Most boxing papers do this.
- **Label**: the correct answer for a window ("right_hook"). Collecting correct labels is usually the most tedious part.
- **Feature**: a number computed from a window that summarises it, e.g. mean of `ax`, max of `gz`, standard deviation of `|a|`. Your `mean/stdev/min/max/range` idea is exactly this. 6 channels × 5 stats = 30 features per window per board.
  - **Time-domain features**: mean, std, min, max, range, skewness, kurtosis, interquartile range, energy, zero-crossings…
  - **Frequency-domain features**: from an FFT, e.g. "how much energy is at 5–10 Hz".
- **Raw-input models**: skip features. Feed the raw window (e.g. 80 samples × 6 channels) straight into a neural network (CNN/LSTM), which learns its own features. These need more data.
- **Classifier / model**: the algorithm mapping features → label. Common ones in these papers:
  - **k-NN**: "which labelled examples is this closest to?"
  - **SVM**: finds boundaries between classes. Strong on small feature datasets.
  - **Decision tree / Random Forest (RF) / XGBoost**: many if-else trees voting. Strong default, robust, easy to explain.
  - **MLP (multilayer perceptron)**: a basic neural network.
  - **CNN / LSTM / Transformer**: deep networks for raw sequences.
- **Training set / test set**: the model learns from one part and is graded on another part it never saw.
- **Overfitting**: the model memorises the training data, so it scores 99% there and much lower on new data. Example: the IEEE glove study (§4.9) got 99.9% on training and 73% on test.
- **Person-dependent (PD) vs person-independent (PI) evaluation**:
  - PD: training and test data come from the *same* people. Easy, and the scores look inflated.
  - PI: test people were *never* in training. Harder, and more honest.
  - **LOSO (Leave-One-Subject-Out)**: repeat PI evaluation, each time holding out one person. With 4 group members plus some friends, this is the method to use. BoxerSense (§4.3) got 98.9% PD vs **91.1% PI** on the same data.
- **Confusion matrix**: table of "true class vs predicted class". It shows *which* punches get mixed up (e.g. hooks predicted as jabs).
- **Precision / Recall / F1**:
  - Precision: of all the "hook" predictions, how many were really hooks.
  - Recall: of all the real hooks, how many we caught.
  - F1: the balance of the two. **Macro-F1** averages F1 over classes so that rare classes count equally.
- **Null / "no-punch" / "unknown" class**: windows where no target move happens (guard, bouncing, walking, blocking). **Essential for a real-time demo**, otherwise the model is forced to call everything a punch.
- **Data augmentation**: making extra training samples by transforming existing ones, e.g. stretching in time (faster/slower punch) or mirroring left↔right.

---

## 4. Sources read (one entry each)

### 4.1 Manoharan et al. (2025). *An active machine learning framework for automatic boxing punch recognition and classification using upper limb kinematics*. PLOS ONE.

- **Links:** [PMC full text](https://pmc.ncbi.nlm.nih.gov/articles/PMC12061147/) · [PLOS](https://journals.plos.org/plosone/article?id=10.1371%2Fjournal.pone.0322490) · related [preprint "IoT Wearable Sensors for Automatic Boxing Punch Recognition…"](https://www.preprints.org/manuscript/202407.1093/v1) · **public dataset** [Zenodo "Boxing punch data" (CC-BY 4.0)](https://zenodo.org/records/14965635)
- **Hardware:** MetaMotion IMUs, **±16 g accel, ±2000 °/s gyro, 200 Hz**, streamed over Bluetooth. **One sensor on each wrist, under the gloves.**
- **Participants:** 8 elite boxers (5 orthodox, 3 southpaw). ~320 shadow-boxing punches over 14 punch types recorded, of which 6 were classified.
- **Classes:** lead/rear × jab/hook/uppercut, plus **"no punch"**. Lead/rear was analysed *separately*, so stance is expressed as **lead vs rear hand, not left vs right** (relevant to your mirroring question).
- **Segmentation:**
  - Rolling window of 180 samples (0.9 s ≈ average punch duration) sliding by 1 sample.
  - A window counted as "punch" if ≥60% of the punch fell inside a 0.8 s window.
  - Post-processing rules: punch ≈ 0.8 s long, and ≥0.2 s between events.
- **Labelling:** video at 60 FPS synced to IMU via UTC timestamps.
- **Features:**
  - Time-domain: mean, std, max, min, IQR, entropy, skewness, kurtosis, mean absolute deviation.
  - Frequency-domain: power spectral density, spectrogram.
  - They note jabs show more energy on x, hooks on y, uppercuts on z. The axis a move happens on is itself a good feature.
- **Models:** committee of Naive Bayes, k-NN, Decision Tree and ensemble, trained with **active learning** ("Query by Committee"):
  - Start with 5% labelled data.
  - The models vote, and the samples they disagree on most are sent to a human to label.
  - Repeat until 15% is labelled.
  - The point is to cut labelling effort about 6×.
- **Results (tested on *unseen* boxers):** punch recognition 91–92%, classification 92–95%. Prior work cited reached 96% with Random Forest, but that used 80% of the data for training.
- **Errors & limitations:**
  - Start/end of punches confused with the preparatory "no-punch" motion.
  - **Some hooks misclassified as jabs** (similar wind-up).
  - **Combos with ≤0.2 s gap merged into one punch** (17 detected vs 18 real).
  - Elite boxers only. Messy or incorrect technique may reduce accuracy.
- **Why it matters to us:**
  - The closest to what we want to build.
  - It is also a ready-made public dataset to prototype the ML pipeline on *before* our hardware works. It uses a different sensor, so it's for learning and not for our final model.

### 4.2 Worsey, Espinosa, Shepherd & Thiel (2020). *An Evaluation of Wearable Inertial Sensor Configuration and Supervised ML Models for Automatic Punch Classification in Boxing*. IoT (MDPI) 1(2).

- **Links:** [MDPI](https://www.mdpi.com/2624-831X/1/2/21) · [PDF](https://www.mdpi.com/2624-831X/1/2/21/pdf?version=1605262719) · [ResearchGate](https://www.researchgate.net/publication/345916196_An_Evaluation_of_Wearable_Inertial_Sensor_Configuration_and_Supervised_Machine_Learning_Models_for_Automatic_Punch_Classification_in_Boxing). MDPI blocked my fetcher, so this summary comes from the abstract, metadata and quotes in other papers.
- **Setup:** SABELSense IMUs (Griffith University, Australia). Two configurations compared:
  - Config 1: **both wrists only**.
  - Config 2: both wrists **+ upper back (thoracic vertebra, T3)**.
  - Recorded during **pad work**.
- **Models compared (six):** including Gaussian SVM, MLP neural net, Random Forest and XGBoost, each with default ("untuned") and tuned hyper-parameters.
- **Results:**
  - Mean accuracy 0.90 ± 0.12 (wrists) vs 0.87 ± 0.09 (wrists + back).
  - Best model: SVM 0.96 on wrists only, MLP 0.98 with the back sensor.
  - **No statistically significant difference** between configurations or between tuned and untuned models.
- **Why it matters:**
  - Directly answers "do we need more than wrist sensors?" For basic punches, **adding a back sensor didn't help significantly.**
  - Tuning didn't matter much either, so a simple model with defaults is a reasonable baseline.
  - The authors recommend testing in *sparring* next. Pad work is cleaner than real fighting.

### 4.3 Hanada, Hossain, Yokokubo & Lopez (2022). *BoxerSense: Punch Detection and Classification Using IMUs*. Springer (ABC 2021 proceedings).

- **Links:** [Springer](https://link.springer.com/chapter/10.1007/978-981-19-0361-8_6) · [ResearchGate](https://www.researchgate.net/publication/360375901_BoxerSense_Punch_Detection_and_Classification_Using_IMUs) (full text paywalled; details below come from the abstract and citing papers)
- **Setup:**
  - 10 participants, 6 punch types from both hands.
  - Compared **a right-wrist IMU vs an upper-back IMU** as the *single* sensor.
- **Results:**
  - Detection 98.8%.
  - Classification with SVM: **98.9% person-dependent vs 91.1% person-independent.**
  - **The upper back suited classifying *both* hands' punches with one sensor better** than one wrist.
- **Why it matters:**
  - A single torso sensor "sees" body rotation for both hands. A single wrist sensor only sees its own hand well.
  - The ~8-point drop from PD to PI is the typical cost of testing on new people, so we should expect and report it.

### 4.4 Khasanshin (2021). *Application of an Artificial Neural Network to Automate the Measurement of Kinematic Characteristics of Punches in Boxing*. Applied Sciences 11(3):1223.

- **Links:** [MDPI](https://www.mdpi.com/2076-3417/11/3/1223) · [open PDF (Semantic Scholar)](https://pdfs.semanticscholar.org/209d/c7ee57b84157e4f752f32447ef395814aa30.pdf). Read in full.
- **Hardware:**
  - Custom IMU box, **±16 g, ±2000 °/s**, 35 g total including microcontroller and Bluetooth.
  - Worn on **both wrists** with the y-axis along the punch direction and the x-axis along the thumb. It says "fixed", with a figure showing it on the back of the wrist.
  - Kalman-filtered on the microcontroller.
  - **1 sample per ms (1 kHz) for 300 ms per punch.**
- **Clever input choice:**
  - The network input was **only the *magnitude* of acceleration and of angular rate**: |a| = √(ax²+ay²+az²), same for gyro.
  - That gives 300 + 300 = 600 numbers.
  - Magnitudes don't depend on sensor orientation or on which hand, so **punches were not separated by left or right hand at all.**
- **Participants:** 85 male boxers in 3 skill groups (1 yr / 2–3 yr / 5+ yr). Split into training people and **separate evaluation people** (person-independent).
- **Model:**
  - MLP with 4 hidden layers (512-256-128-64).
  - 4 outputs: straight, hook, uppercut, **"movement without punches"**.
  - Trained in Keras, quantised to 8-bit, and **run on the STM32 microcontroller in 3 ms per punch.**
- **Results on unseen boxers:**
  - Beginners 87.2% ± 5.4 (91.9% with the "universal" model trained on all groups).
  - Intermediate 95.3%.
  - Experts 91.7%.
  - Beginners have inconsistent technique. Experts each have a personal style. **Both lower accuracy.**
- **Why it matters:**
  - Shows a very simple, orientation-free representation works.
  - Shows "no-punch" as a class.
  - Shows on-device inference is feasible on a microcontroller.
  - Shows that **the skill level of whoever records the data matters**. Our group is probably mixed-skill.

### 4.5 Kimm & Thiel (2015). *Hand Speed Measurements in Boxing*. Procedia Engineering 112.

- **Links:** [ScienceDirect](https://www.sciencedirect.com/science/article/pii/S1877705815014897) · [open PDF](https://d-nb.info/1203379595/34). Read in full.
- **Setup:**
  - 16 amateur boxers, 20 jabs + 20 crosses in the air.
  - Accelerometer **attached to the inside of the wrist with a Velcro band, under the glove.**
  - The sensor was **modified for high accelerations (200 g range).**
- **Findings:**
  - A jab's forward phase: **~+6 g on the forward axis, lasting ~0.148 s.**
  - Then a large negative "inertial jolt" at full extension.
  - Which axis dominates **varied between athletes (technique and style)**, so they used the RMS (magnitude) over all 3 axes.
  - Retraction couldn't be reliably detected from the accelerometer.
  - Fist speeds were ~6–8 m/s.
- **Why it matters:**
  1. The punch is **~150 ms**. At our current 10 Hz, that's 1–2 samples, so we need ≥100 Hz.
  2. **~6 g already exceeds the ±4 g default range.**
  3. Per-person axis differences are another argument for magnitude features.
  4. Shows a Velcro-band attachment method that works.

### 4.6 SensiML / TensorFlow blog: boxing-punch TinyML tutorial

- **Links:** [TensorFlow blog (2021)](https://blog.tensorflow.org/2021/05/building-tinyml-application-with-tf-micro-and-sensiml.html) · [SensiML boxing tutorial docs](https://sensiml.com/documentation/application-tutorials/activity-recognition-boxing-punches.html)
- **Setup:**
  - Arduino Nano 33 BLE Sense (TF blog version) or QuickLogic board (docs version).
  - **104 Hz** accel + gyro over BLE.
  - **Board taped inside the boxing glove with double-sided tape, powered by a small 3.7 V 100 mAh LiPo.**
- **Classes:** Jab, Overhand, Cross, Hook, Uppercut, plus **"Unknown"**.
  - **Separate models for the left glove and the right glove.**
  - Metadata such as boxing experience and dominant hand was recorded.
- **Pipeline:**
  - **Threshold-based event segmentation**: detect a punch event first, then classify it.
  - Features: statistical, shape, area, rate of change.
  - Tree-based feature selection down to **16 features**, then scaled to 1 byte each.
  - A tiny neural net (Dense 12 → 8 → 6, **362 parameters, ~3 KB**).
- **Results:** 92.7% accuracy, F1 92.6. Per-class sensitivity 87–96%.
- **Why it matters:**
  - Almost exactly our hardware class and sample rate.
  - Shows that a *tiny* model on good features works.
  - Shows that per-hand models are a valid alternative to mirroring.
  - Shows the Unknown class is essential to suppress false positives.

### 4.7 Hykso punch trackers (commercial product) and reviews

- **Links:** [Wareable review](https://www.wareable.com/sport/hykso-boxing-wearable-review-230) · [FightQuality review](https://fightquality.com/home/equipment/hykso-punch-trackers-review/) · [HotHardware review](https://hothardware.com/reviews/hykso-punch-trackers-review) · [Digital Trends](https://www.digitaltrends.com/wearables/hykso-wearable-sensors-improve-boxer-performance/)
- **Hardware:**
  - Per wrist: a **high-g accelerometer + a low-g accelerometer + a gyroscope**, sampled at **1000 Hz**.
  - Worn **on top of the wrist, under the hand wraps**.
  - Curved edge must face forward, so orientation matters.
  - Colour-coded left/right units.
- **Classes:** only **left/right × "straight" (jab/cross) vs "power" (hook/uppercut)**. The product also ignores non-punches such as skipping rope, blocks, parries and kicks.
- **Why it matters:**
  - A funded company with 1 kHz dual-range sensors *chose to merge hook and uppercut* into one category for the main classification. That hints hook vs uppercut is the hard pair.
  - It confirms that placing the sensor under wraps works mechanically.
  - It shows that needing both a high-g and a low-g accelerometer is a real engineering concern (see saturation, §5.8).
  - Reviewers reported slipping during wrapping and complained about speed accuracy.

### 4.8 Qi et al. (2026). *Reliability and validity of the "XingXun" system for measuring punch acceleration and velocity in elite boxers*. Frontiers in Physiology.

- **Links:** [Frontiers](https://www.frontiersin.org/journals/physiology/articles/10.3389/fphys.2026.1726442/full) · [PDF](https://www.frontiersin.org/journals/physiology/articles/10.3389/fphys.2026.1726442/pdf)
- **Hardware:**
  - Commercial Chinese boxing tracker using TDK **ICM-20649** IMUs. This is a wide-range part, about ±30 g, according to its datasheet (my own addition, not from the article).
  - Sampled at 200 Hz.
  - Placed **on the outer edge of the wrist, just above the distal radioulnar joint**, with *two overlapping sensors per hand*.
- **Punch types:** lead/rear × jab/hook/uppercut. Validated against Qualisys optical motion capture: acceleration r = 0.84–0.91, ICC 0.93–0.95.
- **Limitations noted:** 200 Hz may be insufficient for rapid combos, and personal anatomy isn't modelled.
- **Why it matters:** another data point that serious systems use **wide-range accelerometers** and a precise, repeatable wrist location.

### 4.9 IEEE: *Boxing Strike Classification from Wearable IMU Sensor Unit* (2024)

- **Link:** [IEEE Xplore PDF](https://ieeexplore.ieee.org/iel8/10578941/10578942/10578966.pdf) (abstract only)
- **Setup:** IMU in a glove, 2 strike types, k-NN.
- **Results:** **99.9% train vs 73% test accuracy.**
- **Why it matters:** a textbook example of overfitting / poor generalisation. It is useful to cite in our report when justifying LOSO evaluation.

### 4.10 Motion Tape study (2025). *Boxing Punch Detection and Classification Using Motion Tape and Machine Learning*. Sensors 25(16):5027.

- **Link:** [PMC full text](https://pmc.ncbi.nlm.nih.gov/articles/PMC12390462/)
- **Setup:**
  - Not an IMU. It uses graphene "strain tape" on the skin: anterior deltoid, middle deltoid, forearm.
  - 80 Hz, BLE.
  - Classes: jab/lead hook × {normal, holding 5 lb dumbbells, hitting a bag}.
- **Method:**
  - **Adaptive threshold punch detector**: the threshold is recalculated from baseline noise, using Mahalanobis distance.
  - Then 3 sequence models compared: Time-Series Transformer 96.9%, MiniRocket 93.8%, InceptionTime 92.3%.
- **Key lesson:**
  - **Jabs into a bag scored only 75% (TST) / 50% (InceptionTime)**, while air punches scored ~100%. **Hitting a target changes the signal a lot.**
  - Also, there was no cross-subject test, and the authors admit overfitting risk and **solder joints breaking during intense movement**.
- **Why it matters:**
  - Air vs bag vs pads is a "condition" we should decide on, and maybe record (rubric: "operating conditions").
  - Also a warning about wiring robustness.

### 4.11 Review: *Limb biomechanics in combat sports: insights from wearable sensor technology* (2025). Frontiers in Bioengineering & Biotechnology.

- **Links:** [PMC](https://pmc.ncbi.nlm.nih.gov/articles/PMC12714896/) · [Frontiers](https://www.frontiersin.org/journals/bioengineering-and-biotechnology/articles/10.3389/fbioe.2025.1663592/full)
- **Summary:**
  - IMUs are usually on the wrist or in the glove for punches, and on the feet/shank or lower back for kicks.
  - Combat sports need *high sampling rates* to capture short impact peaks.
  - **Soft-tissue artefact** (sensor wobbling on skin) is a primary noise source.
  - **The attachment method is "often poorly described"** in the literature. We can gain marks by documenting ours properly.
- **Table of strike-classification studies:**
  - Taekwondo kicks from a single waist accelerometer (SVM 96%).
  - Full-body 17-IMU suits for taekwondo forms (CNN 97.8%).
  - Wrist IMU boxing studies (Worsey, Manoharan).
- **Why it matters:** a good citable overview for the report's background section. It also frames our methodological contribution: *document attachment and sampling properly*.

### 4.12 Sports Technology Blog (2019). *Using Wearable Sensors in Combat Sports*

- **Link:** [sportstechnologyblog.com](https://sportstechnologyblog.com/2019/09/02/using-wearable-sensors-in-combat-sports/)
- **Summary of a systematic review:**
  - Standard IMUs max out at **16 g / 2000 °/s, which is insufficient for high-impact moves**. Spinning kicks averaged ~130 g.
  - Placements: forearm/wrist (most common), hip, upper back, leg/ankle.
  - They recommend putting sensors **on the athlete, not the equipment**, so misses are still captured.
  - "Strike classification with minimal sensors is difficult", and studies that did it well used more devices.
- **Why it matters:** relevant to the "advanced moves" scope question. Spinning or whipping techniques (e.g. spinning backfist 转身鞭拳) produce much larger accelerations and rotation rates, so they are more likely to saturate our sensors.

### 4.13 Low-cost IMU reliability for punches and kicks (2025). Sensors 25(2):307.

- **Link:** [PMC](https://pmc.ncbi.nlm.nih.gov/articles/PMC11769417/)
- **Setup:**
  - A smartphone accelerometer (LIS3DH) at **50 Hz**.
  - Strapped to the wrist **over the glove strap** (and to the calf over the shin guard for kicks).
- **Segmentation:**
  - Find the peak acceleration magnitude.
  - Walk backwards until below a 2 m/s² threshold, which marks the punch start.
- **Results:** only moderate reliability (ICC 0.75–0.79). **The authors say 50 Hz is a major limitation** and estimate ~134 Hz is needed.
- **Why it matters:** independent evidence that low sampling rates hurt. It also gives a simple, reproducible "peak-then-walk-back" segmentation method.

### 4.14 Dehghani et al. (2019). *Subject Cross Validation in Human Activity Recognition*. arXiv 1904.02666.

- **Link:** [arXiv](https://arxiv.org/abs/1904.02666). Companion: [overlapping vs non-overlapping windows (Sensors 2019)](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC6891351/)
- **Finding:**
  - Normal random k-fold cross-validation **inflates HAR accuracy by ~10%**, and **~16% when windows overlap**.
  - The reason: nearly identical neighbouring windows from the same person land in both training and test sets.
  - Overlapping windows gave no real gain.
  - They recommend **subject-wise cross-validation** (e.g. LOSO).
- **Why it matters:** this is the single most important evaluation rule for our report's "rigorous validation" marks.

### 4.15 Bulling, Blanke & Schiele (2014). *A Tutorial on Human Activity Recognition Using Body-worn Inertial Sensors*. ACM Computing Surveys 46(3).

- **Links:** [ACM DL](https://dl.acm.org/doi/10.1145/2499621) · [Semantic Scholar (PDF)](https://www.semanticscholar.org/paper/A-tutorial-on-human-activity-recognition-using-Bulling-Blanke/b9eb00ee1656f40ae3bbfd8631bda30c1dd9206d) · [MATLAB toolbox](https://github.com/andreas-bulling/ActRecTut)
- **Summary:**
  - The standard beginner reference.
  - Defines the **Activity Recognition Chain**: data acquisition → preprocessing → segmentation → feature extraction → classification → evaluation.
  - Discusses the null class and evaluation pitfalls.
- **Why it matters:** good to structure our report §5 around, and a safe citation.
- Newer survey-tutorial: [Past, Present, and Future of Sensor-Based HAR (arXiv 2411.14452)](https://arxiv.org/abs/2411.14452).

### 4.16 Left/right mirroring techniques

- **Links:**
  - [US patent "Method and system for symmetric recognition of handed activities"](https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/11657281)
  - [Robust in-the-wild exercise recognition (arXiv 2511.23173)](https://arxiv.org/pdf/2511.23173)
  - [WIMUSim (Frontiers 2025)](https://www.frontiersin.org/journals/computer-science/articles/10.3389/fcomp.2025.1514933/full)
- **Ideas:**
  1. **Flip-to-canonical**: mathematically mirror all left-hand data so it looks right-handed. Train *one* model for "a hand". At runtime, mirror the left sensor's data before classifying.
  2. **Mirror augmentation**: add mirrored copies of the data to training, to balance handedness.
  3. **Which axes to flip:** mirroring across the body's midline flips the sign of one accelerometer axis and of the *other two* gyroscope axes. One source says "ax, gy, gz" for its axis convention. **The exact axes depend on how our board is mounted**, so we must verify with a test recording.
- **Why it matters:** see §5.5.

### 4.17 Time-warping and speed variation

- **Links:**
  - [Physically plausible augmentations for IMU HAR (arXiv 2508.13284)](https://arxiv.org/pdf/2508.13284)
  - [Head-gesture recognition with activity detection + DTW (PMC11122069)](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC11122069/)
  - [IMU hand-gesture recognition for HMIs (PMC6767360)](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC6767360/)
  - [MATLAB IMU gesture recognition](https://www.mathworks.com/help/nav/ug/gesture-recognition-using-inertial-measurement-units.html)
- **Ideas:**
  - **Dynamic Time Warping (DTW)** compares two sequences while allowing one to be stretched or squeezed in time. It is naturally speed-tolerant.
  - **Time-warp augmentation**: train on copies of each punch stretched by ×0.5, ×0.75, ×1.25, ×1.5.
  - Faster movement = compressed signal *and* bigger acceleration.
- See §5.7.

### 4.18 BLE with several Arduinos → Raspberry Pi

- **Links:**
  - [bleak issue #1858: multi-peripheral disconnects](https://github.com/hbldh/bleak/issues/1858)
  - [RPi forum: multiple Nano 33 over BLE](https://forums.raspberrypi.com/viewtopic.php?t=316755)
  - [Arduino forum: BLE max sampling rate](https://forum.arduino.cc/t/ble-maximal-sampling-rate-arduino-nano-ble-sense/953985)
  - [osteele/Arduino-BLE-IMU (packing into 20-byte packets)](https://github.com/osteele/Arduino-BLE-IMU)
  - [Raspberry Pi ↔ Arduino BLE walkthrough](https://aniotodyssey.com/2021/10/01/raspberry-pi-meet-arduino-arduino-meet-raspberry-pi-let-s-talk-bluetooth-le.html)
  - [bleak](https://pypi.org/project/bleak/)
- **Findings:**
  - On Linux (the Pi uses BlueZ), one user saw **3 IMU devices at 10 Hz stable, but 6 devices at 10 Hz got random disconnects** (`le-connection-abort-by-local`).
    - Root cause is BlueZ/controller limits.
    - Advice: **one process manages all connections**, stagger connecting, reduce the aggregate notification rate.
  - With ArduinoBLE, users report ~10–20 notifications/s in practice with default connection intervals.
    - The minimum connection interval is 7.5 ms.
    - Updating characteristics faster than ~25 ms reportedly causes trouble.
  - **Solution pattern: batching.** Pack several samples into one notification instead of one sample per notification.
- **Implication:** 4 boards × 100 Hz × 1 sample per packet = 400 packets/s. That is very unlikely to work. We need batching, e.g. 10 samples/packet → 40 packets/s total. Alternatively, do punch detection on the board and send only punch windows. **This needs a hands-on test early.**

### 4.19 Powering the Nano 33 IoT from a battery

- **Links:**
  - [Arduino forum: Nano 33 IoT with 3.7 V LiPo](https://forum.arduino.cc/t/powering-the-arduino-nano-33-iot-with-3-7v-lipo-battery/1372000)
  - [How-to thread](https://forum.arduino.cc/t/how-to-arduino-nano-33-iot-with-lipo-battery/615190)
  - [Nano 33 IoT Ultimate Guide](https://github.com/ostaquet/Arduino-Nano-33-IoT-Ultimate-Guide/blob/master/README.md)
- **Findings:**
  - The board runs at 3.3 V.
  - VIN accepts ~4.5/5–21 V (sources differ slightly). A 3.7 V LiPo on VIN is below spec.
  - Common fix: LiPo → **boost converter (e.g. MT3608) to 5 V → VIN**, with a **TP4056** module for charging.
  - Simplest option: a small **USB power bank** into the micro-USB port.
  - BLE draws ~47 mA vs ~110 mA with WiFi.
  - The SensiML tutorial ran a Nano 33 BLE Sense from a 100 mAh LiPo taped inside a glove.
- **Why it matters:** see §5.6.

### 4.20 On-device (TinyML) options for the Nano 33 IoT

- **Links:**
  - [emlearn](https://pypi.org/project/emlearn/)
  - [Eloquent Arduino: RF/XGBoost on Arduino (micromlgen)](https://eloquentarduino.github.io/2020/10/decision-tree-random-forest-and-xgboost-on-arduino/)
  - [Edge Impulse continuous motion recognition](https://docs.edgeimpulse.com/docs/continuous-motion-recognition)
  - [TinyML gesture repo (Nano 33 BLE Sense)](https://github.com/jaredmaks/tinyml-on-the-edge)
- **Findings:**
  - The Nano 33 IoT is a SAMD21 (Cortex-M0+, 48 MHz, **256 KB flash, 32 KB RAM**). That is much weaker than the Nano 33 *BLE Sense* (Cortex-M4F) that most TinyML tutorials use.
  - Random Forests and decision trees can be converted to plain C (emlearn / micromlgen) and run fine on such chips.
  - Edge Impulse officially targets the BLE Sense. I have **not verified** Nano 33 IoT support.
- **Why it matters:**
  - The brief requires display on the Pi, so inference on the Pi is the natural choice.
  - *Punch detection* (a simple threshold) on the Arduino could still cut BLE traffic. This is an "edge vs node" trade-off we can discuss for architecture marks.

### 4.21 Hobby and maker projects (lighter evidence)

- [Hackster news: "TinyML Packs a Punch"](https://www.hackster.io/news/tinyml-packs-a-punch-ccb2e9a086a3):
  - Two Nano 33 BLE Sense boards, one per hand, plus a Wio Terminal display.
  - Edge Impulse model, **99.4% punch-type accuracy** (likely person-dependent).
  - Also detects "blocking" while the other hand punches. Blocking was hard because the arm is static, so a separate k-NN was used.
- [Arduino blog (2026-09-09): Smart boxing band on Arduino Nesso N1](http://blog.arduino.cc/2026/09/09/this-smart-boxing-band-takes-advantage-of-the-new-arduino-nesso-n1/):
  - Edge Impulse, jab/hook/uppercut and timing between punches.
  - No metrics given.
- [karimjaouhar/Boxing-Classifier](https://github.com/karimjaouhar/Boxing-Classifier): ESP32 + LSTM + p5.js live display. The README is mostly placeholders, but it shows the architecture pattern.
- [MathWorks: punch vs flex on Arduino](https://www.mathworks.com/help/simulink/supportpkg/arduino_ref/identify-punch-flex-using-machine-learning-algorithm-on-arduino-hardware.html): a beginner-friendly worked example.
- **YouTube:** searches returned only punch-technique tutorials, no good ML demos. Those tutorials are still handy for *standardising how our participants throw each punch* (a data-quality point), e.g. [BOXING 101: Jab, Cross, Hook & Uppercut](https://www.youtube.com/watch?v=nky4BpWiwew) and [Correct vs Incorrect Punch Technique](https://www.youtube.com/watch?v=7UnRLmstNMU).

---

## 5. Open questions: what the research says

### 5.1 Has anyone done this before?

Yes, plenty. It is a well-established niche (§4.1–4.10). Typical results:

- **~95–99% when tested on the same people** as training.
- **~87–92% on new people.**
- Common baseline: 2 wrist IMUs, 6 classes (lead/rear × jab/hook/uppercut), **plus a no-punch class**.

Our novelty can't be "first ever". It can come from:

- a well-defined context (e.g. home shadow-boxing coach, kickboxing),
- an honest IoT pipeline (Node-Edge-Cloud, real-time),
- rigorous person-independent evaluation,
- a well-argued sensor-placement study using our 4 boards.

### 5.2 Where to place the sensors?

| Placement | Evidence | Notes |
|---|---|---|
| **Both wrists** | Nearly every study. Worsey: wrists-only was as good as wrists+back | Dorsal wrist under wraps (Hykso), inside wrist with Velcro (Kimm & Thiel), outer wrist above the radioulnar joint (XingXun) |
| **Inside the glove** | SensiML (double-sided tape) | Captures the fist, but gloves differ and swapping them is awkward |
| **Upper back (T3)** | BoxerSense: best *single* location for both hands. Worsey: no significant gain *on top of* wrists | Captures trunk rotation, which could help hook vs uppercut and stance detection |
| **Upper arm / shoulder** | Motion Tape (deltoid strain) | Different sensor type, but it shows the shoulder carries information about hooks |
| **Hip / ankle** | Review §4.11/4.12: used for kicks and footwork | Relevant only if scope includes kicks or stance |

**About our 4 boards, without a recommendation yet:**

- The literature suggests that 2 wrists already get you most of the way.
- Boards 3 and 4 need a *justification* (rubric: "multi-sensor where justified").
- Candidate justifications:
  - Torso sensor for body rotation or stance.
  - Upper arms for elbow angle, which may help hook vs uppercut.
  - Hip or ankle, if kicks are in scope.
- A nice report angle: **an ablation study**. Train with all 4 sensors, then with 2, then with 1, and report how accuracy changes. That is exactly the kind of "design trade-off analysis" the HD band asks for.

### 5.3 How to secure them?

Methods seen in the sources:

- **Velcro wristband** with the sensor on the inside of the wrist, under the glove (Kimm & Thiel).
- **Under the hand wraps**, on the back of the wrist, with a fixed forward-facing orientation. Reviewers found it slips unless you wrap first, insert the sensor, then keep wrapping (Hykso).
- **Double-sided tape inside the glove** (SensiML).
- **Strapped over the glove's wrist strap** (low-cost IMU study).
- 3D-printed housing + elastic Velcro strap (general upper-limb HAR literature, [JMIR review](https://www.jmir.org/2024/1/e51994)).

Must-knows:

- Soft-tissue wobble is a main noise source (§4.11).
- **Orientation must be identical every session.** Hykso marks "this edge forward" and Khasanshin defines axes relative to the punch direction. Otherwise the model sees "different" data each time someone re-wears it.
- The Motion Tape team had **solder joints break** during punching.

### 5.4 Scope: basic vs advanced moves?

- Research is almost entirely **jab / cross / hook / uppercut (± overhand)**, lead/rear.
- Even the commercial Hykso merges hook and uppercut into "power" punches, so **hook vs uppercut is the known hard pair**. Hooks are also sometimes confused with jabs (Manoharan).
- I found **no** IMU studies on spinning backfist, superman punch, 盖拳, 翻背拳, etc. That makes them novel, but there is no prior evidence to lean on. Spinning moves also have much higher accelerations and rotation rates and are more likely to saturate our sensors (§4.12).
- The rubric rewards "multiple activities" and a realistic context, **not** a huge class count.

Hedged observation, for us to decide later: a staged scope keeps the project safe.

- **Core set:** 6 classes plus no-punch.
- **Stretch goals:** 1–2 advanced moves that are *mechanically very different*, and are therefore easier to separate. Examples: a spinning backfist (whole-body rotation, which a torso sensor would see) or a body hook (下勾拳, low trajectory).

### 5.5 Mirroring (orthodox vs southpaw, lead vs rear)

Three approaches seen in the literature:

1. **Label by role (lead/rear), not by side (left/right)** (Manoharan):
   - Stance is a separate piece of information: ask the user or detect it.
   - Then "left jab" for an orthodox boxer = "right jab" for a southpaw, in terms of the *role* each hand plays.
2. **Separate per-hand models** (SensiML): one model for the left glove, one for the right. You don't need mirroring, but you need data from both hands for every move.
3. **Mirror data into one canonical hand** (patent, §4.16): mathematically flip left-sensor axes so that one model serves both hands. This doubles the effective data. It needs a careful check of which axes flip.

Also, **magnitude-only features** (Khasanshin) sidestep orientation *and* handedness completely. The cost is losing directional information (jabs = x, hooks = y, uppercuts = z in Manoharan).

Plan to record data in **both stances**, because a southpaw's rear hand isn't a perfect mirror of an orthodox rear hand: footwork and hip rotation differ too.

### 5.6 Power: cables vs violent moves

- No study used cables. Everyone used batteries: LiPo inside a glove (SensiML), commercial rechargeable units (Hykso, MetaMotion).
- For the Nano 33 IoT:
  - 3.7 V LiPo + boost converter to 5 V on VIN, plus a TP4056 charger module.
  - Or a small USB power bank into the micro-USB port.
- **The micro-USB connector is a mechanical weak point** under punching jolts, and a cable to a laptop will get yanked.
- Power-bank caveat (general knowledge, not verified in a source): some banks auto-switch-off when current draw is low (~tens of mA).

### 5.7 Fast vs slow punches (same shape, different speed)

What the literature does:

- **Event segmentation + fixed-length window** around the detected peak, e.g. 300 ms (Khasanshin), 0.8–0.9 s (Manoharan). A slower punch just fills more of the window.
- **Speed-invariant features**: ratios, *which* axis dominates, gyro sign patterns, and relative timings rather than absolute peak values.
- **Time-warp augmentation** (§4.17): train on artificially sped-up and slowed-down copies.
- **DTW-based classifiers**: stretch-tolerant by design.
- **Resampling every detected punch to a fixed number of samples** before classification (Motion Tape resampled to equal length).
- Simplest option: **deliberately record punches at varied speeds and intensities** (e.g. "50% / 75% / 100% effort") so the model learns speed variation directly. This also produces a data-collection "condition" the rubric likes.

### 5.8 What data to collect: raw or summaries?

- **Store raw data (all 6 channels, timestamped) in MongoDB. Compute features later in Python.** Every study kept raw data. If you only store mean/min/max you can never change your features, window length or segmentation afterwards. You'd have to re-record everything.
- Features used for punches (to try later):
  - Per-axis mean, std, min, max, range, IQR, skewness, kurtosis, MAD, entropy.
  - Magnitudes |a| and |ω|.
  - Frequency-domain PSD.
  - "Shape" and area features (SensiML).
- **Sampling rate:** evidence says ≥100 Hz.
  - 104 Hz is the official library's max and what SensiML used.
  - The research-grade systems used 200–1000 Hz.
  - 50 Hz was called "a considerable limitation".
- **Range:** ±16 g accel is standard (we're at ±4 g now). Gyro ±2000 °/s is standard but reported as possibly insufficient for spinning kicks.
- Gyroscope data *is* used by everyone. Rotation is what separates hook from jab.

### 5.9 Which ML model?

Classical ML on hand-made features works well at our data scale:

- SVM was the best in Worsey and BoxerSense.
- Random Forest reached 96% in the work cited by Manoharan.
- A tiny MLP at 92.7% (SensiML).

Deep models (CNN/LSTM/Transformer on raw windows) also work (Motion Tape 92–97%, Khasanshin MLP), but they need more data and are harder to explain in the demo Q&A.

Worsey found **tuning didn't significantly matter**. The rubric wants *several models compared*.

A natural progression to consider:

1. Baseline k-NN / Decision Tree.
2. Random Forest / SVM on features.
3. Maybe a small 1D-CNN on raw windows.

All evaluated with LOSO.

### 5.10 Other must-knows surfaced by the research

See §6.

---

## 6. Other limitations to know about

1. **Sensor saturation**: ±4 g now, and ±16 g may still clip hard deceleration jolts. We should *plot raw data from a hard punch early* and look for flat-topped peaks.
2. **Sampling-rate × BLE-bandwidth × 4-board budget**: 100 Hz × 4 boards needs batching (§4.18). We should test the aggregate throughput on the actual Pi early.
3. **Time sync across 4 boards**: each Arduino has its own clock (`millis()`), and BLE adds 40–50 ms of variable latency. Combining "left wrist + right wrist + torso" into one window needs timestamps or sequence numbers in packets, plus an alignment strategy.
4. **Labelling cost**: every paper synced *video* to labels, or used "throw 20 right hooks now" guided sessions. The guided approach is easier but less realistic (§4.1 active learning exists to cut this cost).
5. **Combos**: punches <0.2 s apart merge into one detected event (Manoharan). A demo with fast combos will under-count.
6. **Null-class realism**: the demo will include walking, adjusting the guard, gesturing while explaining, and blocking. Without "no-punch" data covering these, the system will hallucinate punches.
7. **Air vs bag vs pads**: hitting a target changes the signal a lot (bag jabs 75% vs air ~100%, Motion Tape). We need to decide the condition and record the one we'll demo.
8. **Participant diversity and skill**: beginners are inconsistent and experts are idiosyncratic (Khasanshin). A 4-person, possibly one-expert group is a small, biased dataset. This must be discussed in the report, and we could recruit extra participants.
9. **Evaluation leakage**: random train/test splits with overlapping windows inflate accuracy by 10–16% (Dehghani). Split *by person*, or at least by session.
10. **Re-wearing / orientation drift**: small rotations of the board between sessions change the axes. We can mark the orientation, and possibly add a calibration pose (e.g. hands in guard for 2 s) at the start of each session.
11. **Gravity component**: the accelerometer always includes ~1 g of gravity, whose split across axes depends on arm angle. Some features may need gravity removed, or should rely on the gyro.
12. **Hardware durability**: solder joints and connectors fail under impact (Motion Tape). Header pins and jumper wires on a Nano are the same risk.
13. **Real-time path vs cloud**: MongoDB Atlas round-trips are too slow to sit *in* the recognition loop. Keep classification on the Pi (edge), and log to the cloud asynchronously. This is a good architecture justification.
14. **Ethics and privacy (ULO2)**: we'd be recording body-movement data from people. We should mention consent, anonymised participant IDs, and data storage in the report.

---

## 7. Suggested next steps

Ordered roughly by how much they unblock. Nothing has been started.

1. **Hardware sanity test (1 board):**
   - Raise to ~100 Hz and ±16 g.
   - Record a few hard jabs and hooks.
   - Plot the raw data and check for clipping and punch duration.
2. **BLE throughput test:** 2 then 4 boards, batched packets, on the actual Pi. Measure packet loss.
3. **Prototype the ML pipeline on the public Zenodo dataset** (§4.1) while hardware work continues:
   - Segmentation → features → RF/SVM → LOSO → confusion matrix.
   - It's the fastest way to learn the ML side hands-on.
4. **Decide as a group:**
   - Application context and scope (§5.4).
   - Lead/rear vs left/right labelling (§5.5).
   - Sensor placement plan, including the ablation idea (§5.2).
   - Air vs bag condition (§5.8 / §6.7).
5. **Design the data-collection protocol** so the rubric's dataset table can be filled: participants × stances × speeds × sessions × repetitions, plus no-punch recordings.

---

## 8. Reference list (quick APA-ish, to tidy later)

- Bulling, A., Blanke, U., & Schiele, B. (2014). A tutorial on human activity recognition using body-worn inertial sensors. *ACM Computing Surveys, 46*(3). https://doi.org/10.1145/2499621
- Dehghani, A., Glatard, T., & Shihab, E. (2019). Subject cross validation in human activity recognition. arXiv:1904.02666. https://arxiv.org/abs/1904.02666
- Hanada, Y., Hossain, T., Yokokubo, A., & Lopez, G. (2022). BoxerSense: Punch detection and classification using IMUs. In *Sensor- and Video-Based Activity and Behavior Computing*. Springer. https://doi.org/10.1007/978-981-19-0361-8_6
- Khasanshin, I. (2021). Application of an artificial neural network to automate the measurement of kinematic characteristics of punches in boxing. *Applied Sciences, 11*(3), 1223. https://doi.org/10.3390/app11031223
- Kimm, D., & Thiel, D. V. (2015). Hand speed measurements in boxing. *Procedia Engineering, 112*, 502–506. https://doi.org/10.1016/j.proeng.2015.07.232
- Manoharan, S., et al. (2025). An active machine learning framework for automatic boxing punch recognition and classification using upper limb kinematics. *PLOS ONE*. https://doi.org/10.1371/journal.pone.0322490
- Manoharan, S. (2025). *Boxing punch data* [Data set]. Zenodo. https://doi.org/10.5281/zenodo.14965635
- Qi, J., Jin, R., Wang, T., Li, Z., Finlay, M. J., & Chen, C. (2026). Reliability and validity of the "XingXun" system for measuring punch acceleration and velocity in elite boxers. *Frontiers in Physiology*. https://doi.org/10.3389/fphys.2026.1726442
- Worsey, M., Espinosa, H., Shepherd, J. B., & Thiel, D. V. (2020). An evaluation of wearable inertial sensor configuration and supervised machine learning models for automatic punch classification in boxing. *IoT, 1*(2), 21. https://doi.org/10.3390/iot1020021
- Boxing punch detection and classification using Motion Tape and machine learning. (2025). *Sensors, 25*(16), 5027. https://doi.org/10.3390/s25165027
- Reliability of a low-cost inertial measurement unit (IMU) to measure punch and kick velocity. (2025). *Sensors, 25*(2), 307. https://doi.org/10.3390/s25020307
- Limb biomechanics in combat sports: insights from wearable sensor technology. (2025). *Frontiers in Bioengineering and Biotechnology*. https://doi.org/10.3389/fbioe.2025.1663592
- SensiML. (2021). Building a TinyML application with TF Micro and SensiML. TensorFlow Blog. https://blog.tensorflow.org/2021/05/building-tinyml-application-with-tf-micro-and-sensiml.html

*(Author lists marked with only a title need completing from the DOI pages. My fetcher couldn't open some publisher pages, and I didn't want to guess author names.)*
