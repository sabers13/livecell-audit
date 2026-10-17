# 04. Recruiter-friendly interface specification

## Product requirement

The user wants HR to understand what is happening with as little specialist knowledge as possible. The primary experience is a guided explanation with real examples. Technical reviewers can open the evidence within the same page.

Decisions are recorded in `../DESIGN.md`. The working reference is `../web/prototype/index.html`: an interactive preview based on verified local observations, not a completed audit engine.

## Comprehension goals

Within roughly 20 seconds, a visitor should understand:

> Researchers mark cells in microscope images so an AI model can learn to recognise them. This project checks those images and labels before they are used in an experiment.

Within roughly one minute, they should understand one real finding, its possible consequence, and the evidence supporting it. These timings are design targets, not measured usability results.

## One guided page with progressive detail

### 1. Start here

- Title: “A quality check for the images that teach AI.”
- Explain images, cell markings, and machine learning in two short sentences.
- Use a simple sequence: “Cell images” → “Quality checks” → “Evidence to review”.
- Main action: “See a real example”. Secondary: “How it works”.
- Visible context: “Interactive preview · verified dataset snapshot · 17 October 2026”.
- Do not lead with 1.66 million annotations, a health gauge, the tool stack, an architecture diagram, or check codes.

### 2. See an example

Use two native buttons to switch examples. No audience mode or forced tour.

**Example A: “Same image in two groups”**

- Finding: “30 images were used for both learning and checking.”
- Learning group: examples used to train the model. Checking group: validation images used to check progress.
- Show the same real image in both groups, with accessible captions.
- Consequence: “The model may appear more capable because it has already seen some evaluation images.”
- Boundary: “The effect on model scores has not been measured.”
- Next step: “Keep each source image in one group for this evaluation protocol, and record the selection policy.”
- Introduce the technical term afterward: “This overlap is a form of data leakage.”

**Example B: “Very few marked cells”**

- Finding: “This image contains many visible cells, but its file marks only one.”
- Use the real A172 image and supplied polygon for image ID 150535.
- A checkbox named “Show the existing cell label” toggles the overlay.
- Distinguish the supplied polygon from an enlarged circle or pointer used only to make it visible.
- Consequence: “Incomplete labels can give a model unreliable examples.”
- Next step: “Review the annotation before using this image.”
- Boundary: “A low label count alone is not proof of an error.”
- The nine upstream cases are previously reported examples. Recovering them is a regression check, not proof of a successful ML detector.

### 3. Explore the evidence

Make this an optional disclosure or explicit “View the evidence” action.

The implemented application should support search by filename and filters for finding type, review status, and cell line. Readable finding names appear first; check codes such as C4 belong in technical details. Each finding opens the real image, a short explanation, original record references, and the proposed action.

Distinguish “Confirmed overlap”, “Needs label review”, and “Alternative annotation versions”. They are not all errors. Retain original IDs and raw provenance. Exports must describe their policy and audit run; no “Download fixed dataset” button without a real validated output.

| Observation | Count | Plain wording |
|---|---:|---|
| Distinct image filenames / local files | 5,239 | Cell images in this snapshot |
| COCO image records across splits | 5,387 | Image records, not distinct photos |
| Annotation records | 1,662,447 | Supplied individual cell markings |
| Shared train / validation filenames | 30 | Images present in both groups |
| Train / test or validation / test filename overlaps | 0 | No filename overlap found with the test split |
| Extra within-split records for repeated filenames | 118 | Multiple annotation versions need an explicit policy |
| Previously reported images with at most 10 annotations | 9 | Known examples for label review |

### 4. How it works

Explain the intended pipeline as three actions:

1. **Read the source files.** Match the microscope images to files describing their cell labels.
2. **Check for problems.** Look for reused images, missing files, and labels that need review.
3. **Make the evidence usable.** Record findings and export a report or a derived split with its policy.

Show completion honestly. Independent snapshot verification and this prototype exist. A complete rerunnable audit, automated tests, reviewed exports, and ML evaluation remain planned until implemented.

Optional technical details can expose the data model, validation rules, runtime, memory use, reproducibility metadata, repository link, and upstream references.

## Language mapping

| Technical term | First explanation |
|---|---|
| Dataset | A collection of images and their cell labels |
| Annotation | A marking that tells the model where a cell is |
| Train split | Images the model learns from |
| Validation split | Images used to check the model's progress |
| Data leakage | The same image was used for learning and checking |
| Duplicate record | The same photo has more than one record; label versions may differ |
| Quarantine | Set aside for review |
| Pipeline | Repeatable steps that read, check, and report the data |
| Schema | The expected structure of the source files |

Preserve accuracy: validation is not the separate final test set; an observation is not a measured accuracy improvement; a review candidate is not an automatically confirmed defect.

## Visual and accessibility rules

- Follow the existing yellow, white, and slate palette. No Sartorius logo.
- Real microscopy imagery and restrained annotation overlays provide the visual identity.
- Comfortable body copy around 16–18px; readable secondary labels.
- Semantic headings, visible focus, native controls, and image text alternatives.
- Touch controls around 44px tall; no hover-only explanations.
- At 360px, stack paired image panels with their captions. Prevent page overflow.
- Respect reduced motion. No looping demo, forced tour, or loading animation without real work.
- No fake progress, invented success percentages, or generic health scores.

## Implementation states

- Loading: name the actual operation, such as “Reading image references”. Show counts only when supplied by the pipeline.
- No findings: “No findings from the checks that ran.” List scope and checks; do not imply error-free data.
- Missing image: “This image could not be opened. Its record is still available below.” Preserve the rest of the report.
- Unavailable source: “The dataset files could not be read. Check the source location and try again.”
- Sample preview: label it. Never present an illustrated selection as the full audit output.

## Verification

Check both examples, annotation toggle, disclosures, assets, keyboard access, and desktop/mobile layout in a browser. There is no frontend build configuration yet; do not install a test framework solely for the prototype.

Before calling the interface recruiter-tested, ask someone unfamiliar with the project to explain what is checked, what was found, why it matters, and what Saber actually built. If they need technical details to answer, simplify the first layer.
