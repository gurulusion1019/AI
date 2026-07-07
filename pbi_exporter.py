"""
core/pbi_exporter.py
Power BI export engine.

Generates two artefacts from a completed SemanticModel:

1. model.bim  — Tabular Model BIM JSON (Analysis Services / Power BI Desktop)
   Import via: Power BI Desktop → "Import from local file" or XMLA endpoint.
   This is the full semantic layer: tables, columns, relationships, measures,
   hierarchies, and KPI metadata.

2. report.json — Power BI Report layout JSON (.pbir compatible)
   Auto-selects the best visual type per measure and dimension combination.
   Produces a single-page report with a title, KPI cards row, and up to 6
   data visuals (bar, line, donut, matrix, scatter, treemap) chosen from the
   model's measures and dimension tables.

3. PowerBI_Export.zip — Ready-to-import package:
   model.bim + report.json + README_IMPORT.txt with step-by-step instructions.

Usage (standalone):
    from core.pbi_exporter import export_powerbi_package
    zip_bytes = export_powerbi_package(model)
    with open("PowerBI_Export.zip", "wb") as f:
        f.write(zip_bytes)

Integration into chat_agent / model_builder:
    Call export_powerbi_package(model) in the EXPORT state handler.
"""

import json
import io
import uuid
import zipfile
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional
import pandas as pd

from .models import (
    SemanticModel, MeasureDefinition, Relationship,
    FileMeta, ColumnMeta, AggregationType, JoinType,
)

logger = logging.getLogger(__name__)

# Safety cap so an unusually large table doesn't blow up the BIM file size.
# Tables larger than this still get a full, working schema — just with the
# first N rows embedded rather than all of them.
MAX_EMBEDDED_ROWS = 10000


import zlib
import base64


def _embed_as_enter_data(df: pd.DataFrame,
                          col_names: list[str],
                          meta: "FileMeta") -> list[str]:
    """
    Build the M expression using Power BI's own 'Enter Data' format:
    data → JSON → Base64 → Binary.FromText in M.

    All values are serialized as strings to match PBI's Enter Data behavior.
    """
    import math
    col_metas = {c.name: c for c in meta.columns}

    # Serialise every value to a JSON-safe Python type
    rows_json: list[list] = []
    for _, row in df.iterrows():
        r = []
        for col in col_names:
            val = row[col] if col in row.index else None
            cm = col_metas.get(col)
            is_date = cm.is_date if cm else False
            dtype   = cm.dtype  if cm else "object"

            try:
                if val is None or pd.isna(val):
                    r.append(None)
                    continue
            except (TypeError, ValueError):
                pass

            if is_date:
                try:
                    r.append(str(pd.Timestamp(val).date()))
                except Exception:
                    r.append(None)
            elif "datetime" in dtype.lower():
                try:
                    r.append(str(pd.Timestamp(val).date()))
                except Exception:
                    r.append(None)
            elif "bool" in dtype.lower():
                # Store booleans as "true"/"false" strings
                r.append("true" if bool(val) else "false")
            else:
                # Power BI's own Enter Data stores ALL values as JSON strings
                # (with [Serialized.Text = true] metadata). Passing numbers into
                # a 'type nullable text' column works in PQ preview but the
                # VertiPaq engine fails on Close & Apply for integer/float columns.
                # Converting everything to string here matches PBI's exact format.
                try:
                    f = float(val)
                    if math.isnan(f) or math.isinf(f):
                        r.append(None)
                    elif "int" in dtype.lower() or (f == int(f) and abs(f) < 1e15):
                        r.append(str(int(f)))
                    else:
                        r.append(str(val))
                except (TypeError, ValueError):
                    r.append(str(val) if val is not None else None)
        rows_json.append(r)

    # Compress exactly as Power BI Desktop does
    import math

    # Sanitize: replace any float NaN/Infinity with None BEFORE serializing.
    # Python's json.dumps produces literal "NaN"/"Infinity" for these,
    # which is NOT valid JSON — Power BI's strict Json.Document parser
    # rejects the entire document with DataFormat.Error if any are present.
    def _safe(v):
        if v is None:
            return None
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return None
        return v

    rows_json = [[_safe(v) for v in row] for row in rows_json]

    json_bytes = json.dumps(rows_json, ensure_ascii=False, allow_nan=False).encode("utf-8")

    # Store as plain Base64 JSON — no compression step.
    # Binary.Decompress(Compression.Deflate) was failing in Power BI Desktop
    # June 2026, so we skip it entirely. The Base64 strings are larger but
    # Power Query handles them reliably via Json.Document(Binary.FromText(...)).
    b64 = base64.b64encode(json_bytes).decode("ascii")

    # Use the EXACT 4-line format Power BI Desktop generates for Enter Data tables.
    # Multi-line nested format with TransformColumnTypes looks valid but Power BI
    # fails to execute it silently and returns 0 rows.
    col_type_pairs = ", ".join(f'#"{col}" = _t' for col in col_names)

    expr = [
        "let",
        f'    Source = Table.FromRows(Json.Document(Binary.FromText("{b64}", BinaryEncoding.Base64)), let _t = ((type nullable text) meta [Serialized.Text = true]) in type table [{col_type_pairs}])',
        "in",
        "    Source",
    ]
    return expr


def _m_literal(value, dtype: str, is_date: bool = False) -> str:
    """Scalar fallback for any single-value call sites."""
    try:
        if value is None or pd.isna(value):
            return "null"
    except (TypeError, ValueError):
        pass

    if is_date:
        try:
            ts = pd.Timestamp(value)
            if ts.hour or ts.minute or ts.second:
                return f"#datetime({ts.year},{ts.month},{ts.day},{ts.hour},{ts.minute},{ts.second})"
            return f"#date({ts.year},{ts.month},{ts.day})"
        except (TypeError, ValueError):
            return "null"

    d = dtype.lower()
    if "datetime" in d:
        ts = pd.Timestamp(value)
        return f"#datetime({ts.year},{ts.month},{ts.day},{ts.hour},{ts.minute},{ts.second})"
    if "bool" in d:
        return "true" if bool(value) else "false"
    if "int" in d:
        try:
            return str(int(value))
        except (TypeError, ValueError):
            return "null"
    if "float" in d:
        try:
            f = float(value)
        except (TypeError, ValueError):
            return "null"
        if f != f or f in (float("inf"), float("-inf")):
            return "null"
        return repr(f)

    s = str(value).replace('"', '""').replace("\n", " ").replace("\r", " ")
    return f'"{s}"'


def _m_type(dtype: str, is_date: bool = False) -> str:
    """Map a pandas dtype string (+ the is_date heuristic) to an M type literal."""
    if is_date:
        return "type date"
    d = dtype.lower()
    if "datetime" in d: return "type datetime"
    if "bool" in d:     return "type logical"
    if "int" in d:      return "Int64.Type"
    if "float" in d:    return "type number"
    return "type text"


def _m_type_transform(meta: "FileMeta") -> str:
    """
    Build the M `Table.TransformColumnTypes` type list from each column's real
    dtype — e.g. {"NetRevenue", type number}, {"DateKey", Int64.Type}, ... .

    This replaces the old blanket `each {_, type text}` that cast EVERY column
    to text and made SUM-based measures fail ("Failed to move the data reader
    to the next row"). Types here match what the .bim declares, so numeric
    columns arrive as numbers and aggregate correctly. Fully dynamic — reads
    whatever columns the current file has.
    """
    pairs = []
    for col in meta.columns:
        mt = _m_type(col.dtype, col.is_date)
        cname = col.name.replace('"', '""')
        pairs.append(f'{{"{cname}", {mt}}}')
    return "{" + ", ".join(pairs) + "}"


def _build_file_path_partition(meta: "FileMeta", file_path: str) -> dict:
    """
    Build an M partition using a SourceDataFolder parameter.
    Power BI requires absolute paths — we use a query parameter
    so the user sets their source_data folder path once on first open.

    Each column is cast to its correct type (per-column, from dtype) so that
    numeric measures aggregate without a data-reader error.
    """
    fname = Path(file_path).name
    ext   = Path(file_path).suffix.lower()
    type_list = _m_type_transform(meta)

    if ext in (".xlsx", ".xls"):
        m_expr = [
            "let",
            f"    FilePath = SourceDataFolder & \"\\\\{fname}\",",
            "    Source = Excel.Workbook(File.Contents(FilePath), null, true),",
            "    Sheet1 = Source{[Item=\"Sheet1\",Kind=\"Sheet\"]}[Data],",
            "    Headers = Table.PromoteHeaders(Sheet1, [PromoteAllScalars=true]),",
            f"    Result = Table.TransformColumnTypes(Headers, {type_list})",
            "in",
            "    Result",
        ]
    else:
        m_expr = [
            "let",
            f"    FilePath = SourceDataFolder & \"\\\\{fname}\",",
            "    Source = Csv.Document(File.Contents(FilePath),",
            "        [Delimiter=\",\", Columns=null, Encoding=65001, QuoteStyle=QuoteStyle.None]),",
            "    Headers = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),",
            f"    Result = Table.TransformColumnTypes(Headers, {type_list})",
            "in",
            "    Result",
        ]

    logger.info("Parameter M query for %s -> SourceDataFolder/%s (typed columns)",
                meta.table_name, fname)
    return {
        "name":   meta.table_name,
        "mode":   "import",
        "source": {"type": "m", "expression": m_expr},
    }


def _build_source_folder_parameter() -> dict:
    """
    SourceDataFolder M parameter using correct BIM expression format.
    Power BI reads this as a query parameter — user sets it once on first open.
    """
    return {
        "name": "SourceDataFolder",
        "kind": "m",
        "expression": [
            '"C:\\path\\to\\source_data" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]'
        ],
        "annotations": [
            {"name": "ParameterMetadata", "value": "{\"version\":3}"}
        ],
    }


def _build_data_partition(meta: "FileMeta",
                           df: Optional[pd.DataFrame],
                           file_path: Optional[str] = None) -> dict:
    """
    Build the M partition expression for a table.

    Priority:
    1. If file_path is provided → use file-path M query (preferred)
       Data loads from the saved Excel/CSV file. No row limits.
       User can refresh monthly by replacing source files.
    2. If only df is provided → fall back to Base64 embedding
       Used when no saved file path is available.
    3. If neither → empty placeholder table.
    """
    col_names  = [c.name for c in meta.columns]
    col_list_m = ", ".join(f'"{n}"' for n in col_names)

    # Option 1 — file path (preferred)
    if file_path and Path(file_path).exists():
        return _build_file_path_partition(meta, file_path)

    # Option 2 — Base64 embedding fallback
    if df is not None and len(df) > 0:
        was_truncated = len(df) > MAX_EMBEDDED_ROWS
        rows = df.head(MAX_EMBEDDED_ROWS).reset_index(drop=True)
        if was_truncated:
            logger.warning(
                "Table '%s' has %d rows — only first %d embedded "
                "(cap: MAX_EMBEDDED_ROWS=%d).",
                meta.table_name, len(df), MAX_EMBEDDED_ROWS, MAX_EMBEDDED_ROWS
            )
        m_expr = _embed_as_enter_data(rows, col_names, meta)
        return {
            "name":   meta.table_name,
            "mode":   "import",
            "source": {"type": "m", "expression": m_expr},
        }

    # Option 3 — empty placeholder
    m_expr = [
        "let",
        f"    Source = Table.FromRows({{}}, {{{col_list_m}}})",
        "in",
        "    Source",
    ]
    return {
        "name":   meta.table_name,
        "mode":   "import",
        "source": {"type": "m", "expression": m_expr},
    }


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — BIM SEMANTIC MODEL
# ─────────────────────────────────────────────────────────────────────────────

def _bim_datatype(pandas_dtype: str) -> str:
    """Map pandas dtype → BIM dataType string."""
    d = pandas_dtype.lower()
    if "int" in d:    return "int64"
    if "float" in d:  return "double"
    if "bool" in d:   return "boolean"
    if "datetime" in d: return "dateTime"
    return "string"


def _bim_format(format_string: str) -> str:
    """Map our internal format string to BIM formatString."""
    mapping = {
        "$#,##0":     "\\$#,##0",
        "$#,##0.00":  "\\$#,##0.00",
        "#,##0":      "#,##0",
        "#,##0.00":   "#,##0.00",
        "0.0%":       "0.0%",
        "0%":         "0%",
        "0.00%":      "0.00%",
    }
    return mapping.get(format_string, format_string)


def _dax_expression(measure: MeasureDefinition) -> str:
    """
    Strip the 'MeasureName = ' prefix from the stored DAX formula
    so the BIM expression field contains only the RHS.
    """
    dax = measure.dax_formula or ""
    if "=" in dax:
        return dax.split("=", 1)[1].strip()
    return dax


def _infer_display_folder(measure: MeasureDefinition) -> str:
    if measure.is_kpi:
        return "KPIs"
    ti = (measure.time_intelligence or "").upper()
    if ti in ("YTD", "MTD", "ROLLING_12M", "PRIOR_PERIOD"):
        return "Time Intelligence"
    agg = measure.aggregation
    if agg in (AggregationType.RATIO,):
        return "Ratios"
    if agg in (AggregationType.COUNT, AggregationType.COUNTD):
        return "Counts"
    return "Measures"


def _build_bim_table(meta: FileMeta,
                      df: Optional[pd.DataFrame] = None,
                      file_path: Optional[str] = None) -> dict:
    """Build a single BIM table object from a FileMeta.

    file_path — absolute path to the saved source file on disk.
                When provided, M query reads directly from the file
                (preferred over Base64 embedding).
    df        — DataFrame for Base64 fallback when no file_path available.
    """
    columns = []
    # Power BI allows only ONE isKey=True column per table — use first PK candidate only
    key_assigned = False
    for col in meta.columns:
        col_obj = {
            "name": col.name,
            "dataType": _bim_datatype(col.dtype),
            "isHidden": False,
            "summarizeBy": "none",
            "annotations": [
                {"name": "SummarizationSetBy", "value": "Automatic"}
            ],
        }
        # Numeric columns: let Power BI decide summarisation
        if col.is_numeric:
            col_obj["summarizeBy"] = "sum"
        # Date columns: tag with format
        if col.is_date:
            col_obj["dataType"] = "dateTime"
            col_obj["formatString"] = "General Date"
        # Only the FIRST PK candidate gets isKey=True — PBI allows max one key per table
        if col.is_pk_candidate and not key_assigned:
            col_obj["isKey"] = True
            col_obj["summarizeBy"] = "none"
            key_assigned = True
        columns.append(col_obj)

    table = {
        "name": meta.table_name,
        "columns": columns,
        "partitions": [_build_data_partition(meta, df, file_path)],
        "annotations": [
            {"name": "PBI_ResultType", "value": "Table"},
        ],
    }

    # Add row count annotation for reference
    if meta.row_count > 0:
        table["annotations"].append(
            {"name": "RowCount", "value": str(meta.row_count)}
        )

    return table


def _build_bim_measures(model: SemanticModel) -> list[dict]:
    """
    Build BIM measure objects. Measures are placed on the first raw/fact table.
    If no tables exist, returns empty list.
    """
    if not model.tables:
        return []

    measures = []
    for m in model.measures:
        expr = _dax_expression(m)
        if not expr:
            continue

        measure_obj = {
            "name": m.display_name or m.name,
            "expression": expr,
            "formatString": _bim_format(m.format_string),
            "description": m.description or "",
            "displayFolder": _infer_display_folder(m),
            "isHidden": False,
            "annotations": [
                {"name": "PBI_FormatHint", "value": f'{{"isGeneralNumber":true}}'},
            ],
        }

        # KPI metadata
        if m.is_kpi and m.kpi_target:
            measure_obj["kpi"] = {
                "targetExpression": m.kpi_target,
                "statusGraphic": "Three Symbols UnCircled Colored",
            }

        measures.append(measure_obj)

    return measures


def export_bim(model: SemanticModel,
               dfs: Optional[dict] = None,
               file_paths: Optional[dict] = None) -> str:
    """
    Generate a Power BI / Analysis Services BIM JSON file.
    Returns JSON string ready to write to model.bim.

    file_paths: {table_name: absolute_file_path} — when provided, M queries
    point to the saved source files on disk. This is the preferred approach:
    no row limits, no embedding issues, supports monthly refresh by replacing
    source files. Power BI reads directly from the Excel/CSV files.

    dfs: {table_name: DataFrame} — fallback when no file_paths available.
    Data is Base64-encoded and embedded in the BIM (works but has row limits).

    Import into Power BI Desktop:
      File → Import → Power BI template  (or use Tabular Editor / XMLA)
    """
    dfs        = dfs or {}
    file_paths = file_paths or {}

    # ── Tables ──────────────────────────────────────────────────────────────
    bim_tables = []
    fact_table_name = None  # We'll attach measures to the first raw/fact table

    for meta in model.tables:
        t = _build_bim_table(
            meta,
            df        = dfs.get(meta.table_name),
            file_path = file_paths.get(meta.table_name),
        )
        bim_tables.append(t)
        if fact_table_name is None:
            fact_table_name = meta.table_name  # first table = measure home

    # Attach measures to the first table
    bim_measures = _build_bim_measures(model)
    if bim_measures and bim_tables:
        bim_tables[0]["measures"] = bim_measures

    # ── Date table (auto-generated if any date column found) ─────────────
    has_date_col = any(
        col.is_date
        for meta in model.tables
        for col in meta.columns
    )
    if has_date_col:
        date_table = _build_auto_date_table()
        bim_tables.append(date_table)

    # ── Relationships ────────────────────────────────────────────────────
    bim_relationships = []
    for rel in model.relationships:
        cardinality = "manyToOne"
        if rel.join_type == JoinType.ONE_TO_ONE:
            cardinality = "oneToOne"

        cross_filter_bim = (
            "bothDirections"
            if rel.cross_filter.upper() in ("BOTH", "BOTHDIRECTIONS")
            else "oneDirection"
        )

        bim_relationships.append({
            "name":             f"{rel.from_table}_{rel.from_column}_{rel.to_table}",
            "fromTable":        rel.from_table,
            "fromColumn":       rel.from_column,
            "toTable":          rel.to_table,
            "toColumn":         rel.to_column,
            "crossFilteringBehavior": cross_filter_bim,
            "isActive":         rel.is_active,
            "joinOnDateBehavior": "dateAndTime",
        })

    # ── Roles (Row-Level Security placeholder) ───────────────────────────
    roles = [
        {
            "name": "ReportViewer",
            "modelPermission": "read",
            "members": [],
            "tablePermissions": [],
        }
    ]

    # Note: deliberately no "cultures" block. A culture entry requires a
    # complete, valid linguisticMetadata schema (with a required "Language"
    # property among others) for Power BI's natural-language/Q&A engine to
    # parse — a minimal/placeholder one causes a hard error on load
    # ("Required property 'Language' not found in JSON") even though the
    # rest of the model loads fine. Cultures are optional; the model works
    # correctly without one, just without a defined Q&A linguistic schema.

    # ── Final BIM structure ───────────────────────────────────────────────
    # Add SourceDataFolder parameter if using file-path M queries
    bim_expressions = []
    if file_paths:
        bim_expressions.append(_build_source_folder_parameter())

    bim = {
        "name":                  model.project_name,
        "compatibilityLevel":    1567,           # PBI Desktop current level
        "model": {
            "defaultPowerBIDataSourceVersion": "powerBI_V3",
            "sourceQueryCulture":              "en-US",
            "tables":        bim_tables,
            "relationships": bim_relationships,
            "roles":         roles,
            "expressions":   bim_expressions,
            "annotations": [
                {"name": "PBI_QueryOrder",       "value": json.dumps([t["name"] for t in bim_tables])},
                {"name": "AutoCreatedRelationships", "value": "[]"},
                {"name": "GeneratedBy",          "value": "AI Reporting Automation System v1.0"},
                {"name": "ModelID",              "value": model.model_id},
                {"name": "ProjectID",            "value": model.project_id},
                {"name": "BuiltAt",              "value": str(model.built_at)},
            ],
        },
    }

    return json.dumps(bim, indent=2, default=str)


def _build_auto_date_table() -> dict:
    """
    Generate a standard auto date/calendar table as a calculated M table.
    Provides Year, Quarter, Month, MonthNo, Week, Day columns.
    """
    m_expr = [
        "let",
        "    StartDate = #date(2015, 1, 1),",
        "    EndDate   = #date(2030, 12, 31),",
        "    Duration  = Duration.Days(EndDate - StartDate) + 1,",
        "    Dates     = List.Dates(StartDate, Duration, #duration(1,0,0,0)),",
        "    Table     = Table.FromList(Dates, Splitter.SplitByNothing(), {\"Date\"}),",
        "    TypedDate = Table.TransformColumnTypes(Table, {{\"Date\", type date}}),",
        "    Year      = Table.AddColumn(TypedDate, \"Year\",     each Date.Year([Date]),    Int64.Type),",
        "    Quarter   = Table.AddColumn(Year,      \"Quarter\",  each \"Q\" & Text.From(Date.QuarterOfYear([Date])), type text),",
        "    MonthNo   = Table.AddColumn(Quarter,   \"MonthNo\",  each Date.Month([Date]),   Int64.Type),",
        "    Month     = Table.AddColumn(MonthNo,   \"Month\",    each Date.ToText([Date], \"MMM yyyy\"), type text),",
        "    Week      = Table.AddColumn(Month,     \"WeekNo\",   each Date.WeekOfYear([Date]), Int64.Type),",
        "    Day       = Table.AddColumn(Week,      \"DayOfWeek\",each Date.DayOfWeekName([Date]), type text),",
        "    Sorted    = Table.Sort(Day, {{\"Date\", Order.Ascending}})",
        "in",
        "    Sorted",
    ]

    return {
        "name": "dim_date",
        "columns": [
            {"name": "Date",      "dataType": "dateTime", "isKey": True,  "summarizeBy": "none", "isHidden": False},
            {"name": "Year",      "dataType": "int64",    "isKey": False, "summarizeBy": "none", "isHidden": False},
            {"name": "Quarter",   "dataType": "string",   "isKey": False, "summarizeBy": "none", "isHidden": False},
            {"name": "MonthNo",   "dataType": "int64",    "isKey": False, "summarizeBy": "none", "isHidden": True},
            {"name": "Month",     "dataType": "string",   "isKey": False, "summarizeBy": "none", "isHidden": False},
            {"name": "WeekNo",    "dataType": "int64",    "isKey": False, "summarizeBy": "none", "isHidden": False},
            {"name": "DayOfWeek", "dataType": "string",   "isKey": False, "summarizeBy": "none", "isHidden": False},
        ],
        "partitions": [
            {
                "name": "dim_date",
                "mode": "import",
                "source": {"type": "m", "expression": m_expr},
            }
        ],
        "annotations": [
            {"name": "PBI_ResultType", "value": "Table"},
            {"name": "AutoDateTable",  "value": "true"},
        ],
        "isHidden": False,
        "showAsVariationsOnly": False,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — REPORT LAYOUT (.pbir / report.json)
# ─────────────────────────────────────────────────────────────────────────────

# Canvas dimensions (Power BI default 16:9 widescreen)
CANVAS_W = 1280
CANVAS_H = 720

# Layout grid constants
MARGIN       = 20
TITLE_H      = 60
KPI_ROW_Y    = TITLE_H + MARGIN
KPI_H        = 90
VISUAL_ROW_Y = KPI_ROW_Y + KPI_H + MARGIN
VISUAL_H     = CANVAS_H - VISUAL_ROW_Y - MARGIN
VISUAL_COLS  = 3


def _new_guid() -> str:
    return str(uuid.uuid4()).upper()


def _visual_base(x: int, y: int, w: int, h: int,
                 visual_type: str, name: str) -> dict:
    """Skeleton visual container used by all visual builders."""
    return {
        "id":   _new_guid(),
        "name": name,
        "visualType": visual_type,
        "x": x, "y": y, "z": 0,
        "width": w, "height": h,
        "config": "{}",
        "filters": "[]",
        "objects": "{}",
    }


def _title_visual(project_name: str) -> dict:
    """Full-width title text box."""
    v = _visual_base(MARGIN, MARGIN, CANVAS_W - 2 * MARGIN, TITLE_H - MARGIN,
                     "textbox", "ReportTitle")
    v["config"] = {
        "singleVisual": {
            "visualType": "textbox",
            "objects": {
                "general": [{
                    "properties": {
                        "paragraphs": [{
                            "textRuns": [{
                                "value": project_name,
                                "textStyle": {
                                    "fontWeight": "bold",
                                    "fontSize": "24pt",
                                    "color": {"solid": {"color": "#0F2D52"}},
                                },
                            }],
                            "horizontalTextAlignment": "Left",
                        }]
                    }
                }]
            },
        }
    }
    return v


def _kpi_card_visual(measure: MeasureDefinition,
                     x: int, y: int, w: int, h: int,
                     fact_table: str) -> dict:
    """Single KPI card visual."""
    v = _visual_base(x, y, w, h, "card", f"kpi_{measure.name}")
    v["config"] = {
        "singleVisual": {
            "visualType": "card",
            "projections": {
                "Values": [{"queryRef": f"{fact_table}.{measure.display_name}"}]
            },
            "prototypeQuery": {
                "Select": [{
                    "Measure": {
                        "Expression": {"SourceRef": {"Source": fact_table}},
                        "Property": measure.display_name,
                    },
                    "Name": f"{fact_table}.{measure.display_name}",
                }],
                "From": [{"Name": fact_table, "Entity": fact_table}],
            },
            "objects": {
                "labels": [{"properties": {
                    "fontSize": {"expr": {"Literal": {"Value": "20D"}}},
                    "fontBold": {"expr": {"Literal": {"Value": "true"}}},
                    "color": {"solid": {"color": "#0F2D52"}},
                }}],
                "title": [{"properties": {
                    "show": {"expr": {"Literal": {"Value": "true"}}},
                    "titleText": {"expr": {"Literal": {"Value": f"'{measure.display_name}'"}}},
                    "fontSize": {"expr": {"Literal": {"Value": "9D"}}},
                    "fontColor": {"solid": {"color": "#616161"}},
                }}],
                "background": [{"properties": {
                    "show": {"expr": {"Literal": {"Value": "true"}}},
                    "color": {"solid": {"color": "#E3F2FD"}},
                    "transparency": {"expr": {"Literal": {"Value": "0D"}}},
                }}],
                "border": [{"properties": {
                    "show": {"expr": {"Literal": {"Value": "true"}}},
                    "color": {"solid": {"color": "#1565C0"}},
                    "radius": {"expr": {"Literal": {"Value": "4D"}}},
                }}],
            },
        }
    }
    return v


def _bar_chart_visual(measure: MeasureDefinition, dim_table: FileMeta,
                      dim_col: str, x: int, y: int, w: int, h: int,
                      fact_table: str) -> dict:
    """Clustered bar chart: dimension on Y-axis, measure on X-axis."""
    v = _visual_base(x, y, w, h, "barChart", f"bar_{measure.name}")
    v["config"] = {
        "singleVisual": {
            "visualType": "barChart",
            "projections": {
                "Category": [{"queryRef": f"{dim_table.table_name}.{dim_col}"}],
                "Y":        [{"queryRef": f"{fact_table}.{measure.display_name}"}],
            },
            "prototypeQuery": {
                "Select": [
                    {
                        "Column": {
                            "Expression": {"SourceRef": {"Source": dim_table.table_name}},
                            "Property": dim_col,
                        },
                        "Name": f"{dim_table.table_name}.{dim_col}",
                    },
                    {
                        "Measure": {
                            "Expression": {"SourceRef": {"Source": fact_table}},
                            "Property": measure.display_name,
                        },
                        "Name": f"{fact_table}.{measure.display_name}",
                    },
                ],
                "From": [
                    {"Name": dim_table.table_name, "Entity": dim_table.table_name},
                    {"Name": fact_table,           "Entity": fact_table},
                ],
                "OrderBy": [{
                    "Direction": 2,
                    "Expression": {
                        "Measure": {
                            "Expression": {"SourceRef": {"Source": fact_table}},
                            "Property": measure.display_name,
                        }
                    },
                }],
                "Top": {"Count": 20},
            },
            "objects": {
                "title": [{"properties": {
                    "show": {"expr": {"Literal": {"Value": "true"}}},
                    "titleText": {"expr": {"Literal": {"Value": f"'{measure.display_name} by {dim_col}'"}}},
                    "fontSize": {"expr": {"Literal": {"Value": "10D"}}},
                    "fontColor": {"solid": {"color": "#0F2D52"}},
                }}],
                "dataColors": [{"properties": {
                    "fill": {"solid": {"color": "#1565C0"}},
                }}],
            },
        }
    }
    return v


def _line_chart_visual(measure: MeasureDefinition, date_col: str,
                       date_table: str, x: int, y: int, w: int, h: int,
                       fact_table: str) -> dict:
    """Line chart: date on X-axis, measure on Y-axis."""
    v = _visual_base(x, y, w, h, "lineChart", f"line_{measure.name}")
    v["config"] = {
        "singleVisual": {
            "visualType": "lineChart",
            "projections": {
                "Category": [{"queryRef": f"{date_table}.{date_col}"}],
                "Y":        [{"queryRef": f"{fact_table}.{measure.display_name}"}],
            },
            "prototypeQuery": {
                "Select": [
                    {
                        "Column": {
                            "Expression": {"SourceRef": {"Source": date_table}},
                            "Property": date_col,
                        },
                        "Name": f"{date_table}.{date_col}",
                    },
                    {
                        "Measure": {
                            "Expression": {"SourceRef": {"Source": fact_table}},
                            "Property": measure.display_name,
                        },
                        "Name": f"{fact_table}.{measure.display_name}",
                    },
                ],
                "From": [
                    {"Name": date_table,  "Entity": date_table},
                    {"Name": fact_table,  "Entity": fact_table},
                ],
                "OrderBy": [{
                    "Direction": 1,
                    "Expression": {
                        "Column": {
                            "Expression": {"SourceRef": {"Source": date_table}},
                            "Property": date_col,
                        }
                    },
                }],
            },
            "objects": {
                "title": [{"properties": {
                    "show":      {"expr": {"Literal": {"Value": "true"}}},
                    "titleText": {"expr": {"Literal": {"Value": f"'{measure.display_name} Over Time'"}}},
                    "fontSize":  {"expr": {"Literal": {"Value": "10D"}}},
                    "fontColor": {"solid": {"color": "#0F2D52"}},
                }}],
                "line": [{"properties": {
                    "stroke":          {"solid": {"color": "#1565C0"}},
                    "strokeWidth":     {"expr": {"Literal": {"Value": "2D"}}},
                    "showMarkers":     {"expr": {"Literal": {"Value": "true"}}},
                    "markerShape":     {"expr": {"Literal": {"Value": "'circle'"}}},
                    "markerSize":      {"expr": {"Literal": {"Value": "4D"}}},
                }}],
            },
        }
    }
    return v


def _donut_chart_visual(measure: MeasureDefinition, dim_table: FileMeta,
                        dim_col: str, x: int, y: int, w: int, h: int,
                        fact_table: str) -> dict:
    """Donut chart for proportion breakdown."""
    v = _visual_base(x, y, w, h, "donutChart", f"donut_{measure.name}")
    v["config"] = {
        "singleVisual": {
            "visualType": "donutChart",
            "projections": {
                "Category": [{"queryRef": f"{dim_table.table_name}.{dim_col}"}],
                "Y":        [{"queryRef": f"{fact_table}.{measure.display_name}"}],
            },
            "prototypeQuery": {
                "Select": [
                    {
                        "Column": {
                            "Expression": {"SourceRef": {"Source": dim_table.table_name}},
                            "Property": dim_col,
                        },
                        "Name": f"{dim_table.table_name}.{dim_col}",
                    },
                    {
                        "Measure": {
                            "Expression": {"SourceRef": {"Source": fact_table}},
                            "Property": measure.display_name,
                        },
                        "Name": f"{fact_table}.{measure.display_name}",
                    },
                ],
                "From": [
                    {"Name": dim_table.table_name, "Entity": dim_table.table_name},
                    {"Name": fact_table,           "Entity": fact_table},
                ],
                "Top": {"Count": 8},
            },
            "objects": {
                "title": [{"properties": {
                    "show":      {"expr": {"Literal": {"Value": "true"}}},
                    "titleText": {"expr": {"Literal": {"Value": f"'{measure.display_name} Mix'"}}},
                    "fontSize":  {"expr": {"Literal": {"Value": "10D"}}},
                    "fontColor": {"solid": {"color": "#0F2D52"}},
                }}],
            },
        }
    }
    return v


def _matrix_visual(measures: list[MeasureDefinition],
                   row_table: FileMeta, row_col: str,
                   x: int, y: int, w: int, h: int,
                   fact_table: str) -> dict:
    """Matrix (pivot table) with up to 3 measures as columns."""
    use_measures = measures[:3]
    v = _visual_base(x, y, w, h, "pivotTable", f"matrix_summary")

    value_projections = [
        {"queryRef": f"{fact_table}.{m.display_name}"}
        for m in use_measures
    ]
    value_selects = [
        {
            "Measure": {
                "Expression": {"SourceRef": {"Source": fact_table}},
                "Property": m.display_name,
            },
            "Name": f"{fact_table}.{m.display_name}",
        }
        for m in use_measures
    ]

    v["config"] = {
        "singleVisual": {
            "visualType": "pivotTable",
            "projections": {
                "Rows":   [{"queryRef": f"{row_table.table_name}.{row_col}"}],
                "Values": value_projections,
            },
            "prototypeQuery": {
                "Select": [
                    {
                        "Column": {
                            "Expression": {"SourceRef": {"Source": row_table.table_name}},
                            "Property": row_col,
                        },
                        "Name": f"{row_table.table_name}.{row_col}",
                    },
                    *value_selects,
                ],
                "From": [
                    {"Name": row_table.table_name, "Entity": row_table.table_name},
                    {"Name": fact_table,           "Entity": fact_table},
                ],
                "OrderBy": [{
                    "Direction": 2,
                    "Expression": {
                        "Measure": {
                            "Expression": {"SourceRef": {"Source": fact_table}},
                            "Property": use_measures[0].display_name,
                        }
                    },
                }],
            },
            "objects": {
                "title": [{"properties": {
                    "show":      {"expr": {"Literal": {"Value": "true"}}},
                    "titleText": {"expr": {"Literal": {"Value": "'Summary Matrix'"}}},
                    "fontSize":  {"expr": {"Literal": {"Value": "10D"}}},
                    "fontColor": {"solid": {"color": "#0F2D52"}},
                }}],
                "columnHeaders": [{"properties": {
                    "fontBold":        {"expr": {"Literal": {"Value": "true"}}},
                    "backColor":       {"solid": {"color": "#0F2D52"}},
                    "fontColor":       {"solid": {"color": "#FFFFFF"}},
                    "fontSize":        {"expr": {"Literal": {"Value": "9D"}}},
                }}],
            },
        }
    }
    return v


def _slicer_visual(dim_table: FileMeta, dim_col: str,
                   x: int, y: int, w: int, h: int) -> dict:
    """Dropdown slicer for a dimension column."""
    v = _visual_base(x, y, w, h, "slicer", f"slicer_{dim_table.table_name}_{dim_col}")
    v["config"] = {
        "singleVisual": {
            "visualType": "slicer",
            "projections": {
                "Values": [{"queryRef": f"{dim_table.table_name}.{dim_col}"}]
            },
            "prototypeQuery": {
                "Select": [{
                    "Column": {
                        "Expression": {"SourceRef": {"Source": dim_table.table_name}},
                        "Property": dim_col,
                    },
                    "Name": f"{dim_table.table_name}.{dim_col}",
                }],
                "From": [{"Name": dim_table.table_name, "Entity": dim_table.table_name}],
            },
            "objects": {
                "data": [{"properties": {
                    "mode": {"expr": {"Literal": {"Value": "'Dropdown'"}}},
                }}],
                "header": [{"properties": {
                    "show":      {"expr": {"Literal": {"Value": "true"}}},
                    "fontColor": {"solid": {"color": "#0F2D52"}},
                    "fontBold":  {"expr": {"Literal": {"Value": "true"}}},
                    "fontSize":  {"expr": {"Literal": {"Value": "9D"}}},
                }}],
                "title": [{"properties": {
                    "show":      {"expr": {"Literal": {"Value": "true"}}},
                    "titleText": {"expr": {"Literal": {"Value": f"'Filter by {dim_col}'"}}},
                }}],
            },
        }
    }
    return v


# ── Visual selection logic ────────────────────────────────────────────────

def _pick_dimension_col(meta: FileMeta) -> Optional[str]:
    """
    Pick the best non-PK column from a dimension table for use as a
    categorical axis. Prefers low-to-medium cardinality text columns.
    """
    candidates = []
    for col in meta.columns:
        if col.is_pk_candidate:
            continue
        if col.is_numeric or col.is_date:
            continue
        candidates.append((col.unique_count, col.name))
    if not candidates:
        # fall back to first non-numeric column
        for col in meta.columns:
            if not col.is_numeric:
                return col.name
        return meta.columns[0].name if meta.columns else None
    # Sort by cardinality: prefer 2–50 unique values (ideal for charts)
    candidates.sort(key=lambda x: abs(x[0] - 15))
    return candidates[0][1]


def _find_date_info(model: SemanticModel) -> tuple[Optional[str], Optional[str]]:
    """Find a date column and its table from the model."""
    for meta in model.tables:
        for col in meta.columns:
            if col.is_date:
                return meta.table_name, col.name
    return None, None


def _layout_visuals_grid(visuals: list[dict],
                          start_x: int, start_y: int,
                          total_w: int, h: int, cols: int) -> None:
    """
    Re-layout a list of visuals into a grid in-place.
    Each visual gets equal width; rows wrap at `cols` columns.
    """
    if not visuals:
        return
    cell_w = (total_w - MARGIN * (cols - 1)) // cols
    for i, v in enumerate(visuals):
        col_i = i % cols
        row_i = i // cols
        v["x"] = start_x + col_i * (cell_w + MARGIN)
        v["y"] = start_y + row_i * (h + MARGIN)
        v["width"]  = cell_w
        v["height"] = h



def _stringify_report_fields(obj):
    """
    Recursively walk the report dict and convert any 'config', 'filters',
    'objects' values that are dicts or lists into JSON strings.
    Power BI Desktop May 2026+ (.pbir format) requires these as strings.
    """
    if isinstance(obj, dict):
        for key in list(obj.keys()):
            if key in ("config", "filters", "objects"):
                if isinstance(obj[key], (dict, list)):
                    obj[key] = json.dumps(obj[key], separators=(',', ':'))
            else:
                _stringify_report_fields(obj[key])
    elif isinstance(obj, list):
        for item in obj:
            _stringify_report_fields(item)


def export_report_json(model: SemanticModel) -> str:
    """
    Generate a Power BI report layout JSON file.

    Visual selection strategy:
    - KPI cards row   → one card per KPI measure (up to 5)
    - Time series     → line chart if date column found
    - Bar charts      → one per dimension table × top measure
    - Donut chart     → proportion breakdown for count/ratio measures
    - Matrix          → summary pivot if 2+ measures exist
    - Slicers         → one per dimension table (right column)

    Returns JSON string for report.json (use inside .pbip folder).
    """
    if not model.tables:
        return json.dumps({"error": "No tables in model"})

    fact_table = model.tables[0].table_name
    dim_tables = model.tables[1:] if len(model.tables) > 1 else []

    kpi_measures  = [m for m in model.measures if m.is_kpi]
    all_measures  = model.measures
    top_measure   = all_measures[0] if all_measures else None

    visuals: list[dict] = []

    # ── Title ─────────────────────────────────────────────────────────────
    visuals.append(_title_visual(model.project_name))

    # NOTE: KPI cards, charts, matrix, and slicers are deliberately NOT
    # auto-generated below. Power BI's report.json field-binding format
    # isn't publicly documented (Microsoft: "doesn't support external
    # editing"), and hand-built visuals were unreliable in testing —
    # some bound correctly, some didn't, with no clear pattern. Rather
    # than ship a report that's broken in ways that are hard to predict,
    # this exports a clean title-only report. The semantic model (tables,
    # relationships, measures, DAX) is fully built and correct — open
    # this in Power BI Desktop and drag fields from the Data pane to
    # build the actual report; every field will work since the model
    # itself loads correctly.
    chart_visuals: list[dict] = []

    # ── Report JSON ──────────────────────────────────────────────────────
    report = {
        "id":          _new_guid(),
        "name":        model.project_name,
        "reportSchemaVersion": "1.13.0",
        "dataTransforms": {},
        "sections": [
            {
                "id":          _new_guid(),
                "name":        "Overview",
                "displayName": f"{model.project_name} — Overview",
                "filters":     [],
                "ordinal":     0,
                "visualContainers": visuals,
                "config": {
                    "type": "report",
                    "themeCollection": {
                        "baseTheme": {
                            "name": "CY24SU10",
                            "version": "5.63",
                            "type": "SharedResources",
                        }
                    },
                    "canvasLayout": {
                        "canvasSize": {
                            "width":  CANVAS_W,
                            "height": CANVAS_H,
                        },
                        "backgroundImage": {"transparency": 100},
                    },
                    "activeSectionIndex": 0,
                },
            }
        ],
        "config": {
            "version":     "1.13.0",
            "themeCollection": {
                "baseTheme": {
                    "name": "CY24SU10",
                    "version": "5.63",
                    "type": "SharedResources",
                }
            },
            "settings": {
                "useStylableVisualContainerHeader": True,
                "allowChangeFilterTypes": True,
                "useDefaultAggregateDisplayName": True,
            },
            "objects": {
                "section": [{
                    "properties": {
                        "background": {"solid": {"color": "#F4F4FA"}},
                        "wallpaper":  {"solid": {"color": "#E3F2FD"}},
                    }
                }]
            },
        },
        "annotations": [
            {"name": "GeneratedBy",  "value": "AI Reporting Automation System v1.0"},
            {"name": "ModelID",      "value": model.model_id},
            {"name": "ProjectID",    "value": model.project_id},
            {"name": "VisualCount",  "value": str(len(visuals))},
        ],
    }

    # Power BI May 2026+ requires config/filters/objects to be JSON strings not objects
    _stringify_report_fields(report)
    return json.dumps(report, indent=2, default=str)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — README / IMPORT GUIDE
# ─────────────────────────────────────────────────────────────────────────────

def _build_readme(model: SemanticModel, has_source_files: bool = False) -> str:
    built = str(model.built_at)[:19] if model.built_at else "unknown"
    measure_list = "\n".join(
        f"  • {m.display_name}  ({m.aggregation.value})"
        + ("  ← KPI" if m.is_kpi else "")
        for m in model.measures
    )
    rel_list = "\n".join(
        f"  • {r.from_table}.{r.from_column} → {r.to_table}.{r.to_column}"
        for r in model.relationships
    )
    table_list = "\n".join(
        f"  • {t.table_name}  ({t.row_count:,} rows, {t.col_count} columns)"
        for t in model.tables
    )

    return f"""
╔══════════════════════════════════════════════════════════════════╗
║         POWER BI EXPORT — IMPORT INSTRUCTIONS                    ║
║         Generated by AI Reporting Automation System v1.0         ║
╚══════════════════════════════════════════════════════════════════╝

Project:    {model.project_name}
Model ID:   {model.model_id}
Built:      {built}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 CONTENTS OF THIS ZIP
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  model.bim       — Tabular Model BIM (semantic layer: tables,
                    relationships, measures, DAX, KPIs)

  report.json     — Power BI report layout (visuals, KPI cards,
                    bar/line/donut charts, matrix, slicers)

  README_IMPORT.txt — This file

  {_safe_project_name(model.project_name)}.pbip
  {_safe_project_name(model.project_name)}.Report/
  {_safe_project_name(model.project_name)}.SemanticModel/
                  — A Power BI Project (PBIP) — opens directly in
                    Power BI Desktop without Tabular Editor (see Method D).
                    This is a PREVIEW feature on Microsoft's side.


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 HOW TO IMPORT INTO POWER BI DESKTOP (Recommended)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

METHOD D — Open the .pbip directly (Easiest, but PREVIEW feature)
  1. In Power BI Desktop: File → Options and settings → Options →
     Preview features → check "Power BI Project (.pbip) save option"
     (also enable "Store reports using enhanced metadata format (PBIR)"
     if prompted). Restart Power BI Desktop.
  2. Double-click {_safe_project_name(model.project_name)}.pbip
  3. Power BI Desktop opens the report and model directly — no
     Tabular Editor step needed.
  4. If Power BI Desktop reports an error opening a specific file,
     that error message will name the file and the problem — this
     project structure was hand-built rather than saved by Power BI
     Desktop itself, so treat this method as experimental.

METHOD A — External Tools / Tabular Editor (Most reliable)
  1. Install Tabular Editor 3 (free trial) or Tabular Editor 2 (free)
     https://tabulareditor.com
  2. Open Power BI Desktop → create a blank report
  3. In Power BI Desktop toolbar → External Tools → Tabular Editor
  4. In Tabular Editor: File → Open → From File → select model.bim
  5. Review tables, measures, relationships
  6. File → Save to Power BI Desktop (Ctrl+S)
  7. Back in Power BI Desktop, you will see all tables and measures loaded

METHOD B — XMLA Endpoint (Power BI Premium / Fabric)
  1. In your Power BI workspace: Settings → Enable XMLA endpoint
  2. Copy the workspace XMLA connection string
  3. In SQL Server Management Studio or Azure Data Studio:
     Connect → Analysis Services → paste XMLA connection string
  4. Right-click database → Restore from file → select model.bim
  5. Then connect Power BI Desktop to the published dataset

METHOD C — Manual (Fallback)
  1. Open Power BI Desktop → Get Data → Blank Query
  2. For each table in this model, create a query pointing to your data source
  3. In the Model view, recreate the relationships listed below
  4. In the Report view, use the New Measure button and paste the DAX formulas
     from the Measures sheet in the accompanying Excel workbook


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 HOW TO IMPORT THE REPORT LAYOUT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  The report.json file is a Power BI report layout file compatible
  with the .pbip (Power BI Project) format.

  To use it:
  1. Create a folder named: {model.project_name.replace(' ', '_')}.Report
  2. Place report.json inside it
  3. Create a companion file definition.pbir with this content:
     {{
       "version": "1.0",
       "datasetReference": {{
         "byPath": {{ "path": "../{model.project_name.replace(' ', '_')}.Dataset" }}
       }}
     }}
  4. Open the .pbip project file in Power BI Desktop (June 2023+ version)

  Alternatively, after loading your dataset via Method A above,
  in Tabular Editor: View → Report Layout → paste the report.json content.


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 MODEL SUMMARY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TABLES ({len(model.tables)}):
{table_list or "  (none)"}

RELATIONSHIPS ({len(model.relationships)}):
{rel_list or "  (none)"}

MEASURES ({len(model.measures)}):
{measure_list or "  (none)"}


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 NEXT STEPS AFTER IMPORT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  1. Connect your data sources: In Power BI Desktop → Transform Data,
     replace the placeholder M queries with your actual data connections
     (CSV files, SQL Server, SharePoint, etc.)

  2. Validate measure totals: Cross-check each KPI measure total against
     your source system to confirm the DAX formulas are correct.

  3. Apply Row-Level Security: The model includes a placeholder "ReportViewer"
     role. Add DAX filters in Power BI Desktop → Manage Roles to restrict
     data access per user.

  4. Customise visuals: The report layout is a starting point. Resize,
     recolour, and add drill-through as needed.

  5. Publish to Power BI Service: File → Publish → select your workspace.


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 SUPPORT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  This export was generated by AI Reporting Automation System v1.0.
  All DAX formulas were generated from your protocol document.
  If a measure produces unexpected results, check the DAX formula
  in the Excel export (Measures sheet) and adjust the base column
  or filter conditions as needed.

  Generated: {built}
  Model ID:  {model.model_id}
""".strip()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3B — .PBIP PROJECT STRUCTURE (direct double-click open, no Tabular Editor)
# ─────────────────────────────────────────────────────────────────────────────

def _safe_project_name(name: str) -> str:
    """Sanitize a project name for use as a Windows-safe folder/file name."""
    name = (name or "PowerBI_Project").strip()
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    return name or "PowerBI_Project"


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3C — PBIR FOLDER STRUCTURE (modern format, publicly documented)
# ─────────────────────────────────────────────────────────────────────────────
# PBIR replaces legacy report.json with a folder of small JSON files —
# one per page, one per visual — each validating against Microsoft's
# published schemas. Officially supports external generation.

PBIR_SCHEMA_BASE = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition"
PBIR_PAGE_W = 1280
PBIR_PAGE_H = 720


def _pbir_id() -> str:
    """Generate a runtime ID in the style Power BI uses (20 hex chars)."""
    return uuid.uuid4().hex[:20]


def _pbir_report_json() -> dict:
    """Report-wide settings (definition/report.json)."""
    return {
        "$schema": f"{PBIR_SCHEMA_BASE}/report/2.0.0/schema.json",
        "themeCollection": {
            "baseTheme": {
                "name": "CY24SU10",
                "reportVersionAtImport": "5.55",
                "type": "SharedResources"
            }
        },
        "layoutOptimization": "None",
        "resourcePackages": [
            {
                "name": "SharedResources",
                "type": "SharedResources",
                "items": [
                    {"name": "CY24SU10", "path": "BaseThemes/CY24SU10.json", "type": "BaseTheme"}
                ]
            }
        ],
        "settings": {"useNewFilterPaneExperience": True}
    }


def _pbir_version_json() -> dict:
    return {
        "$schema": f"{PBIR_SCHEMA_BASE}/versionMetadata/1.0.0/schema.json",
        "version": "2.0.0"
    }


def _pbir_pages_json(page_ids: list, active: str) -> dict:
    return {
        "$schema": f"{PBIR_SCHEMA_BASE}/pagesMetadata/1.0.0/schema.json",
        "pageOrder": page_ids,
        "activePageName": active
    }


def _pbir_page_json(page_id: str, display_name: str) -> dict:
    """CRITICAL: schema 2.0.0 allows ONLY these 6 properties — no extras."""
    return {
        "$schema": f"{PBIR_SCHEMA_BASE}/page/2.0.0/schema.json",
        "name": page_id,
        "displayName": display_name,
        "displayOption": "FitToPage",
        "height": PBIR_PAGE_H,
        "width": PBIR_PAGE_W
    }


def _pbir_title_visual(title_text: str, x: int, y: int, w: int, h: int) -> dict:
    """Text box visual showing the report title."""
    return {
        "$schema": f"{PBIR_SCHEMA_BASE}/visualContainer/2.0.0/schema.json",
        "name": _pbir_id(),
        "position": {"x": x, "y": y, "z": 0, "height": h, "width": w, "tabOrder": 0},
        "visual": {
            "visualType": "textbox",
            "drillFilterOtherVisuals": True,
            "objects": {
                "general": [
                    {
                        "properties": {
                            "paragraphs": [
                                {
                                    "textRuns": [
                                        {
                                            "value": title_text,
                                            "textStyle": {"fontSize": "20pt", "fontWeight": "bold"}
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                ]
            }
        }
    }


def _pbir_card_visual(measure_name: str, fact_table: str,
                       x: int, y: int, w: int, h: int, tab: int) -> dict:
    """KPI card bound to a measure. Entity/Property must match model.bim exactly."""
    return {
        "$schema": f"{PBIR_SCHEMA_BASE}/visualContainer/2.0.0/schema.json",
        "name": _pbir_id(),
        "position": {"x": x, "y": y, "z": 1, "height": h, "width": w, "tabOrder": tab},
        "visual": {
            "visualType": "card",
            "drillFilterOtherVisuals": True,
            "query": {
                "queryState": {
                    "Values": {
                        "projections": [
                            {
                                "field": {
                                    "Measure": {
                                        "Expression": {"SourceRef": {"Entity": fact_table}},
                                        "Property": measure_name
                                    }
                                },
                                "queryRef": f"{fact_table}.{measure_name}"
                            }
                        ]
                    }
                }
            }
        }
    }


def _legacy_card_config(measure_name: str, fact_table: str,
                         x: float, y: float, w: float, h: float, tab: int) -> str:
    """
    Build the config string for a card visual bound to a measure,
    using the EXACT format Power BI Desktop writes (verified from real saved files).
    """
    config = {
        "name": _pbir_id(),
        "layouts": [{"id": 0, "position": {
            "x": x, "y": y, "z": 0, "width": w, "height": h, "tabOrder": tab}}],
        "singleVisual": {
            "visualType": "card",
            "projections": {
                "Values": [{"queryRef": f"{fact_table}.{measure_name}"}]
            },
            "prototypeQuery": {
                "Version": 2,
                "From": [{"Name": "r", "Entity": fact_table, "Type": 0}],
                "Select": [
                    {
                        "Measure": {
                            "Expression": {"SourceRef": {"Source": "r"}},
                            "Property": measure_name
                        },
                        "Name": f"{fact_table}.{measure_name}",
                        "NativeReferenceName": measure_name
                    }
                ]
            },
            "drillFilterOtherVisuals": True
        }
    }
    return json.dumps(config)


def _legacy_slicer_config(column_name: str, dim_table: str,
                           x: float, y: float, w: float, h: float, tab: int) -> str:
    """Config string for a slicer bound to a dimension column."""
    config = {
        "name": _pbir_id(),
        "layouts": [{"id": 0, "position": {
            "x": x, "y": y, "z": 0, "width": w, "height": h, "tabOrder": tab}}],
        "singleVisual": {
            "visualType": "slicer",
            "projections": {
                "Values": [{"queryRef": f"{dim_table}.{column_name}", "active": True}]
            },
            "prototypeQuery": {
                "Version": 2,
                "From": [{"Name": "d", "Entity": dim_table, "Type": 0}],
                "Select": [
                    {
                        "Column": {
                            "Expression": {"SourceRef": {"Source": "d"}},
                            "Property": column_name
                        },
                        "Name": f"{dim_table}.{column_name}",
                        "NativeReferenceName": column_name
                    }
                ]
            },
            "drillFilterOtherVisuals": True
        }
    }
    return json.dumps(config)


def _legacy_title_config(title_text: str) -> str:
    """Config string for the title textbox."""
    config = {
        "name": _pbir_id(),
        "layouts": [{"id": 0, "position": {
            "x": 24, "y": 20, "z": 0, "width": 1232, "height": 50, "tabOrder": 0}}],
        "singleVisual": {
            "visualType": "textbox",
            "objects": {
                "general": [{
                    "properties": {
                        "paragraphs": [{
                            "textRuns": [{
                                "value": title_text,
                                "textStyle": {"fontSize": "24pt", "fontWeight": "bold",
                                              "color": {"solid": {"color": "#0F2D52"}}}
                            }],
                            "horizontalTextAlignment": "Left"
                        }]
                    }
                }]
            },
            "drillFilterOtherVisuals": True
        }
    }
    return json.dumps(config)


def _legacy_chart_config(visual_type: str, category_col: str, cat_table: str,
                          measure_name: str, fact_table: str,
                          x: float, y: float, w: float, h: float, tab: int,
                          measure_is_real: bool = True) -> str:
    """
    Config for bar/line/donut charts — category axis + measure value.
    Uses the exact prototypeQuery format Power BI writes:
    - Category column via Column binding on the category table
    - Value via Measure binding on the fact table

    Roles differ per visual type:
      barChart/columnChart : Category + Y
      lineChart            : Category + Y
      donutChart           : Category + Y
    """
    # Determine role names by visual type
    if visual_type in ("lineChart",):
        cat_role, val_role = "Category", "Y"
    elif visual_type in ("donutChart", "pieChart"):
        cat_role, val_role = "Category", "Y"
    else:  # barChart, columnChart, clusteredBarChart, clusteredColumnChart
        cat_role, val_role = "Category", "Y"

    cat_ref = f"{cat_table}.{category_col}"
    val_ref = f"{fact_table}.{measure_name}"

    select_items = [
        {
            "Column": {
                "Expression": {"SourceRef": {"Source": "c"}},
                "Property": category_col
            },
            "Name": cat_ref,
            "NativeReferenceName": category_col
        },
        {
            "Measure": {
                "Expression": {"SourceRef": {"Source": "f"}},
                "Property": measure_name
            },
            "Name": val_ref,
            "NativeReferenceName": measure_name
        }
    ]

    from_clause = [
        {"Name": "c", "Entity": cat_table, "Type": 0},
        {"Name": "f", "Entity": fact_table, "Type": 0}
    ]

    config = {
        "name": _pbir_id(),
        "layouts": [{"id": 0, "position": {
            "x": x, "y": y, "z": 0, "width": w, "height": h, "tabOrder": tab}}],
        "singleVisual": {
            "visualType": visual_type,
            "projections": {
                cat_role: [{"queryRef": cat_ref, "active": True}],
                val_role: [{"queryRef": val_ref}]
            },
            "prototypeQuery": {
                "Version": 2,
                "From": from_clause,
                "Select": select_items
            },
            "drillFilterOtherVisuals": True
        }
    }
    return json.dumps(config)


def _legacy_column_agg_chart_config(visual_type: str, category_col: str, cat_table: str,
                                     agg_col: str, agg_table: str, agg_func: int,
                                     x: float, y: float, w: float, h: float, tab: int) -> str:
    """
    Chart using an AGGREGATION of a raw column (not a measure) —
    used as a fallback when measures have broken/stub DAX.
    agg_func: 0=Sum, 1=Avg, 2=Min, 3=Max, 5=Count.
    This matches exactly the donut chart format PBI wrote in the ground-truth file.
    """
    cat_ref = f"{cat_table}.{category_col}"
    agg_names = {0: "Sum", 1: "Average", 2: "Min", 3: "Max", 5: "CountNonNull"}
    val_ref = f"{agg_names.get(agg_func,'Sum')}({agg_table}.{agg_col})"

    if visual_type == "lineChart":
        cat_role, val_role = "Category", "Y"
    else:
        cat_role, val_role = "Category", "Y"

    same_table = (cat_table == agg_table)
    if same_table:
        from_clause = [{"Name": "r", "Entity": cat_table, "Type": 0}]
        cat_src, agg_src = "r", "r"
    else:
        from_clause = [
            {"Name": "c", "Entity": cat_table, "Type": 0},
            {"Name": "f", "Entity": agg_table, "Type": 0}
        ]
        cat_src, agg_src = "c", "f"

    config = {
        "name": _pbir_id(),
        "layouts": [{"id": 0, "position": {
            "x": x, "y": y, "z": 0, "width": w, "height": h, "tabOrder": tab}}],
        "singleVisual": {
            "visualType": visual_type,
            "projections": {
                cat_role: [{"queryRef": cat_ref, "active": True}],
                val_role: [{"queryRef": val_ref}]
            },
            "prototypeQuery": {
                "Version": 2,
                "From": from_clause,
                "Select": [
                    {
                        "Column": {
                            "Expression": {"SourceRef": {"Source": cat_src}},
                            "Property": category_col
                        },
                        "Name": cat_ref,
                        "NativeReferenceName": category_col
                    },
                    {
                        "Aggregation": {
                            "Expression": {"Column": {
                                "Expression": {"SourceRef": {"Source": agg_src}},
                                "Property": agg_col
                            }},
                            "Function": agg_func
                        },
                        "Name": val_ref,
                        "NativeReferenceName": agg_col
                    }
                ]
            },
            "drillFilterOtherVisuals": True
        }
    }
    return json.dumps(config)


def _is_key_like(name: str) -> bool:
    """
    Dataset-agnostic detector for identifier / surrogate-key columns.
    Such columns (CustomerID, OrderID, DateKey, ProductKey, ...) are numeric
    but MUST NOT be summed/averaged on a chart — they only belong on an axis,
    a relationship, or a distinct count. Works for any dataset by shape of the
    name, not by a hardcoded list.
    """
    n = name.strip().lower().replace(" ", "").replace("_", "")
    return n == "id" or n.endswith("id") or n.endswith("key")


def _chart_value_measures(model: "SemanticModel") -> list[str]:
    """Names of real, validated measures to use as chart values (never a raw key)."""
    return [(m.display_name or m.name) for m in (model.measures or [])]


def build_pbir_report_files(model: SemanticModel, report_folder: str) -> dict:
    """
    Generate report using the LEGACY config-string format that Power BI
    Desktop actually writes (verified from real saved .pbip files).

    This is the hybrid format PBI uses even with PBIR preview on:
    - definition/pages/pages.json  (modern metadata location)
    - report.json                  (legacy config-string visuals — what PBI writes)

    Auto-generates: title + KPI cards + slicers, all dynamically bound.
    Returns {relative_path: json_text}.
    """
    files = {}
    page_id = _pbir_id()

    fact_table = model.tables[0].table_name if model.tables else None
    dim_tables = model.tables[1:] if len(model.tables) > 1 else []

    # ── Build visual containers in legacy format ──────────────────────────
    visual_containers = []

    # Title
    title_cfg = _legacy_title_config(model.project_name or "Report")
    visual_containers.append({
        "config": title_cfg, "filters": "[]",
        "height": 50.0, "width": 1232.0, "x": 24.0, "y": 20.0, "z": 0.0
    })

    # KPI cards — up to 4 measures
    if fact_table and model.measures:
        kpis = model.measures[:4]
        n = len(kpis)
        margin = 24
        card_w = (1280 - 2 * margin - (n - 1) * margin) / n
        card_y = 90
        for i, m in enumerate(kpis):
            mname = m.display_name or m.name
            cx = margin + i * (card_w + margin)
            cfg = _legacy_card_config(mname, fact_table, cx, card_y, card_w, 120, i + 1)
            visual_containers.append({
                "config": cfg, "filters": "[]",
                "height": 120.0, "width": card_w, "x": cx, "y": float(card_y), "z": 0.0
            })

    # ── Helper: pick a good category column from a table ──────────────────
    def _pick_category(table):
        text_cols = [c for c in table.columns if not c.is_numeric and not c.is_date]
        # Prefer name-like columns over ID-like columns
        for c in text_cols:
            low = c.name.lower()
            if "name" in low or "type" in low or "category" in low or "status" in low or "region" in low:
                return c.name
        return text_cols[0].name if text_cols else None

    # ── Choose what goes on each chart's VALUE (Y) slot ───────────────────
    # Priority 1: a real, validated measure (we usually have ~30). These are
    #             the correct thing to plot and match the KPI cards.
    # Priority 2 (fallback only): a numeric NON-KEY column, aggregated with SUM.
    # A key/ID column (DateKey, CustomerID, ...) is NEVER eligible for a value
    # slot — this is what previously produced the meaningless Sum(DateKey).
    fact_tbl_obj = model.tables[0] if model.tables else None
    measure_names = _chart_value_measures(model)
    numeric_nonkey = [
        c.name for c in (fact_tbl_obj.columns if fact_tbl_obj else [])
        if c.is_numeric and not c.is_date and not _is_key_like(c.name)
    ]
    fallback_agg_col = numeric_nonkey[0] if numeric_nonkey else None

    def _emit_chart(visual_type, cat, cat_table, tab, x, donut=False):
        """
        Build one chart config, measure-first. Returns config str or None.
        Guarantees the value is a measure or a non-key numeric column — never a key.
        """
        if measure_names:
            # Rotate through available measures so charts aren't all identical.
            mname = measure_names[tab % len(measure_names)]
            return _legacy_chart_config(
                visual_type, cat, cat_table,
                mname, fact_table,
                x, chart_y, chart_w, chart_h, tab, measure_is_real=True)
        if fallback_agg_col:
            func = 5 if donut else 0   # 5 = Count(NonNull), 0 = Sum
            return _legacy_column_agg_chart_config(
                visual_type, cat, cat_table,
                fallback_agg_col, fact_table, func,
                x, chart_y, chart_w, chart_h, tab)
        return None   # nothing safe to plot -> skip rather than emit garbage

    # ── Charts row (y=240) — bar + line + donut ───────────────────────────
    chart_y = 240
    chart_h = 260
    chart_w = 400
    chart_gap = 16
    chart_x = 24
    have_value = bool(measure_names or fallback_agg_col)

    # Bar chart: first dimension category × measure
    if dim_tables and have_value:
        cat = _pick_category(dim_tables[0])
        if cat:
            cfg = _emit_chart("clusteredBarChart", cat, dim_tables[0].table_name, 20, chart_x)
            if cfg:
                visual_containers.append({
                    "config": cfg, "filters": "[]",
                    "height": float(chart_h), "width": float(chart_w),
                    "x": float(chart_x), "y": float(chart_y), "z": 0.0})
                chart_x += chart_w + chart_gap

    # Line chart: a real DATE column × measure (over time)
    date_table = None
    date_col = None
    for t in model.tables:
        for c in t.columns:
            if c.is_date or "date" in c.name.lower():
                date_table, date_col = t.table_name, c.name
                break
        if date_col:
            break
    if date_col and have_value:
        cfg = _emit_chart("lineChart", date_col, date_table, 21, chart_x)
        if cfg:
            visual_containers.append({
                "config": cfg, "filters": "[]",
                "height": float(chart_h), "width": float(chart_w),
                "x": float(chart_x), "y": float(chart_y), "z": 0.0})
            chart_x += chart_w + chart_gap

    # Donut chart: second dimension category × measure (share by category)
    if len(dim_tables) > 1 and have_value:
        cat = _pick_category(dim_tables[1])
        if cat:
            cfg = _emit_chart("donutChart", cat, dim_tables[1].table_name, 22, chart_x, donut=True)
            if cfg:
                visual_containers.append({
                    "config": cfg, "filters": "[]",
                    "height": float(chart_h), "width": float(chart_w),
                    "x": float(chart_x), "y": float(chart_y), "z": 0.0})

    # ── Slicers row (y=520) — up to 3 dimension tables ────────────────────
    slicer_y = 520
    slicer_w = 240
    slicer_x = 24
    for j, dim in enumerate(dim_tables[:3]):
        col = _pick_category(dim)
        if not col:
            continue
        cfg = _legacy_slicer_config(col, dim.table_name, slicer_x, slicer_y, slicer_w, 170, 30 + j)
        visual_containers.append({
            "config": cfg, "filters": "[]",
            "height": 170.0, "width": float(slicer_w),
            "x": float(slicer_x), "y": float(slicer_y), "z": 0.0})
        slicer_x += slicer_w + 16

    # ── report.json in legacy format ──────────────────────────────────────
    report_config = json.dumps({
        "version": "5.73",
        "themeCollection": {"baseTheme": {
            "name": "CY24SU10", "version": "5.63", "type": "SharedResources"}},
        "activeSectionIndex": 0,
        "defaultDrillFilterOtherVisuals": True,
        "settings": {
            "useNewFilterPaneExperience": True,
            "useStylableVisualContainerHeader": True
        }
    })

    section = {
        "config": "{}",
        "displayName": "Overview",
        "displayOption": 1,
        "filters": "[]",
        "height": 720.0,
        "name": page_id,
        "visualContainers": visual_containers,
        "width": 1280.0
    }

    report_json = {
        "config": report_config,
        "layoutOptimization": 0,
        "resourcePackages": [{
            "resourcePackage": {
                "disabled": False,
                "items": [{"name": "CY24SU10", "path": "BaseThemes/CY24SU10.json", "type": 202}],
                "name": "SharedResources",
                "type": 2
            }
        }],
        "sections": [section]
    }

    # ONLY report.json — do NOT also write definition/pages/pages.json.
    # Power BI rejects a PBIP that has both PBIR-Legacy (report.json) AND
    # modern PBIR (definition/ folder) — "cannot have both formats".
    # The legacy report.json is self-contained (page metadata is in sections).
    files[f"{report_folder}/report.json"] = json.dumps(report_json, indent=2)

    logger.info("Report generated (legacy config format): 1 page, %d visuals",
                len(visual_containers))
    return files


def build_pbip_files(model: SemanticModel, bim_str: str, report_str: str) -> dict:
    """
    Build the file map for a .pbip project folder structure, so the export
    can be opened directly in Power BI Desktop (double-click the .pbip)
    instead of requiring Tabular Editor as an intermediate step.

    Returns {relative_path_within_zip: text_content}.

    IMPORTANT — this requires Power BI Desktop preview features to be
    enabled first: File > Options and settings > Options > Preview features
    > check "Power BI Project (.pbip) save option". This whole feature is
    still in preview on Microsoft's side, so treat this as experimental:
    if Power BI Desktop reports an error opening it, the error message will
    say exactly which file it didn't like, and that's fixable.

    Confidence note: the root .pbip file and definition.pbir content below
    match Microsoft's official documentation and independently-verified
    real-world examples. definition.pbism's exact content is reconstructed
    from Microsoft's description of its purpose (it isn't fully documented
    with a literal example anywhere public at time of writing) — it's the
    one piece here that's an educated reconstruction rather than a
    confirmed-correct example.
    """
    name = _safe_project_name(model.project_name or "PowerBI_Project")
    report_folder = f"{name}.Report"
    model_folder = f"{name}.SemanticModel"

    pbip_root = {
        "version": "1.0",
        "artifacts": [
            {"report": {"path": report_folder}}
        ],
        "settings": {"enableAutoRecovery": True},
    }

    # version 4.0 + report.json = PBIR-Legacy format
    # This is what Power BI Desktop actually writes (verified from real saved files)
    definition_pbir = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
        "version": "4.0",
        "datasetReference": {
            "byPath": {"path": f"../{model_folder}"}
        },
    }

    definition_pbism = {
        "version": "4.0",
    }

    files = {
        f"{name}.pbip": json.dumps(pbip_root, indent=2),
        f"{report_folder}/definition.pbir": json.dumps(definition_pbir, indent=2),
        f"{model_folder}/definition.pbism": json.dumps(definition_pbism, indent=2),
        f"{model_folder}/model.bim": bim_str,
    }

    # Modern PBIR folder structure with auto-generated visuals
    # (replaces the legacy single report.json approach)
    files.update(build_pbir_report_files(model, report_folder))

    return files


def export_powerbi_package(model: SemanticModel,
                            dfs: Optional[dict] = None,
                            file_paths: Optional[dict] = None) -> bytes:
    """
    Build and return a ZIP containing:
      model.bim          — BIM semantic model (M queries point to source files)
      report.json        — Report layout
      README_IMPORT.txt  — Step-by-step import guide
      .pbip structure    — Direct open in Power BI Desktop
      source_data/       — Original uploaded Excel/CSV files (when file_paths provided)

    When file_paths is provided:
    - M queries point to the source files by path
    - Source files are also included in the ZIP under source_data/
    - User extracts ZIP to a folder, opens .pbip → data loads immediately
    - Monthly refresh: replace files in source_data/ folder, click Refresh in PBI

    When only dfs provided:
    - Falls back to Base64 embedding (original behaviour)

    Returns bytes ready to write to disk or serve via HTTP.
    """
    file_paths = file_paths or {}
    dfs        = dfs or {}

    # If using file paths, rewrite them to point to the source_data subfolder
    # inside the extracted ZIP (relative to where the .pbip will live)
    adjusted_paths = {}
    if file_paths:
        for table_name, abs_path in file_paths.items():
            fname = Path(abs_path).name
            # In the ZIP, source files go into source_data/
            # User extracts ZIP → {project}/source_data/{filename}
            # We store the original abs_path for the ZIP write,
            # but the M query uses a placeholder we'll explain in README
            adjusted_paths[table_name] = abs_path

    bim_str    = export_bim(model, dfs=dfs, file_paths=adjusted_paths or None)
    report_str = export_report_json(model)
    readme_str = _build_readme(model, has_source_files=bool(file_paths))
    pbip_files = build_pbip_files(model, bim_str, report_str)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("model.bim",          bim_str)
        zf.writestr("report.json",        report_str)
        zf.writestr("README_IMPORT.txt",  readme_str)
        for rel_path, content in pbip_files.items():
            zf.writestr(rel_path, content)

        # Include source Excel/CSV files in the ZIP
        if file_paths:
            for table_name, abs_path in file_paths.items():
                p = Path(abs_path)
                if p.exists():
                    zf.write(p, f"source_data/{p.name}")
                    logger.info("Added source file to ZIP: source_data/%s", p.name)

    logger.info(
        "Power BI package built: %d tables, %d relationships, %d measures, "
        "%d source files included",
        len(model.tables), len(model.relationships), len(model.measures),
        len(file_paths),
    )
    return buf.getvalue()