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

Tokens live in `web/review/assets/review.css` (`:root`, with a `prefers-color-scheme: dark` set). HIG level: principles-only.

| Role | Token | Light | Dark | Contrast on paper (light / dark) |
| --- | --- | --- | --- | --- |
| Surface | `--paper` / `--wash` | `#FFFFFF` / `#F8FAFC` | `#0B1220` / `#131C2E` | — |
| Specimen stage | `--stage` | `#0B111C` | `#05080F` | readout text `#CBD5E1` 12.7:1 / 13.5:1 |
| Content | `--ink` | `#0F172A` | `#F1F5F9` | 17.9:1 / 17.1:1 |
| Secondary content | `--body` / `--muted` | `#334155` / `#475569` | `#CBD5E1` / `#94A3B8` | 10.4:1, 7.6:1 / 12.6:1, 7.3:1 |
| Action and focus | `--action` | `#214B85` | `#8FB4FF` | 8.7:1 / 9.0:1 |
| Control boundary | `--control` | `#64748B` | `#64748B` | 4.8:1 / 3.9:1 |
| Cell label (signal) | `--label` | `#FFED00` | `#FFED00` | 15.6:1 on the stage |

- **One colour, one meaning.** Yellow marks supplied cell labels, and the wordmark's highlighter underline is their legend. Interface state (selected tab, pressed buttons, switches, primary buttons) uses ink, never yellow.
- **Typeface.** Atkinson Hyperlegible Next for text and Atkinson Hyperlegible Mono for identifiers, counts and readouts, self-hosted under the SIL OFL. The site is about reading data exactly, and these faces keep 1/l/I and 0/O distinct in filenames, ids and hashes. Scale in rem: display `clamp(2rem, …, 2.75rem)`, h2 1.75rem, h3 1.3125rem, body 1.0625rem (17 px), small 0.9375rem, caption 0.875rem, meta 0.8125rem (mono identifiers only).
- **Signature: the specimen stage.** Every microscopy image sits on a dark stage with a mono readout strip (filename, label count, pixel size), as imaging software shows a specimen. Yellow outlines on it are the only strong colour on the page.
- **Motion.** One orchestrated moment: the first time the overview is shown, the hero image comes into focus, its label appears, and the locator ring closes onto the only labelled cell (about one second, once). Otherwise only brief feedback: the tab bar slides, a newly shown panel rises in, label layers fade in, switches and disclosure chevrons ease. All motion is off under `prefers-reduced-motion`; the translucent tab bar turns opaque under `prefers-reduced-transparency`.
- Left-aligned text, prose under about 72 characters per line, quiet surroundings, no decorative gradients.

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
