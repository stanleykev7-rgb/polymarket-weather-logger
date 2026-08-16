"""
Performance report generation for the Weather Market Data Explorer.

Builds a "day-by-day model performance across lead times" report as
either a PNG image (quick visual, one page worth of charts) or a DOCX
document (more detail, includes data tables and narrative insights),
for either all cities combined or a single city.

This module is intentionally decoupled from Streamlit -- it takes a
plain pandas DataFrame in and returns raw bytes out -- so it can be
tested and reasoned about without spinning up the app itself.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone

import matplotlib
matplotlib.use("Agg")  # no display backend needed/available server-side
import matplotlib.pyplot as plt
import pandas as pd

LEAD_TIME_BINS = [-float("inf"), 24, 48, 72, 168, float("inf")]
LEAD_TIME_LABELS = ["<24h", "24-48h", "48-72h", "3-7d", "7d+"]

MODEL_HIT_COLS = {
    "ECMWF": "ecmwf_hit",
    "GFS": "gfs_hit",
    "Market Favorite": "market_favorite_hit",
}


# ---------------------------------------------------------------------------
# Data prep
# ---------------------------------------------------------------------------
def _settled_subset(df: pd.DataFrame, city: str | None) -> pd.DataFrame:
    """Rows with at least one resolved hit flag, optionally filtered to
    one city. Doesn't require ALL three hit columns to be non-null --
    older rows may only have ecmwf_hit populated (gfs_hit/
    market_favorite_hit didn't exist pre-schema-v2), and that's fine;
    each metric below handles its own column's nulls independently.
    """
    hit_cols = [c for c in MODEL_HIT_COLS.values() if c in df.columns]
    if not hit_cols:
        return df.iloc[0:0]
    mask = pd.Series(False, index=df.index)
    for c in hit_cols:
        mask = mask | df[c].isin([True, False])
    out = df[mask].copy()
    if city:
        out = out[out["city"] == city]
    return out


def compute_daily_performance(df: pd.DataFrame, city: str | None = None) -> pd.DataFrame:
    """One row per target_date: hit rate for each model, plus sample size."""
    settled = _settled_subset(df, city)
    if settled.empty:
        return pd.DataFrame(columns=["target_date", "n"] + [f"{m}_hit_rate" for m in MODEL_HIT_COLS])

    rows = []
    for target_date, group in settled.groupby("target_date"):
        row = {"target_date": target_date, "n": len(group)}
        for model, col in MODEL_HIT_COLS.items():
            if col in group.columns:
                vals = group[col].dropna()
                row[f"{model}_hit_rate"] = vals.mean() if len(vals) else None
                row[f"{model}_n"] = len(vals)
            else:
                row[f"{model}_hit_rate"] = None
                row[f"{model}_n"] = 0
        rows.append(row)
    out = pd.DataFrame(rows).sort_values("target_date")
    return out


def compute_lead_time_performance(df: pd.DataFrame, city: str | None = None) -> pd.DataFrame:
    """Hit rate for each model, binned by lead time at observation time."""
    settled = _settled_subset(df, city)
    if settled.empty or "lead_time_hours" not in settled.columns:
        return pd.DataFrame(columns=["lead_bucket", "n"] + [f"{m}_hit_rate" for m in MODEL_HIT_COLS])

    settled = settled.copy()
    settled["lead_bucket"] = pd.cut(
        settled["lead_time_hours"], bins=LEAD_TIME_BINS, labels=LEAD_TIME_LABELS
    )
    rows = []
    for bucket in LEAD_TIME_LABELS:
        group = settled[settled["lead_bucket"] == bucket]
        if group.empty:
            continue
        row = {"lead_bucket": bucket, "n": len(group)}
        for model, col in MODEL_HIT_COLS.items():
            if col in group.columns:
                vals = group[col].dropna()
                row[f"{model}_hit_rate"] = vals.mean() if len(vals) else None
                row[f"{model}_n"] = len(vals)
            else:
                row[f"{model}_hit_rate"] = None
                row[f"{model}_n"] = 0
        rows.append(row)
    return pd.DataFrame(rows)


def compute_overall_summary(df: pd.DataFrame, city: str | None = None) -> dict:
    settled = _settled_subset(df, city)
    summary = {"scope": city or "All Cities", "settled_n": len(settled)}
    for model, col in MODEL_HIT_COLS.items():
        if col in settled.columns:
            vals = settled[col].dropna()
            summary[f"{model}_hit_rate"] = vals.mean() if len(vals) else None
            summary[f"{model}_n"] = len(vals)
        else:
            summary[f"{model}_hit_rate"] = None
            summary[f"{model}_n"] = 0
    return summary


def generate_insights(summary: dict, daily: pd.DataFrame, lead: pd.DataFrame) -> list[str]:
    """Small set of auto-generated, plainly-worded observations. These
    are DESCRIPTIVE statements about what's in the data, not trading
    recommendations -- deliberately no "buy/sell"/"edge" language.
    """
    insights = []
    n = summary.get("settled_n", 0)

    if n < 20:
        insights.append(
            f"Only {n} settled observations in this report -- treat every "
            "number below as preliminary. Model comparisons typically need "
            "dozens of resolved outcomes per slice before differences are "
            "distinguishable from noise."
        )

    rates = {m: summary.get(f"{m}_hit_rate") for m in MODEL_HIT_COLS}
    valid_rates = {m: r for m, r in rates.items() if r is not None}
    if len(valid_rates) >= 2:
        best = max(valid_rates, key=valid_rates.get)
        worst = min(valid_rates, key=valid_rates.get)
        if best != worst:
            insights.append(
                f"{best} had the highest bucket hit rate in this sample "
                f"({valid_rates[best]*100:.0f}%), vs {worst} at "
                f"{valid_rates[worst]*100:.0f}%."
            )

    if not lead.empty and "ECMWF_hit_rate" in lead.columns:
        valid = lead.dropna(subset=["ECMWF_hit_rate"])
        if len(valid) >= 2:
            first, last = valid.iloc[0], valid.iloc[-1]
            direction = "higher" if last["ECMWF_hit_rate"] > first["ECMWF_hit_rate"] else "lower"
            insights.append(
                f"ECMWF's hit rate at {last['lead_bucket']} lead time "
                f"({last['ECMWF_hit_rate']*100:.0f}%) was {direction} than "
                f"at {first['lead_bucket']} ({first['ECMWF_hit_rate']*100:.0f}%) "
                "in this sample."
            )

    if summary.get("Market Favorite_hit_rate") is not None:
        insights.append(
            f"The market's own favorite (highest-priced) bucket won "
            f"{summary['Market Favorite_hit_rate']*100:.0f}% of the time "
            f"in this sample -- this is a baseline for how often 'the "
            "crowd' is simply right, independent of either model."
        )

    if not insights:
        insights.append("Not enough settled data yet to generate insights.")

    return insights


# ---------------------------------------------------------------------------
# Chart helpers (shared between PNG and DOCX paths)
# ---------------------------------------------------------------------------
_COLORS = {"ECMWF": "#4C72B0", "GFS": "#DD8452", "Market Favorite": "#55A868"}


def _plot_hit_rate_by(ax, table: pd.DataFrame, x_col: str, title: str):
    if table.empty:
        ax.text(0.5, 0.5, "Not enough data", ha="center", va="center", transform=ax.transAxes)
        ax.set_title(title)
        return
    x = range(len(table))
    width = 0.25
    for i, model in enumerate(MODEL_HIT_COLS):
        rate_col = f"{model}_hit_rate"
        if rate_col not in table.columns:
            continue
        offsets = [xi + (i - 1) * width for xi in x]
        ax.bar(offsets, table[rate_col].fillna(0), width=width, label=model, color=_COLORS[model])
    ax.set_xticks(list(x))
    ax.set_xticklabels(table[x_col].astype(str), rotation=30, ha="right")
    ax.set_ylim(0, 1)
    ax.set_ylabel("Hit Rate")
    ax.set_title(title)
    ax.legend(fontsize=8)


def _render_charts_figure(daily: pd.DataFrame, lead: pd.DataFrame, scope_label: str) -> plt.Figure:
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
    _plot_hit_rate_by(axes[0], daily, "target_date", f"Hit Rate by Day - {scope_label}")
    _plot_hit_rate_by(axes[1], lead, "lead_bucket", f"Hit Rate by Lead Time - {scope_label}")
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# PNG report
# ---------------------------------------------------------------------------
def build_png_report(df: pd.DataFrame, city: str | None = None) -> bytes:
    scope_label = city or "All Cities"
    summary = compute_overall_summary(df, city)
    daily = compute_daily_performance(df, city)
    lead = compute_lead_time_performance(df, city)
    insights = generate_insights(summary, daily, lead)

    fig = plt.figure(figsize=(11, 7.5))
    gs = fig.add_gridspec(3, 2, height_ratios=[0.7, 2.4, 1.1], hspace=0.55)

    # Header
    ax_header = fig.add_subplot(gs[0, :])
    ax_header.axis("off")
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header_text = (
        f"Weather Market Model Performance Report -- {scope_label}\n"
        f"Generated {generated}  |  Settled observations: {summary['settled_n']}"
    )
    ax_header.text(0, 0.6, header_text, fontsize=13, fontweight="bold", va="top")
    hit_rate_line = "   ".join(
        f"{m}: {summary[f'{m}_hit_rate']*100:.0f}% (n={summary[f'{m}_n']})"
        if summary.get(f"{m}_hit_rate") is not None else f"{m}: n/a"
        for m in MODEL_HIT_COLS
    )
    ax_header.text(0, 0.1, hit_rate_line, fontsize=10, va="top")

    ax1 = fig.add_subplot(gs[1, 0])
    _plot_hit_rate_by(ax1, daily, "target_date", "Hit Rate by Day")
    ax2 = fig.add_subplot(gs[1, 1])
    _plot_hit_rate_by(ax2, lead, "lead_bucket", "Hit Rate by Lead Time")

    ax_insights = fig.add_subplot(gs[2, :])
    ax_insights.axis("off")
    insight_text = "Insights:\n" + "\n".join(f"- {i}" for i in insights)
    ax_insights.text(0, 1.0, insight_text, fontsize=9, va="top", wrap=True)

    caveat = (
        "Settlement uses official station data where available (NOAA/HKO), "
        "an Open-Meteo reanalysis proxy otherwise -- see SCHEMA.md. "
        "This report is descriptive, not a trading signal."
    )
    fig.text(0.02, 0.005, caveat, fontsize=7, color="gray")

    fig.tight_layout(rect=[0, 0.02, 1, 0.98])
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# DOCX report
# ---------------------------------------------------------------------------
def build_docx_report(df: pd.DataFrame, city: str | None = None) -> bytes:
    from docx import Document
    from docx.shared import Inches, Pt
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    scope_label = city or "All Cities"
    summary = compute_overall_summary(df, city)
    daily = compute_daily_performance(df, city)
    lead = compute_lead_time_performance(df, city)
    insights = generate_insights(summary, daily, lead)

    doc = Document()

    title = doc.add_heading(f"Weather Market Model Performance Report", level=1)
    subtitle = doc.add_paragraph()
    subtitle.add_run(f"Scope: {scope_label}").bold = True
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    doc.add_paragraph(f"Generated: {generated}")
    doc.add_paragraph(f"Settled observations included: {summary['settled_n']}")

    doc.add_paragraph(
        "Settlement uses official station data where available (NOAA/NWS for "
        "New York, Chicago, Miami; Hong Kong Observatory for Hong Kong), and "
        "an Open-Meteo reanalysis proxy for all other cities. See SCHEMA.md "
        "for details. This report is descriptive only -- it is not a trading "
        "recommendation."
    )

    doc.add_heading("Overall Hit Rates", level=2)
    table = doc.add_table(rows=1, cols=3)
    table.style = "Light Grid Accent 1"
    hdr = table.rows[0].cells
    hdr[0].text, hdr[1].text, hdr[2].text = "Model", "Hit Rate", "Sample Size (n)"
    for model in MODEL_HIT_COLS:
        row = table.add_row().cells
        rate = summary.get(f"{model}_hit_rate")
        row[0].text = model
        row[1].text = f"{rate*100:.1f}%" if rate is not None else "n/a"
        row[2].text = str(summary.get(f"{model}_n", 0))

    doc.add_heading("Charts", level=2)
    fig = _render_charts_figure(daily, lead, scope_label)
    img_buf = io.BytesIO()
    fig.savefig(img_buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    img_buf.seek(0)
    doc.add_picture(img_buf, width=Inches(6.3))

    doc.add_heading("Hit Rate by Day", level=2)
    if daily.empty:
        doc.add_paragraph("No settled data available for this scope yet.")
    else:
        t = doc.add_table(rows=1, cols=1 + len(MODEL_HIT_COLS) + 1)
        t.style = "Light Grid Accent 1"
        hdr = t.rows[0].cells
        hdr[0].text = "Target Date"
        for i, model in enumerate(MODEL_HIT_COLS, start=1):
            hdr[i].text = f"{model} Hit Rate"
        hdr[-1].text = "n"
        for _, r in daily.iterrows():
            cells = t.add_row().cells
            cells[0].text = str(r["target_date"])
            for i, model in enumerate(MODEL_HIT_COLS, start=1):
                rate = r.get(f"{model}_hit_rate")
                cells[i].text = f"{rate*100:.0f}%" if pd.notnull(rate) else "n/a"
            cells[-1].text = str(int(r["n"]))

    doc.add_heading("Hit Rate by Lead Time", level=2)
    if lead.empty:
        doc.add_paragraph("No settled data available for this scope yet.")
    else:
        t2 = doc.add_table(rows=1, cols=1 + len(MODEL_HIT_COLS) + 1)
        t2.style = "Light Grid Accent 1"
        hdr = t2.rows[0].cells
        hdr[0].text = "Lead Time"
        for i, model in enumerate(MODEL_HIT_COLS, start=1):
            hdr[i].text = f"{model} Hit Rate"
        hdr[-1].text = "n"
        for _, r in lead.iterrows():
            cells = t2.add_row().cells
            cells[0].text = str(r["lead_bucket"])
            for i, model in enumerate(MODEL_HIT_COLS, start=1):
                rate = r.get(f"{model}_hit_rate")
                cells[i].text = f"{rate*100:.0f}%" if pd.notnull(rate) else "n/a"
            cells[-1].text = str(int(r["n"]))

    doc.add_heading("Insights", level=2)
    for insight in insights:
        doc.add_paragraph(insight, style="List Bullet")

    out = io.BytesIO()
    doc.save(out)
    out.seek(0)
    return out.getvalue()
