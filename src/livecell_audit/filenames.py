"""Parse LIVECell image filenames into separate fields.

Every referenced filename follows the acquisition naming scheme, for example::

    A172_Phase_A7_1_01d04h00m_3.tif
    Huh7_Phase_A10_2_00d00h00m_3.tif
    SKOV3_Phase_G4_1_00d20h00m_3.tif

Fields, in order: cell line, imaging modality (always ``Phase``), microplate
well (row letter + column number, e.g. ``A7``), acquisition field index within
the well, incubation timestamp (``<days>d<hours>h<minutes>m``), and crop index
within the field. The extension is ``.tif`` in the COCO ``file_name`` values
(the companion ``original_filename`` uses ``.png``).
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

_FILENAME_RE = re.compile(
    r"^(?P<cell_line>[A-Za-z0-9]+)"
    r"_Phase_"
    r"(?P<well>[A-H][1-9][0-9]*)"
    r"_(?P<field>\d+)"
    r"_(?P<days>\d+)d(?P<hours>\d+)h(?P<minutes>\d+)m"
    r"_(?P<crop>\d+)"
    r"\.(?P<extension>tif|tiff|png)$"
)


def parse_filename(file_name: Any) -> Optional[Dict[str, Any]]:
    """Parse a COCO ``file_name`` into its acquisition fields.

    Returns a dict with ``cell_line``, ``well``, ``well_row``, ``well_col``,
    ``field``, ``timestamp`` (raw ``01d04h00m`` form), ``days``, ``hours``,
    ``minutes``, ``time_hours`` (total incubation time in hours as a float),
    ``crop`` and ``extension``. Returns ``None`` when the value does not match
    the naming scheme (callers record these as unparseable rather than
    failing the whole run).
    """
    if not isinstance(file_name, str):
        return None
    match = _FILENAME_RE.match(file_name)
    if match is None:
        return None
    groups = match.groupdict()
    days = int(groups["days"])
    hours = int(groups["hours"])
    minutes = int(groups["minutes"])
    return {
        "cell_line": groups["cell_line"],
        "well": groups["well"],
        "well_row": groups["well"][0],
        "well_col": int(groups["well"][1:]),
        "field": int(groups["field"]),
        "timestamp": f"{groups['days']}d{groups['hours']}h{groups['minutes']}m",
        "days": days,
        "hours": hours,
        "minutes": minutes,
        "time_hours": days * 24.0 + hours + minutes / 60.0,
        "crop": int(groups["crop"]),
        "extension": groups["extension"],
    }
