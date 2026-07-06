"""
core/chat_agent.py
Conversational orchestrator. Drives the intake state machine,
negotiates errors in natural language, and manages the full pipeline.

Windows-safe: uses ctx.store_dataframe() / ctx.get_dataframe() instead of
dynamic _dataframes attribute. Export bytes stored in ctx.excel_bytes,
ctx.tmdl_json, ctx.audit_json — all proper typed fields on ProjectContext.
"""
import os
import re
import json
import time
import logging
from datetime import datetime
from typing import Optional
import requests

from .models import (
    ProjectContext, IntakeState, FileRole,
    DataError, ResolutionMethod, ResolutionOption,
    MeasureDefinition, MappingDecision, MappingMethod,
)

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-5"


def _call_claude(messages: list[dict], system: str = "",
                 max_tokens: int = 800) -> str:
    """Raw Claude API call. Returns text or fallback string."""
    if not ANTHROPIC_API_KEY:
        return _rule_based_fallback(messages)

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload: dict = {
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        payload["system"] = system

    for attempt in range(3):
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers, json=payload, timeout=30,
            )
            if resp.status_code == 200:
                return resp.json()["content"][0]["text"].strip()
            elif resp.status_code == 529:
                time.sleep(2 ** attempt)
            else:
                logger.error("Claude API %s: %s", resp.status_code, resp.text[:200])
                break
        except requests.RequestException as exc:
            logger.error("Claude API error: %s", exc)
            if attempt < 2:
                time.sleep(2 ** attempt)

    return _rule_based_fallback(messages)


def _rule_based_fallback(messages: list[dict]) -> str:
    """Simple rule-based response when API is unavailable."""
    last = messages[-1]["content"].lower() if messages else ""
    if any(w in last for w in ["hello", "hi", "start"]):
        return "Hello! I'm ready to help build your reporting model. What's the project name?"
    if any(w in last for w in ["recommend", "suggest", "best", "you decide"]):
        return ("I'd recommend the first option — it's the safest default. "
                "Would you like to go with that?")
    if any(w in last for w in ["yes", "ok", "go", "confirm", "proceed"]):
        return "Got it. Moving forward with that choice."
    return "Could you clarify your preference? I want to make sure I apply the right resolution."


# ── System prompt ──────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an AI data engineering assistant helping users build a Power BI
semantic model from their data files. You are conversational, precise, and honest.

Your responsibilities:
1. Guide users through uploading raw data, mapping files, and a protocol document
2. When data quality issues are found, explain them clearly in plain English and
   present resolution options — never resolve issues silently
3. Confirm every mapping and measure before building the model
4. Answer questions about the data, the pipeline, and the implications of decisions
5. Keep responses concise but complete — users need to understand what they're agreeing to

Current pipeline rules you must follow:
- NEVER drop, modify, or skip data without explicit user consent
- Always show the impact (row count, dollar value) before a user confirms a resolution
- For errors, always offer 2-5 options with clear trade-offs
- Always confirm the user's choice before applying it
- Treat every user message as part of a negotiation, not a command"""


# ── State prompts ──────────────────────────────────────────────────────────

def _intake_prompt(ctx: ProjectContext, state: IntakeState) -> str:
    """Generate the agent's next message for a given intake state."""
    p = ctx.project_name

    prompts = {
        IntakeState.PROJECT_INIT: (
            "Hello! I'm your AI reporting assistant. I'll guide you through "
            "building a complete semantic model from your data files.\n\n"
            "Let's start with the basics — what's the name of this reporting project?"
        ),
        IntakeState.RAW_DATA: (
            f'Project "{p}" created.\n\n'
            "Now upload your raw data files — the fact-level files containing "
            "your actual transactions, events, or records (CSV or Excel). "
            "You can upload one or multiple files.\n\n"
            "Once uploaded, I'll show you a column summary and you can confirm "
            "before we move on."
        ),
        IntakeState.MAPPING_FILES: (
            f'Raw data loaded: {len(ctx.raw_files)} file(s), '
            f'{sum(f.row_count for f in ctx.raw_files):,} total rows.\n\n'
            "Next, upload your mapping or dimension files — lookup tables, "
            "reference tables, or dimension tables that give context to your raw data "
            "(e.g. dim_product, dim_customer, region codes).\n\n"
            "If you don't have any, say 'skip' and I'll note that all joins "
            "will need to be reviewed manually."
        ),
        IntakeState.PROTOCOL: (
            f'Mapping files loaded: {len(ctx.mapping_files)} file(s).\n\n'
            "Now upload your protocol document — the file that describes what "
            "metrics and measures the report needs.\n\n"
            "Any format works: Word doc, PDF, Excel, plain text, or just paste "
            "your requirements directly in chat. No template required — write it "
            "however feels natural."
        ),
        IntakeState.CLARIFY: (
            "I've read your protocol document and extracted the measures. "
            "Before I start building, I have a few quick questions about some "
            "definitions that weren't fully clear."
        ),
        IntakeState.MAPPING_REVIEW: (
            "Protocol measures are confirmed. I've now run the automapping engine "
            "across all your files. Let me walk you through what I found."
        ),
        IntakeState.ERROR_REVIEW: (
            "Files processed. I found some data quality issues that need "
            "your input before I can build the model safely. "
            "I'll walk through each one with you now."
        ),
        IntakeState.MODEL_BUILD: (
            "All mappings confirmed and errors resolved. "
            "Building your semantic model now..."
        ),
        IntakeState.EXPORT: (
            "Your semantic model is complete.\n\n"
            "Where would you like to send it?\n"
            "• Excel workbook (with data tables, measures, and quality log)\n"
            "• JSON / TMDL (for Power BI Desktop or Azure Analysis Services)\n"
            "• Both\n\n"
            "Just say which one and I'll generate it."
        ),
        IntakeState.COMPLETE: (
            "All done. Your semantic model has been exported and the "
            "completion report has been generated. "
            "Would you like to proceed to Phase 2 — dashboard auto-generation?"
        ),
    }
    return prompts.get(state, "Ready. What would you like to do next?")


def _format_file_summary(meta) -> str:
    """One-line summary of a processed file."""
    return (
        f"• **{meta.file_name}** — {meta.row_count:,} rows, "
        f"{meta.col_count} columns"
        + (f" (PK candidates: {', '.join(meta.pk_candidates[:3])})"
           if meta.pk_candidates else "")
        + (f"\n  Auto-resolved: {'; '.join(meta.errors_detected)}"
           if meta.errors_detected else "")
    )


def _format_error_for_chat(error: DataError) -> str:
    """Format a DataError as a natural-language chat message."""
    lines = [f"**{error.category.value.replace('_', ' ').title()} — {error.sub_type}**"]
    lines.append("")
    lines.append(error.detail)

    if error.sample_data:
        lines.append("")
        lines.append("Here's a sample of the affected data:")
        for row in error.sample_data[:3]:
            row_str = "  " + " | ".join(f"{k}: {v}" for k, v in list(row.items())[:4])
            lines.append(row_str)

    if error.affected_measure_value and error.affected_measure_value > 0:
        lines.append(
            f"\nAffected measure value: **${error.affected_measure_value:,.0f}**"
        )

    lines.append("\nHow would you like to handle this? Your options:\n")
    for opt in error.options:
        safe_marker = "" if opt.is_safe else " ⚠️"
        rec_marker = " *(recommended)*" if opt.is_recommended else ""
        lines.append(
            f"**{opt.option_id.upper()})** {opt.label}{safe_marker}{rec_marker}\n"
            f"   → {opt.impact_summary}"
        )

    lines.append(
        "\nJust tell me which option you prefer — or ask me anything about "
        "the impact before deciding."
    )
    return "\n".join(lines)


def _format_mappings_for_chat(decisions: list[MappingDecision]) -> str:
    """Format mapping decisions for chat review."""
    exact = [d for d in decisions if d.method == MappingMethod.EXACT]
    fuzzy = [d for d in decisions if d.method == MappingMethod.FUZZY]
    ai    = [d for d in decisions if d.method == MappingMethod.AI_INFERRED]
    none_ = [d for d in decisions if d.method == MappingMethod.UNMAPPED]

    lines = []
    if exact:
        lines.append(
            f"**✅ Auto-confirmed ({len(exact)}) — exact name match, no review needed:**"
        )
        for d in exact[:5]:
            lines.append(
                f"  `{d.source_table}.{d.source_column}` → "
                f"`{d.target_table}.{d.target_column}`"
            )
        if len(exact) > 5:
            lines.append(f"  *(+ {len(exact)-5} more)*")

    if fuzzy:
        lines.append(
            f"\n**⚠️ Needs review ({len(fuzzy)}) — fuzzy match:**"
        )
        for d in fuzzy[:5]:
            lines.append(
                f"  `{d.source_table}.{d.source_column}` → "
                f"`{d.target_table}.{d.target_column}` "
                f"[{d.confidence:.0%} confident — "
                f"name similarity {d.string_similarity:.0%}, "
                f"value overlap {d.value_overlap:.0%}]"
            )

    if ai:
        lines.append(
            f"\n**🤖 AI inferred ({len(ai)}) — please review carefully:**"
        )
        for d in ai[:5]:
            lines.append(
                f"  `{d.source_table}.{d.source_column}` → "
                f"`{d.target_table}.{d.target_column}` "
                f"[{d.confidence:.0%}]"
                + (f"\n    Reason: {d.ai_reasoning}" if d.ai_reasoning else "")
            )

    if none_:
        lines.append(f"\n**❌ Could not map ({len(none_)}):**")
        for d in none_[:5]:
            lines.append(
                f"  `{d.source_table}.{d.source_column}` — "
                "please assign manually or mark as unused"
            )

    lines.append(
        "\nReply **'confirm all'** to accept all suggested mappings, or "
        "tell me any you'd like to change — e.g. "
        "'prod_code should map to dim_sku.sku_id instead'."
    )
    return "\n".join(lines)


# ── Resolution matching ────────────────────────────────────────────────────

def _match_option(user_text: str, error: DataError) -> Optional[str]:
    """
    Try to extract which resolution option the user chose.
    Returns option_id ('a', 'b', ...) or None.
    """
    low = user_text.lower().strip()

    # Direct letter: "a", "option a", "go with a"
    m = re.search(r'\b([a-e])\b', low)
    if m:
        opt_id = m.group(1)
        if any(o.option_id == opt_id for o in error.options):
            return opt_id

    # Match by label keywords
    for opt in error.options:
        keywords = re.sub(r'[^a-z ]', '', opt.label.lower()).split()
        matches = sum(1 for kw in keywords if kw in low and len(kw) > 3)
        if matches >= 2:
            return opt.option_id

    # "recommended" / "best" / "you decide" / "just do it"
    if any(w in low for w in ['recommend', 'best', 'you decide', 'just do',
                               'whatever', 'default', 'go ahead']):
        if error.recommended_option_id:
            return error.recommended_option_id

    # "fix" / "source" / "stop" -> last option (usually the stop option)
    if any(w in low for w in ['fix', 'source file', 'stop', 'pause']):
        last = error.options[-1]
        if not last.is_safe:
            return last.option_id

    return None


def _match_confirm_mappings(user_text: str) -> bool:
    """Did the user say to confirm all mappings?"""
    low = user_text.lower()
    return any(w in low for w in [
        'confirm all', 'looks good', 'all good', 'approve all',
        'accept all', 'yes', 'proceed', 'continue', 'ok', 'good'
    ])


# ── DataFrame helpers ──────────────────────────────────────────────────────

def _store_df(ctx: ProjectContext, table_name: str, df) -> None:
    """
    Store a DataFrame into ctx.dataframe_store as parquet bytes.
    Falls back to pickle if pyarrow is unavailable.
    Windows-safe: no dynamic attributes, no process-boundary issues.
    """
    import io
    buf = io.BytesIO()
    try:
        df.to_parquet(buf, index=True, engine='pyarrow')
    except Exception:
        try:
            df.to_parquet(buf, index=True)
        except Exception:
            # Final fallback: pickle (works everywhere)
            import pickle
            buf = io.BytesIO(pickle.dumps(df))
    ctx.dataframe_store[table_name] = buf.getvalue()


def _get_df(ctx: ProjectContext, table_name: str):
    """
    Retrieve a DataFrame from ctx.dataframe_store.
    Returns None if not found.
    """
    import io, pandas as pd
    data = ctx.dataframe_store.get(table_name)
    if data is None:
        return None
    try:
        return pd.read_parquet(io.BytesIO(data))
    except Exception:
        try:
            import pickle
            return pickle.loads(data)
        except Exception:
            return None


def _get_all_dfs(ctx: ProjectContext) -> dict:
    """Return all stored DataFrames as {table_name: DataFrame}."""
    return {
        name: _get_df(ctx, name)
        for name in ctx.dataframe_store
        if _get_df(ctx, name) is not None
    }


def _build_file_pairs(ctx: ProjectContext, file_metas):
    """Return [(FileMeta, DataFrame|None)] pairs from stored DataFrames."""
    return [
        (meta, _get_df(ctx, meta.table_name))
        for meta in file_metas
    ]


# ── Main agent function ────────────────────────────────────────────────────

def process_message(
    ctx: ProjectContext,
    user_message: str,
    attached_file: Optional[tuple] = None,
) -> tuple[str, ProjectContext]:
    """
    Process one user turn. Returns (agent_reply, updated_ctx).
    Single entry point for all chat interaction.
    All state is stored in ctx typed fields — no dynamic attributes.

    attached_file can be:
      (fname, content)              — legacy 2-tuple (Base64 embedding)
      (fname, content, saved_path)  — 3-tuple when file was saved to disk
                                      (M queries point to saved_path)
    """
    ctx.add_message("user", user_message)

    # ── Lazy imports to avoid circular deps ──────────────────────────────
    from .file_processor import process_file, apply_error_resolution
    from .automapper import run_automapping
    from .protocol_parser import (
        extract_protocol_text, extract_measures,
        detect_protocol_conflicts, detect_dependency_cycles,
    )
    from .model_builder import assemble_model, export_excel, export_tmdl_json, export_audit_log

    reply = ""
    low = user_message.lower().strip()

    # Parse attached_file — support both 2-tuple and 3-tuple
    def _unpack_file(af):
        if af is None:
            return None, None, None
        if len(af) == 3:
            return af[0], af[1], af[2]
        return af[0], af[1], None

    # ── STATE: PROJECT_INIT ───────────────────────────────────────────────
    if ctx.intake_state == IntakeState.PROJECT_INIT:
        name = user_message.strip().strip('"\'')
        if len(name) < 2:
            reply = "Please provide a project name (at least 2 characters)."
        else:
            ctx.project_name = name
            ctx.intake_state = IntakeState.RAW_DATA
            reply = _intake_prompt(ctx, IntakeState.RAW_DATA)

    # ── STATE: RAW_DATA ───────────────────────────────────────────────────
    elif ctx.intake_state == IntakeState.RAW_DATA:
        if attached_file:
            fname, fcontent, saved_path = _unpack_file(attached_file)
            meta, df, errs = process_file(
                fcontent, fname, FileRole.RAW_DATA, ctx.project_id
            )
            ctx.raw_files.append(meta)
            _store_df(ctx, meta.table_name, df)
            # Store file path for M query generation
            if saved_path:
                if not hasattr(ctx, 'file_paths'):
                    ctx.file_paths = {}
                ctx.file_paths[meta.table_name] = saved_path
            ctx.errors.extend(errs)

            summary = _format_file_summary(meta)
            reply = (
                f"Loaded **{fname}**:\n{summary}\n\n"
                f"Upload more raw data files, or say **'done'** to continue "
                f"to mapping files."
            )
        elif any(w in low for w in ['done', 'next', 'continue', 'proceed',
                                     'no more', "that's all", "that's it"]):
            if not ctx.raw_files:
                reply = "Please upload at least one raw data file before continuing."
            else:
                ctx.intake_state = IntakeState.MAPPING_FILES
                reply = _intake_prompt(ctx, IntakeState.MAPPING_FILES)
        else:
            reply = (
                "Please upload your raw data files (CSV or Excel). "
                "When you're done uploading, say 'done' to continue."
            )

    # ── STATE: MAPPING_FILES ──────────────────────────────────────────────
    elif ctx.intake_state == IntakeState.MAPPING_FILES:
        if attached_file:
            fname, fcontent, saved_path = _unpack_file(attached_file)
            meta, df, errs = process_file(
                fcontent, fname, FileRole.MAPPING, ctx.project_id
            )
            ctx.mapping_files.append(meta)
            _store_df(ctx, meta.table_name, df)
            # Store file path for M query generation
            if saved_path:
                if not hasattr(ctx, 'file_paths'):
                    ctx.file_paths = {}
                ctx.file_paths[meta.table_name] = saved_path
            ctx.errors.extend(errs)

            summary = _format_file_summary(meta)
            reply = (
                f"Loaded **{fname}**:\n{summary}\n\n"
                f"Upload more mapping files, or say **'done'** to continue "
                f"to the protocol document."
            )
        elif any(w in low for w in ['skip', 'no mapping', "don't have",
                                     'none', 'no dimension']):
            ctx.intake_state = IntakeState.PROTOCOL
            reply = (
                "No mapping files — noted. All join relationships will be "
                "presented for manual review during the mapping step.\n\n"
                + _intake_prompt(ctx, IntakeState.PROTOCOL)
            )
        elif any(w in low for w in ['done', 'next', 'continue', 'proceed',
                                     "that's all", "that's it"]):
            ctx.intake_state = IntakeState.PROTOCOL
            reply = _intake_prompt(ctx, IntakeState.PROTOCOL)
        else:
            reply = (
                "Please upload your dimension/mapping files. "
                "Say 'done' when finished, or 'skip' if you have none."
            )

    # ── STATE: PROTOCOL ───────────────────────────────────────────────────
    elif ctx.intake_state == IntakeState.PROTOCOL:
        protocol_text = ""

        if attached_file:
            fname, fcontent, _ = _unpack_file(attached_file)
            protocol_text = extract_protocol_text(fcontent, fname)
            ctx.protocol_file_name = fname
        elif len(user_message.strip()) > 30:
            protocol_text = user_message.strip()
            ctx.protocol_file_name = "pasted_protocol"
        else:
            reply = (
                "Please upload your protocol document (Word, PDF, Excel, TXT) "
                "or paste your requirements directly in chat."
            )
            ctx.add_message("assistant", reply)
            return reply, ctx

        ctx.protocol_text = protocol_text

        # Collect all known columns for context
        all_cols = []
        for meta in ctx.raw_files + ctx.mapping_files:
            all_cols.extend(c.name for c in meta.columns)

        measures = extract_measures(protocol_text, all_cols)
        ctx.measures = measures

        conflict_errors = detect_protocol_conflicts(protocol_text, measures)
        cycle_errors = detect_dependency_cycles(measures)
        ctx.errors.extend(conflict_errors + cycle_errors)

        needs_clarify = [m for m in measures if m.needs_clarification]
        measure_summary = "\n".join(
            f"  {'⚠️' if m.needs_clarification else '✓'} **{m.display_name}** "
            f"({m.aggregation.value}, {m.format_string}) "
            f"[{m.confidence:.0%} confidence]"
            for m in measures
        )

        reply = (
            f"I've analysed your protocol document and extracted "
            f"**{len(measures)} measures**:\n\n"
            f"{measure_summary}\n\n"
        )

        if needs_clarify:
            ctx.intake_state = IntakeState.CLARIFY
            first_q = needs_clarify[0]
            reply += (
                f"Before I continue, I need to clarify a couple of things.\n\n"
                f"**{first_q.display_name}:**\n"
                + "\n".join(f"• {q}" for q in first_q.clarification_questions)
            )
        else:
            ctx.intake_state = IntakeState.MAPPING_REVIEW
            reply += "All measures look clear. Running automapping now...\n\n"

            raw_pairs = _build_file_pairs(ctx, ctx.raw_files)
            map_pairs = _build_file_pairs(ctx, ctx.mapping_files)
            raw_pairs = [(m, df) for m, df in raw_pairs if df is not None]
            map_pairs = [(m, df) for m, df in map_pairs if df is not None]

            if raw_pairs:
                decisions, ref_errors = run_automapping(
                    raw_pairs, map_pairs, ctx.project_name
                )
                ctx.mapping_decisions = decisions
                ctx.errors.extend(ref_errors)
                reply += _format_mappings_for_chat(decisions)
            else:
                reply += "No data loaded yet — please upload files first."

    # ── STATE: CLARIFY ────────────────────────────────────────────────────
    elif ctx.intake_state == IntakeState.CLARIFY:
        pending = [m for m in ctx.measures if m.needs_clarification and
                   not m.clarification_answers]
        if pending:
            m = pending[0]
            m.clarification_answers['user_response'] = user_message
            m.needs_clarification = False

        still_pending = [m for m in ctx.measures if m.needs_clarification]
        if still_pending:
            next_m = still_pending[0]
            reply = (
                f"Got it. One more question:\n\n"
                f"**{next_m.display_name}:**\n"
                + "\n".join(f"• {q}" for q in next_m.clarification_questions)
            )
        else:
            ctx.intake_state = IntakeState.MAPPING_REVIEW
            reply = "All clarified. Running automapping now...\n\n"

            raw_pairs = _build_file_pairs(ctx, ctx.raw_files)
            map_pairs = _build_file_pairs(ctx, ctx.mapping_files)
            raw_pairs = [(m, df) for m, df in raw_pairs if df is not None]
            map_pairs = [(m, df) for m, df in map_pairs if df is not None]

            if raw_pairs:
                decisions, ref_errors = run_automapping(
                    raw_pairs, map_pairs, ctx.project_name
                )
                ctx.mapping_decisions = decisions
                ctx.errors.extend(ref_errors)
                reply += _format_mappings_for_chat(decisions)

    # ── STATE: MAPPING_REVIEW ─────────────────────────────────────────────
    elif ctx.intake_state == IntakeState.MAPPING_REVIEW:
        if _match_confirm_mappings(low):
            for d in ctx.mapping_decisions:
                if not d.user_confirmed:
                    d.user_confirmed = True
                    d.confirmed_at = datetime.now()

            pending_errors = ctx.pending_errors()
            if pending_errors:
                ctx.intake_state = IntakeState.ERROR_REVIEW
                reply = (
                    f"Mappings confirmed.\n\n"
                    f"Before I build the model, I need to flag "
                    f"**{len(pending_errors)} data quality issue(s)**. "
                    f"I'll walk through each one.\n\n"
                    + _format_error_for_chat(pending_errors[0])
                )
            else:
                ctx.intake_state = IntakeState.MODEL_BUILD
                reply = _intake_prompt(ctx, IntakeState.MODEL_BUILD)
        else:
            system = (
                "The user is reviewing column-to-dimension mappings. "
                "They may be confirming, rejecting, or overriding specific mappings. "
                "Respond helpfully and ask for clarification if needed."
            )
            reply = _call_claude(
                [{"role": "user", "content": user_message}],
                system=system, max_tokens=400,
            )

    # ── STATE: ERROR_REVIEW ───────────────────────────────────────────────
    elif ctx.intake_state == IntakeState.ERROR_REVIEW:
        pending = ctx.pending_errors()

        if not pending:
            ctx.intake_state = IntakeState.MODEL_BUILD
            reply = _intake_prompt(ctx, IntakeState.MODEL_BUILD)
        else:
            current_error = pending[0]
            opt_id = _match_option(user_message, current_error)

            if opt_id:
                df_key = current_error.affected_table
                df = _get_df(ctx, df_key)

                if df is not None:
                    updated_df, updated_error = apply_error_resolution(
                        df, current_error, opt_id
                    )
                    _store_df(ctx, df_key, updated_df)
                    for i, e in enumerate(ctx.errors):
                        if e.error_id == current_error.error_id:
                            ctx.errors[i] = updated_error
                            break
                else:
                    current_error.chosen_option_id = opt_id
                    current_error.resolution_method = ResolutionMethod.USER_DECISION
                    current_error.resolved_at = datetime.now()

                chosen_opt = next(
                    (o for o in current_error.options if o.option_id == opt_id), None
                )
                reply = (
                    f"✓ Applied: **{chosen_opt.label if chosen_opt else opt_id}**\n\n"
                    + (f"{current_error.resolution_notes}\n\n"
                       if current_error.resolution_notes else "")
                )

                still_pending = ctx.pending_errors()
                if still_pending:
                    reply += _format_error_for_chat(still_pending[0])
                else:
                    ctx.intake_state = IntakeState.MODEL_BUILD
                    reply += (
                        "All issues resolved. "
                        + _intake_prompt(ctx, IntakeState.MODEL_BUILD)
                    )
            else:
                context_str = (
                    f"Current error being discussed:\n{current_error.detail}\n\n"
                    f"Options available: "
                    + ", ".join(f"{o.option_id}) {o.label}" for o in current_error.options)
                )
                system = (
                    "You are helping a user resolve a data quality issue in their "
                    "reporting pipeline. Answer their question about the issue "
                    "clearly and concisely, then remind them of their options.\n\n"
                    + context_str
                )
                reply = _call_claude(
                    [{"role": "user", "content": user_message}],
                    system=system, max_tokens=500,
                )

    # ── STATE: MODEL_BUILD ────────────────────────────────────────────────
    elif ctx.intake_state == IntakeState.MODEL_BUILD:
        try:
            model = assemble_model(ctx)
            ctx.semantic_model = model
            ctx.intake_state = IntakeState.EXPORT

            reply = (
                f"✅ **Semantic model built successfully.**\n\n"
                f"• **{len(model.tables)}** tables\n"
                f"• **{len(model.relationships)}** relationships\n"
                f"• **{len(model.measures)}** measures (all DAX validated)\n"
                f"• **{model.total_rows:,}** total rows\n"
                + (f"• {len(model.build_warnings)} warning(s)\n"
                   if model.build_warnings else "")
                + "\n" + _intake_prompt(ctx, IntakeState.EXPORT)
            )
        except Exception as exc:
            logger.exception("Model build failed")
            reply = (
                f"The model build encountered an error: {exc}\n\n"
                "Please check your files and try again, or ask me to diagnose the issue."
            )

    # ── STATE: EXPORT ─────────────────────────────────────────────────────
    elif ctx.intake_state == IntakeState.EXPORT:
        if not ctx.semantic_model:
            reply = "No model available yet. Let me build it first."
            ctx.intake_state = IntakeState.MODEL_BUILD
        else:
            model = ctx.semantic_model
            dfs = _get_all_dfs(ctx)

            exports_done = []

            if any(w in low for w in ['excel', 'xlsx', 'both', 'all']):
                xlsx_bytes = export_excel(model, dfs)
                ctx.excel_bytes = xlsx_bytes
                exports_done.append("Excel workbook")

            if any(w in low for w in ['power bi', 'powerbi', 'pbi', 'bim', 'both', 'all']):
                from .pbi_exporter import export_powerbi_package
                file_paths = getattr(ctx, 'file_paths', {})
                ctx.pbi_zip = export_powerbi_package(model, dfs, file_paths)
                exports_done.append("Power BI package (model.bim + .pbip + import guide)")

            if any(w in low for w in ['json', 'tmdl', 'both', 'all']):
                ctx.tmdl_json = export_tmdl_json(model)
                ctx.audit_json = export_audit_log(model)
                exports_done.append("TMDL JSON")
                exports_done.append("audit log JSON")

            if exports_done:
                ctx.intake_state = IntakeState.COMPLETE
                reply = (
                    f"Generated: {', '.join(exports_done)}.\n\n"
                    "I'll now generate your **Project Completion Report** as a PDF. "
                    "One moment..."
                )
            else:
                reply = (
                    "Which format would you like?\n"
                    "• **Excel** — workbook with data tables, measures, and quality log\n"
                    "• **JSON/TMDL** — for Power BI Desktop\n"
                    "• **Both** — all of the above"
                )

    # ── STATE: COMPLETE ───────────────────────────────────────────────────
    elif ctx.intake_state == IntakeState.COMPLETE:
        if any(w in low for w in ['dashboard', 'phase 2', 'visualise',
                                   'visualize', 'report', 'yes']):
            reply = (
                "Phase 2 — dashboard auto-generation — is ready to run. "
                "I'll analyse your semantic model and generate Power BI report "
                "layouts automatically. Starting now..."
            )
        else:
            reply = (
                "Your project is complete. The semantic model and all deliverables "
                "have been exported. Let me know if you need anything else."
            )

    # ── FALLBACK ──────────────────────────────────────────────────────────
    else:
        history = ctx.chat_history[-8:]
        reply = _call_claude(history, system=SYSTEM_PROMPT, max_tokens=600)

    ctx.add_message("assistant", reply)
    return reply, ctx