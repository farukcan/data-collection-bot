"""Plotly table images for Telegram commands and the LLM agent."""
from pathlib import Path

import plotly.graph_objects as go

HEADER_FILL = "#4472C4"


def rows_to_columns(headers: list[str], rows: list[list[str]]) -> list[list[str]]:
    """Transpose row-oriented data into Plotly column lists."""
    if len(rows) == 0:
        return [[] for _ in headers]
    width = len(headers)
    columns: list[list[str]] = []
    for i in range(width):
        col: list[str] = []
        for row in rows:
            if i >= len(row):
                raise ValueError(f"row has {len(row)} cells, expected {width}")
            col.append(str(row[i]))
        columns.append(col)
    return columns


def render_table_image(
    path: Path | str,
    headers: list[str],
    columns: list[list[str]],
    *,
    width: int = 1100,
    row_height: int = 28,
    base_height: int = 36,
    min_height: int = 120,
    scale: int = 2,
) -> None:
    if len(columns) != len(headers):
        raise ValueError("headers and columns length mismatch")
    row_count = len(columns[0]) if columns else 0
    for col in columns:
        if len(col) != row_count:
            raise ValueError("column row count mismatch")

    fig = go.Figure(
        data=[
            go.Table(
                header=dict(
                    values=headers,
                    fill_color=HEADER_FILL,
                    font=dict(color="white", size=12),
                    align="left",
                ),
                cells=dict(values=columns, align="left"),
            )
        ]
    )
    fig.update_layout(margin=dict(l=8, r=8, t=8, b=8))
    height = max(min_height, base_height + row_height * max(row_count, 1))
    fig.write_image(str(path), width=width, height=height, scale=scale)


def render_table_from_rows(
    path: Path | str,
    headers: list[str],
    rows: list[list[str]],
    **kwargs,
) -> None:
    render_table_image(path, headers, rows_to_columns(headers, rows), **kwargs)
