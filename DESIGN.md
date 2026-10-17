# LIVECell Audit design decisions

## Audience and purpose

The first reader is a recruiter without a microscopy or machine-learning background. The user explicitly requested a friendly interface that lets HR understand what is happening. A technical reviewer must also be able to inspect the evidence.

Information priority:

1. Explain the task: researchers mark cells in microscope images to create examples for an AI model.
2. Show a real problem and explain why it could affect an experiment.
3. Show evidence and the work behind the check, with technical detail available on request.

## Reference screen and visual direction

- Reference screen: the guided overview and real-image example in `web/prototype/index.html`.
- This is a reviewable interface prototype. It uses verified local observations; the automated audit engine is not implemented.
- Default mode: light, following `docs/04_react_ui_spec.md`.
- Accent: yellow `#FFED00`. Surfaces: white `#FFFFFF` and cool light neutral `#F8FAFC`. Text: `#0F172A` and `#334155`. Secondary text: `#475569`. Divider: `#D9E1E8`.
- Typeface: Inter when available, then system sans-serif. Headings and body are left aligned. Keep prose below about 75 characters per line.
- Use real microscopy images as the distinctive visual element. Quiet surroundings, limited borders, no decorative gradients or progress animations.
- Deliberate departure from the default dense developer-tool direction: the first screen is a guided explanation with larger readable text and fewer controls. Dense evidence belongs below the explanation.
- HIG level: principles-only. Prioritize hierarchy, feedback, keyboard access, contrast, and predictable controls.

## Interaction and content rules

- No audience selector or required onboarding. The understandable version is the default for everyone.
- Show the problem, its consequence, and a real example together.
- Introduce plain wording before technical terminology. Define terms where used.
- Navigation: “Start here”, “See an example”, “How it works”. Technical evidence is an optional disclosure.
- Use native buttons, links, checkboxes, and disclosures. Interaction must work without hover.
- A selected finding updates its explanation, image, status, and recommended next step together.
- Use text labels as well as color. Yellow is an accent and image annotation aid, not a severity scale.
- Do not invent a health percentage or imply a changed dataset has been scientifically corrected.
- Distinguish a verified observation, an interpretation, and a recommended action.
- Preserve alternative annotation versions; a repeated filename is not automatically a corrupt record.
- Never label a drawn explanation marker as a detected cell boundary.
- Keep preview status and snapshot date visible. No working “Run audit” button until an actual audit is connected.

## Quick UI verification

The prototype has no dependency installation or build step. For the UI fast gate, check script syntax, both example selections, the label-overlay control, evidence disclosures, image loading, keyboard operation, and layout at desktop and 360px widths in a live browser. Do not add a test framework just to review this prototype.

## Acceptance criteria

A reader unfamiliar with the field should be able to answer these without opening technical evidence:

1. What is being checked? Images and labels used to teach and evaluate an AI model.
2. What was found? The same 30 images occur in learning and validation groups; nine reported images have very few marked cells.
3. Why does that matter? Evaluation may reuse familiar examples, and incomplete labels can provide unreliable training examples.
4. What does the tool do about it? It records the finding and provides evidence and a recommended review action. The prototype does not claim a completed correction.
5. What did Saber contribute? Independent verification and interface design now; reproducible automation and policy exports when implemented.

These are comprehension goals. A real recruiter usability check remains to be performed; browser checks alone do not establish that the interface is understandable.
