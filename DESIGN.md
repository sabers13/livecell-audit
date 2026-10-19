# LIVECell Audit design decisions

These rules govern the review website in `web/review/` (described in
`docs/11_review_interface.md`).

## Audience and purpose

The first reader is a non-specialist without a microscopy or machine-learning background; a technical reviewer must also be able to inspect the evidence.

Information priority:

1. Explain what was built in one sentence, and show the headline findings as KPI tiles with their practical meaning and upstream origin.
2. Show a compact real microscope example beside the introduction, then offer "Two examples in detail" and a short "What I built" card in the overview grid.
3. Offer four sections: Overview, Images, Methods and Data.

The overview combines the introduction and a complete, uncropped microscope preview capped at 300 px wide; the preview stacks below the text on phones. KPI tiles, one chart and two progress bars replace raw numbers in prose. A compact two-state example card occupies the former example's grid position beside an equally wide and tall "What I built" card. Its native button selector shows either the same photo with both complete label versions or the sparse image with its supplied label and locator. Switching keeps the card height stable. Explore this example opens the matching full case study in Images, where label controls and evidence disclosures remain. The introduction text aligns with the preview's top edge; Review images sits beside the preview heading. Images opens the image browser immediately, with an image selector, a next-image button and a supplied-label toggle; search, filters and detailed counts are optional disclosures. Methods holds the comparison as tiles, bars and tables; Data holds the full rankings, downloads and optional provenance.

## Visual direction

The outer page follows the portfolio project template in `sabers13/sabers13.github.io/hicp-eurostat.html`: a fixed, full-width translucent white header, blue Saber Sojudi home link, gray navigation with a blue selection underline, Back link, and a pale gray background without an enclosing rounded shell. The project pill shows the LIVECell Audit wordmark. The interior retains the dashboard cards, split icon rail, gradient hero tile, hatched bars, blur-in entrance and count-up figures. Tokens live in `web/review/assets/review.css` (`:root` and an explicit `[data-theme="dark"]` set). Every page opening starts in light mode, including on a dark OS or after a previous dark selection; appearance buttons switch only the current visit.

| Role | Token | Light | Dark | Notes |
| --- | --- | --- | --- | --- |
| Page / shell / card / inset | `--bg-page` … `--bg-inset` | `#F9FAFB` / `#F9FAFB` / `#FFFFFF` / `#F3F4F6` | `#030712` / `#0B0F19` / `#111827` / `#1A2232` | Portfolio project background; depth comes from tone, not shadows |
| Content | `--ink` / `--ink-2` / `--ink-muted` | `#111827` / `#374151` / `#6B7280` | `#F9FAFB` / `#D1D5DB` / `#9CA3AF` | muted is 4.8:1 on white |
| Accent | `--accent` | `#2563EB` (portfolio blue-600) | same | one hero tile per section, the label switch, the default method's bar, the notification dot |
| Hero gradient | `--accent-gradient` | blue-600 → blue-700 → violet-700 | same | white text is 5.2:1 or more on every stop; the soft highlight sits behind the tile icon, not text; the violet end echoes the portfolio's purple gradient text |
| Soft chips | `--accent-soft` / `--on-accent-soft` | `#DBEAFE` / `#1E40AF` | translucent blue / `#BFDBFE` | the portfolio's blue-100 / blue-800 chip |
| Status chips | `--success*`, `--danger*` | green-700 on green-100, red-700 on red-100 | light text on translucent tint | always with text, never colour alone |
| Cell label (signal) | `--label` | `#FFED00` | `#FFED00` | label yellow; 15.6:1 on the stage |
| Specimen stage | `--stage` | `#0B111C` | `#05080F` | readout text `#CBD5E1` |

- **One colour, one meaning.** Yellow marks supplied cell labels and the LIVECell tag's highlighter underline. Blue is the portfolio accent and header navigation underline; primary buttons and the selected rail icon are ink (white in dark mode).
- **Typeface.** Inter (variable, latin subset, self-hosted under the SIL OFL), as on the portfolio, with `cv11`/`ss01` and tabular figures for every number; filenames, ids and hashes use the system monospace. Title 40 px/600/-0.03em, KPI 34 px/600/-0.02em, card title 18 px/600, body 15 px.
- **Signature: the specimen stage.** Every microscopy image sits on a dark stage with a mono readout strip (filename, label count, pixel size), as imaging software shows a specimen. Yellow outlines on it are the only yellow on the page apart from the legend.
- **Motion.** Cards enter blur → sharp with a 60 ms stagger each time a section is shown; big figures count up and bars fill the first time; the nav pill slides; the appearance switch wipes in as a circle from the pressed button. The first time the overview is shown, the hero image comes into focus, its label appears and the locator ring closes onto the only labelled cell. The chrome never animates on a section change. All of it is off under `prefers-reduced-motion` (figures show their final value at once).
- Left-aligned text, prose under about 75 characters per line, no drop shadows on light-mode cards.

## Interaction and content rules

- The overview introduction uses portfolio-style icon badges for Python, pandas, scikit-learn, data pipelines and data quality. Each badge names a skill the project implements. Do not add cloud or lab-team collaboration claims without project evidence. The introduction links to `https://github.com/sabers13/livecell-audit` and the portfolio's `#contact` section through “View on GitHub” and “Hire Me”.
- No audience selector or required onboarding. The understandable version is the default for everyone.
- Show the problem, its consequence and a real example together. Introduce plain wording before technical terms, and define terms where used.
- Navigation is four accessible tabs with arrow-key and Home/End support, mirrored by the icon rail; the selection is kept in the URL and browser history.
- Native buttons, links, checkboxes and disclosures only; nothing depends on hover.
- A selected example updates its image, status, finding and recommended next step together.
- Text labels accompany colour. Yellow is an accent and an annotation aid, not a severity scale.
- Supplied labels are drawn as supplied and never described as cell boundaries detected by this project. A dense image shows all of its supplied outlines, never a subset that could be mistaken for missing labels.
- Do not invent a health percentage, a trend or a delta: every KPI chip is a ratio or difference of figures shown on the page (for example 30 of 569 validation photos), never a change over time. Do not imply that the dataset has been corrected. Distinguish a verified observation, an interpretation and a recommended action.
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
