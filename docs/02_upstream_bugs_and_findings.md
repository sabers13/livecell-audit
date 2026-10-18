# 02. Upstream Issues & Empirical Findings in LIVECell

## 1. LIVECell Benchmark Overview

- **Source Paper:** *LIVECell—A large-scale dataset for label-free live-cell segmentation* (Edlund et al., *Nature Methods* 18, 1038–1045, 2021; PubMed 34462594, DOI 10.1038/s41592-021-01249-6).
- **Authors:** Sartorius Corporate Research (Umeå, Sweden), Sartorius BioAnalytics (Royston, UK) and the German Research Center for Artificial Intelligence (DFKI, Saarbrücken), with Umeå University.
- **Data Details:**
  - 5,239 distinct phase-contrast microscopy images, 704 × 520 pixels.
  - 1,662,447 individual cell annotations.
  - 8 cell lines: A172, BT-474, BV-2, Huh-7, MCF-7, SH-SY5Y, SK-BR-3, SK-OV-3.
  - Standard split: Train (3,253 records), Val (570 records), Test (1,564 records).
- **Official Repository:** [github.com/sartorius-research/LIVECell](https://github.com/sartorius-research/LIVECell) (215 stars in October 2026).

---

## 2. Empirical Verification on Downloaded Splits

Running the audit directly against the official `livecell_coco_{train,val,test}.json` produced these numbers. They were reproduced a second time with independent code on 18 October 2026.

### Summary Table

| Metric | Validation Set | Test Set | Training Set |
|---|---:|---:|---:|
| **Total Image Records** | 570 | 1,564 | 3,253 |
| **Unique Image IDs** | 570 | 1,564 | 3,253 |
| **Unique `file_name` Entries** | 569 | 1,512 | 3,188 |
| **Extra records for an already-listed filename** | 1 | 52 | 65 |
| **Total Annotations** | 181,610 | 462,261 | 1,018,576 |
| **Images with ≤ 10 Annotations** | 1 | 2 | 6 |

Image ids are unique within each split and across splits: there are no id collisions. Every repeated filename has its own id and its own, different set of annotation polygons.

---

## 3. Upstream Issues

### Issue #26: Duplicate images with different IDs + train and validation sets not mutually exclusive
- **Reported:** 8 April 2023 by community member `OasisArtisan`.
- **Current Status:** Open, no replies (October 2026).
- **Reproduced here:**
  1. **Train/validation overlap:** exactly **30 image filenames occur in both `train.json` and `val.json`**; none occurs in `test.json`. All 30 are Huh7 or SKOV3 images.
     - *Examples:*
       - `Huh7_Phase_A10_2_00d00h00m_3.tif`
       - `Huh7_Phase_A10_2_00d12h00m_1.tif`
       - `SKOV3_Phase_G4_1_00d20h00m_3.tif`
       - `SKOV3_Phase_H4_1_00d16h00m_1.tif`
     - *What they are:* in all 30 cases the training and validation records carry different annotation sets (different cell counts and polygons). They are the dual annotations the authors describe in issue #14, with one version in each split.
     - *Impact:* a model trained on the official training split and validated on the validation split sees 30 of its 569 validation photos during training, with a different annotator's labels. This affects validation-based model selection. The official test split is not affected, and the effect on any reported score has not been measured here.
  2. **Within-split repeats (alternate annotation versions):**
     - 65 extra records in `train.json` (e.g. `Huh7_Phase_A10_2_00d16h00m_4.tif` as ID `742297` and ID `1012330`).
     - 52 extra records in `test.json`.
     - 1 extra record in `val.json` (`Huh7_Phase_A10_2_00d04h00m_2.tif` as `876543` and `1037056`).

### Issue #21: A few images have incomplete annotations
- **Reported:** 13 October 2022 by `ivan-ea`, using a threshold of 10 segments per image.
- **Current Status:** Open. Rickard Sjögren (Sartorius) replied that "some images are missing annotations and have simply slipped through QA" and that excluding them from training is probably the best option. A later thread with `StefanBaar` discussed unannotated ambiguous regions (a documented design choice) and polygon-to-mask conversion.
- **Reproduced here:** all nine reported images have 10 or fewer annotations in the official files, for example:
    - `A172_Phase_D7_1_01d20h00m_1.tif` (ID 205798): 1 annotation.
    - `BT474_Phase_B3_1_03d00h00m_3.tif` (ID 10517): 1 annotation.
    - `A172_Phase_A7_1_01d04h00m_3.tif` (ID 150535): 1 annotation.
    - `SkBr3_Phase_H3_2_00d00h00m_1.tif` (ID 1248961): 1 annotation.
    - `BV2_Phase_A4_2_00d00h00m_1.tif` (ID 976048): 2 annotations.
  - The issue listed the original `.png` names; the COCO files use `.tif`.
  - *Cause:* not established beyond the maintainer's statement that the images slipped through QA.
  - *Impact:* sparse labels give a model wrong negative examples. The size of the effect on training or evaluation has not been measured here.

### Issue #25: No-sign S3 command
- **Reported:** 7 April 2023 by `ctr26`.
- **Content:** suggests documenting a download command that needs no AWS credentials:
  ```bash
  aws s3 sync s3://livecell-dataset . --region eu-central-1 --no-sign-request
  ```
- Any README change for this should credit `ctr26`.

### Issue #14: Why the JSON count exceeds the image folder count
- **Author Response (Christoffer Edlund, Sartorius):**
  > "As stated in our paper there is a number of images that got annotated twice due to annotation error. Each duplicate annotation will then have its own reference to the same image. Instead of pruning the annotations we selected to keep both versions and disclose which image that has dual annotations, as can be seen in our supplementary table."
- **Consequence:** the dual annotations are disclosed in the paper. Because both versions stay in the public COCO files, some photos appear twice within a split and 30 appear in both train and validation. A user who needs one record per photo, or split-disjoint photos, has to choose a policy explicitly.

---

## 4. What the Audit Adds
The pipeline reproduces these reports from the raw files, records each finding with its evidence, and writes an explicit validation-membership policy (exclude the 30 overlapping validation records) without modifying the source data. It does not correct the dataset, and it does not claim the issues as new discoveries.
