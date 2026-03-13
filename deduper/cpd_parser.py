from __future__ import annotations

from pathlib import Path
import xml.etree.ElementTree as ET

from .types import DuplicationGroup, DuplicationOccurrence


def _tag_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def parse_cpd_xml(xml_path: Path) -> list[DuplicationGroup]:
    tree = ET.parse(xml_path)
    root = tree.getroot()

    groups: list[DuplicationGroup] = []
    duplication_nodes = [
        node
        for node in root.iter()
        if _tag_name(node.tag) == "duplication"
    ]
    for idx, dup in enumerate(duplication_nodes, start=1):
        lines = int(dup.attrib.get("lines", 0))
        tokens = int(dup.attrib.get("tokens", 0))

        occurrences: list[DuplicationOccurrence] = []
        for file_node in dup:
            if _tag_name(file_node.tag) != "file":
                continue
            path = Path(file_node.attrib["path"])
            line = int(file_node.attrib.get("line", 1))
            column_raw = file_node.attrib.get("column")
            end_line_raw = file_node.attrib.get("endline")
            end_column_raw = file_node.attrib.get("endcolumn")
            occurrences.append(
                DuplicationOccurrence(
                    path=path,
                    line=line,
                    column=int(column_raw) if column_raw is not None else None,
                    end_line=int(end_line_raw) if end_line_raw is not None else None,
                    end_column=int(end_column_raw) if end_column_raw is not None else None,
                )
            )

        fragment_node = next(
            (item for item in dup if _tag_name(item.tag) == "codefragment"),
            None,
        )
        code_fragment = ""
        if fragment_node is not None:
            code_fragment = (fragment_node.text or "").strip("\n")

        groups.append(
            DuplicationGroup(
                id=idx,
                lines=lines,
                tokens=tokens,
                occurrences=occurrences,
                code_fragment=code_fragment,
            )
        )

    return groups
