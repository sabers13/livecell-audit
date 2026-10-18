"""The nine previously reported sparse-label cases used as regression examples.

These image records were reported upstream in
``sartorius-research/LIVECell`` issue #21 ("A few images have incomplete
annotations.", opened by ``ivan-ea`` on 13 October 2022 with a threshold of
ten segments per image; a Sartorius maintainer replied that the images had
slipped through QA) and re-verified locally in
``docs/02_upstream_bugs_and_findings.md``.

They are stored here as *identities* (filename, split, image id) only. The
annotation counts are never stored here: the audit derives every count from
the raw COCO files and checks that each of these records is recovered by the
simple "at most N annotations" baseline rule. They are labelled review
candidates and regression examples -- recovering them says the rule still
fires on known examples, nothing about ML generalisation.
"""

from __future__ import annotations

from typing import Dict, List

UPSTREAM_ISSUE = "https://github.com/sartorius-research/LIVECell/issues/21"
UPSTREAM_REPORTER = "ivan-ea"

KNOWN_SPARSE_CASES: List[Dict[str, object]] = [
    {"file_name": "BT474_Phase_B3_1_03d00h00m_3.tif", "split": "train", "image_id": 10517},
    {"file_name": "A172_Phase_A7_1_01d04h00m_3.tif", "split": "train", "image_id": 150535},
    {"file_name": "A172_Phase_D7_1_01d20h00m_1.tif", "split": "train", "image_id": 205798},
    {"file_name": "BV2_Phase_C4_1_01d16h00m_3.tif", "split": "train", "image_id": 628256},
    {"file_name": "BV2_Phase_D4_1_00d12h00m_2.tif", "split": "train", "image_id": 718286},
    {"file_name": "SHSY5Y_Phase_D10_1_01d16h00m_4.tif", "split": "train", "image_id": 1494964},
    {"file_name": "SkBr3_Phase_H3_2_00d00h00m_1.tif", "split": "val", "image_id": 1248961},
    {"file_name": "BV2_Phase_A4_2_00d00h00m_1.tif", "split": "test", "image_id": 976048},
    {"file_name": "BV2_Phase_A4_2_02d04h00m_3.tif", "split": "test", "image_id": 1007442},
]
