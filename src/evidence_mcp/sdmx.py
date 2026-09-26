"""Client for SDMX 2.1 REST APIs (default: the OECD's public statistics API).

SDMX is the ISO standard for exchanging official statistics; the OECD, ECB, Eurostat and
IMF all publish through it. Three request types are enough for an assistant:

- the catalogue of dataflows (datasets):       GET {base}/dataflow/all
- the structure of one dataflow:               GET {base}/dataflow/{agency}/{id}/{version}
                                                    ?references=descendants
- the observations for a filtered series key:  GET {base}/data/{agency},{id},{version}/{key}
                                                    ?format=csvfilewithlabels

Structures come back as SDMX-ML (XML). The parsers below match elements by their local
name and ignore XML namespaces, so they keep working across providers and SDMX versions.
Data comes back as CSV, which is compact and easy for a model to read.
"""

from __future__ import annotations

import csv
import io
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlencode

from .http_cache import HttpFetcher, NotFound

XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
MB = 1_000_000


# --------------------------------------------------------------------------- data classes


@dataclass(frozen=True)
class Dataflow:
    agency: str
    id: str
    version: str
    name: str
    description: str = ""


@dataclass
class Dimension:
    id: str
    position: int
    name: str
    codelist: str | None = None
    codes: dict[str, str] = field(default_factory=dict)  # code -> label


@dataclass
class DatasetStructure:
    dataflow: Dataflow
    dimensions: list[Dimension]  # in key order
    time_dimension: str | None = None

    @property
    def key_template(self) -> str:
        return ".".join(d.id for d in self.dimensions)


@dataclass
class DataTable:
    columns: list[str]
    rows: list[list[str]]
    constant_columns: dict[str, str]
    total_rows: int

    @property
    def truncated(self) -> bool:
        return len(self.rows) < self.total_rows


# --------------------------------------------------------------------------- XML helpers


def _local(tag: str) -> str:
    """'{namespace}Name' -> 'Name'."""
    return tag.rsplit("}", 1)[-1]


def _iter_named(root: ET.Element, name: str):
    return (el for el in root.iter() if _local(el.tag) == name)


def _child(el: ET.Element, name: str) -> ET.Element | None:
    return next((c for c in el if _local(c.tag) == name), None)


def _text(el: ET.Element, name: str, lang: str = "en") -> str:
    """Text of the child `name` in `lang`, falling back to the first one present."""
    candidates = [c for c in el if _local(c.tag) == name]
    for c in candidates:
        if c.get(XML_LANG) == lang:
            return (c.text or "").strip()
    return (candidates[0].text or "").strip() if candidates else ""


def _ref(el: ET.Element | None) -> ET.Element | None:
    """The first <Ref> element anywhere below `el`."""
    return None if el is None else next(_iter_named(el, "Ref"), None)


# --------------------------------------------------------------------------- parsers


def parse_dataflows(xml_bytes: bytes) -> list[Dataflow]:
    root = ET.fromstring(xml_bytes)
    flows = []
    for el in _iter_named(root, "Dataflow"):
        if el.get("id") is None:  # skip references to dataflows, keep definitions
            continue
        flows.append(
            Dataflow(
                agency=el.get("agencyID", ""),
                id=el.get("id", ""),
                version=el.get("version", "1.0"),
                name=_text(el, "Name"),
                description=_text(el, "Description"),
            )
        )
    return flows


def parse_structure(xml_bytes: bytes, dataflow_id: str) -> DatasetStructure:
    root = ET.fromstring(xml_bytes)

    flow_el = next(
        (el for el in _iter_named(root, "Dataflow") if el.get("id") == dataflow_id), None
    )
    if flow_el is None:
        raise NotFound(f"Dataflow {dataflow_id} not found in the structure response.")
    dataflow = Dataflow(
        agency=flow_el.get("agencyID", ""),
        id=dataflow_id,
        version=flow_el.get("version", "1.0"),
        name=_text(flow_el, "Name"),
        description=_text(flow_el, "Description"),
    )

    dsd_ref = _ref(_child(flow_el, "Structure"))
    dsd_id = dsd_ref.get("id") if dsd_ref is not None else None
    dsd = next(
        (el for el in _iter_named(root, "DataStructure") if el.get("id") == dsd_id),
        next(_iter_named(root, "DataStructure"), None),
    )
    if dsd is None:
        raise NotFound(f"No data structure definition returned for {dataflow_id}.")

    # Concept names give dimensions human labels, e.g. REF_AREA -> "Reference area".
    concept_names: dict[str, str] = {}
    for scheme in _iter_named(root, "ConceptScheme"):
        for concept in scheme:
            if _local(concept.tag) == "Concept":
                concept_names[f"{scheme.get('id')}:{concept.get('id')}"] = _text(concept, "Name")

    codelists: dict[str, dict[str, str]] = {}
    for cl in _iter_named(root, "Codelist"):
        codes = {c.get("id", ""): _text(c, "Name") for c in cl if _local(c.tag) == "Code"}
        codelists[cl.get("id", "")] = codes

    # Only direct children of <DimensionList> are dimensions. Elsewhere in a DSD (attribute
    # relationships, groups) <Dimension> also appears, but as a reference without an id.
    dimension_list = next(_iter_named(dsd, "DimensionList"), None)
    if dimension_list is None:
        raise NotFound(f"The structure for {dataflow_id} has no dimension list.")

    dimensions: list[Dimension] = []
    time_dimension = None
    for el in dimension_list:
        kind = _local(el.tag)
        if kind == "TimeDimension":
            time_dimension = el.get("id")
        elif kind == "Dimension":
            concept = _ref(_child(el, "ConceptIdentity"))
            concept_key = (
                f"{concept.get('maintainableParentID')}:{concept.get('id')}"
                if concept is not None
                else ""
            )
            enum = _ref(_child(el, "LocalRepresentation"))
            codelist_id = enum.get("id") if enum is not None else None
            dimensions.append(
                Dimension(
                    id=el.get("id", ""),
                    position=int(el.get("position", len(dimensions) + 1)),
                    name=concept_names.get(concept_key) or el.get("id", ""),
                    codelist=codelist_id,
                    codes=codelists.get(codelist_id or "", {}),
                )
            )
    dimensions.sort(key=lambda d: d.position)
    return DatasetStructure(dataflow=dataflow, dimensions=dimensions, time_dimension=time_dimension)


def parse_csv(text: str, max_rows: int) -> DataTable:
    """Parse SDMX-CSV into a compact table.

    Columns whose value is identical on every row (the dataflow id, a single unit, a fixed
    frequency...) are lifted out into `constant_columns`, and empty columns are dropped.
    That typically halves the tokens a model has to read without losing any information.
    """
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    header = next(reader, None)
    if not header:
        return DataTable(columns=[], rows=[], constant_columns={}, total_rows=0)
    all_rows = [row for row in reader if any(cell.strip() for cell in row)]
    total = len(all_rows)

    keep: list[int] = []
    constants: dict[str, str] = {}
    for i, column in enumerate(header):
        values = {row[i] if i < len(row) else "" for row in all_rows}
        if values <= {""}:
            continue  # empty column
        if len(values) == 1 and total > 1 and column not in ("OBS_VALUE", "TIME_PERIOD"):
            constants[column] = values.pop()
            continue
        keep.append(i)

    rows = [[row[i] if i < len(row) else "" for i in keep] for row in all_rows[:max_rows]]
    return DataTable(
        columns=[header[i] for i in keep], rows=rows, constant_columns=constants, total_rows=total
    )


# --------------------------------------------------------------------------- client


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


class SdmxClient:
    def __init__(self, fetcher: HttpFetcher):
        self.fetcher = fetcher
        self.base = fetcher.settings.sdmx_base_url

    # -- catalogue

    def catalogue(self) -> list[Dataflow]:
        url = f"{self.base}/dataflow/all"
        body = self.fetcher.get(url, self.fetcher.ttl.catalogue, max_bytes=80 * MB)
        return parse_dataflows(body)

    def search(self, query: str, limit: int) -> list[Dataflow]:
        """Rank dataflows by how many query words appear in their id, name and description.

        Name matches count double. Simple and transparent; the model refines the query itself.
        """
        terms = _words(query)
        scored = []
        for flow in self.catalogue():
            name_hits = len(terms & _words(flow.name + " " + flow.id))
            desc_hits = len(terms & _words(flow.description))
            score = 2 * name_hits + desc_hits
            if score:
                scored.append((score, flow))
        scored.sort(key=lambda pair: (-pair[0], pair[1].name))
        return [flow for _, flow in scored[:limit]]

    def resolve_version(self, agency: str, dataflow_id: str, version: str) -> str:
        """Turn 'latest' into a concrete version using the (cached) catalogue."""
        if version != "latest":
            return version
        versions = [
            f.version for f in self.catalogue() if f.agency == agency and f.id == dataflow_id
        ]
        if not versions:
            raise NotFound(
                f"No dataflow {agency}:{dataflow_id} in the catalogue. Use search_datasets first."
            )
        return max(versions, key=lambda v: tuple(int(p) for p in v.split(".") if p.isdigit()))

    # -- structure

    def structure(self, agency: str, dataflow_id: str, version: str) -> DatasetStructure:
        version = self.resolve_version(agency, dataflow_id, version)
        url = f"{self.base}/dataflow/{agency}/{dataflow_id}/{version}?references=descendants"
        body = self.fetcher.get(url, self.fetcher.ttl.structure, max_bytes=40 * MB)
        return parse_structure(body, dataflow_id)

    # -- data

    def data_url(
        self,
        agency: str,
        dataflow_id: str,
        version: str,
        key: str,
        start_period: str | None,
        end_period: str | None,
    ) -> str:
        params = {"format": "csvfilewithlabels"}
        if start_period:
            params["startPeriod"] = start_period
        if end_period:
            params["endPeriod"] = end_period
        return f"{self.base}/data/{agency},{dataflow_id},{version}/{key}?{urlencode(params)}"

    def data(
        self,
        agency: str,
        dataflow_id: str,
        version: str,
        key: str,
        start_period: str | None,
        end_period: str | None,
        max_rows: int,
    ) -> tuple[DataTable, str, str]:
        """Return (table, source_url, retrieved_on)."""
        version = self.resolve_version(agency, dataflow_id, version)
        url = self.data_url(agency, dataflow_id, version, key, start_period, end_period)
        try:
            body = self.fetcher.get(url, self.fetcher.ttl.data, max_bytes=25 * MB)
        except NotFound:
            # SDMX services answer 404 when a valid query matches no observations.
            return DataTable([], [], {}, 0), url, date.today().isoformat()
        table = parse_csv(body.decode("utf-8", errors="replace"), max_rows)
        return table, url, date.today().isoformat()
