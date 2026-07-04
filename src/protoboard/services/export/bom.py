# SPDX-License-Identifier: MIT
"""Bill-of-Materials CSV exporter."""

from __future__ import annotations

import csv
import io
import uuid

from protoboard.domain.components.definition import ComponentDefinition
from protoboard.domain.components.instance import ComponentInstance
from protoboard.domain.layout.layout import Layout

_FIELDS = ["RefDes", "Value", "Footprint", "Description"]


def export_bom_csv(
    layout: Layout,
    components: dict[str, ComponentDefinition],
    instances: dict[uuid.UUID, ComponentInstance],
) -> str:
    """Return a CSV string with one row per placed component, sorted by RefDes."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_FIELDS, lineterminator="\n")
    writer.writeheader()

    rows: list[dict[str, str]] = []
    for comp_id, placement in layout.placements.items():
        inst = instances.get(comp_id)
        if inst is None:
            continue
        defn = components.get(inst.definition_ref)
        # Prefer the placed variant's address ("DO-35/typ"), then the
        # definition's family ref; the generic type name is the last resort.
        fp_name = ""
        if defn is not None:
            fp_name = (inst.package_variant or defn.package_family_ref
                       or type(defn.default_footprint()).__name__)
        desc = defn.description if defn is not None else ""
        rows.append(
            {
                "RefDes": inst.refdes,
                "Value": inst.value,
                "Footprint": fp_name,
                "Description": desc,
            }
        )

    for row in sorted(rows, key=lambda r: r["RefDes"]):
        writer.writerow(row)

    return buf.getvalue()
