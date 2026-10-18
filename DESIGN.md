# LIVECell Audit design decisions

These rules govern the review website in `web/review/` (described in
`docs/11_review_interface.md`).

## Audience and purpose

The first reader is a recruiter without a microscopy or machine-learning background; a technical reviewer must also be able to inspect the evidence.

Information priority:

1. Explain what was built in one sentence, with the two headline findings, their practical meaning and their upstream origin.
2. Show one real, already annotated example and a short "What I built" section without requiring a click.
3. Offer three tabs: Overview, Try it and Results.

The overview is a short reading experience, not a dashboard to operate: no selector, overlay switch, search field or ranking control appears by default, and the first screen must explain the work even if the reader never clicks. "Try it" opens a real image immediately, with an image selector, a next-image button and a supplied-label toggle; search, filters and detailed counts are optional disclosures. Results holds the comparison, full rankings, downloads and optional provenance.

## Visual direction

- Light theme. Accent yellow `#FFED00`. Surfaces white `#FFFFFF` and cool light neutral `#F8FAFC`. Text `#0F172A` and `#334155`; secondary text `#475569`; dividers `#D9E1E8`.
- Typeface: Inter when available, then the system sans-serif. Left-aligned headings and body; prose kept below about 75 characters per line.
- Real microscopy images are the distinctive visual element. Quiet surroundings, limited borders, no decorative gradients or progress animations.
- The first screen is a guided explanation with larger readable text and few controls; dense evidence sits below or behind disclosures.
- Apple HIG at principles level: hierarchy, feedback, keyboard access, contrast and predictable controls.

## Interaction and content rules

- No audience selector or required onboarding. The understandable version is the default for everyone.
- Show the problem, its consequence and a real example together. Introduce plain wording before technical terms, and define terms where used.
- Navigation is three accessible tabs with arrow-key and Home/End support; the selection is kept in the URL and browser history.
- Native buttons, links, checkboxes and disclosures only; nothing depends on hover.
- A selected example updates its image, status, finding and recommended next step together.
- Text labels accompany colour. Yellow is an accent and an annotation aid, not a severity scale.
- Supplied labels are drawn as supplied and never described as cell boundaries detected by this project. A dense image shows all of its supplied outlines, never a subset that could be mistaken for missing labels.
- Do not invent a health percentage or imply that the dataset has been corrected. Distinguish a verified observation, an interpretation and a recommended action.
- Credit upstream reports where a finding is shown; a reproduced issue is not presented as a discovery.
- Preserve alternative annotation versions; a repeated filename is not automatically a corrupt record.
- State the limits of an evaluation next to its result (for example, that both tests are defined by label count).
- Keep the snapshot date visible. No "Run audit" control: the page does not run an audit.
- Sources use a compact count/date summary; full paths, sizes and checksums sit in a closed disclosure with a bounded scroll region.

## Comprehension goals

A reader unfamiliar with the field should be able to answer these without opening technical evidence:

1. What is being checked? Images and labels used to teach and evaluate an AI model.
2. What was found? The same 30 photos occur in the learning and checking groups; nine previously reported images have very few marked cells.
3. Why does that matter? Validation may reuse familiar photos, and incomplete labels give a model unreliable examples.
4. What does the tool do about it? It records each finding with evidence and a recommended review action; it does not correct the dataset.
5. What did Saber contribute? Independent reproduction of the findings, a warehouse and feature analysis, a review ranking with an honest evaluation, reproducible release automation, automated checks and this interface.

These are design goals. A real recruiter usability check has not been performed; browser checks alone do not establish that the interface is understandable.
