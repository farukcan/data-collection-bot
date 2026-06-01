"""Plotly table images for Telegram commands and the LLM agent."""
from pathlib import Path

import plotly.graph_objects as go

HEADER_FILL = "#4472C4"
FONT_SIZE = 11
CHAR_PX = 6.5
CELL_PAD_X = 10
MIN_COL_WIDTH = 36
MAX_COL_WIDTH = 220
MIN_FIG_WIDTH = 140
MAX_FIG_WIDTH = 880
HEADER_HEIGHT = 22
CELL_HEIGHT = 20
MARGIN = 6


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


def _column_display_lengths(headers: list[str], columns: list[list[str]]) -> list[int]:
    lengths: list[int] = []
    for i, header in enumerate(headers):
        max_len = len(header)
        for cell in columns[i]:
            max_len = max(max_len, len(str(cell)))
        lengths.append(max_len)
    return lengths


def _column_width_px(char_len: int) -> int:
    return max(MIN_COL_WIDTH, min(MAX_COL_WIDTH, int(char_len * CHAR_PX) + CELL_PAD_X))


def _figure_dimensions(
    headers: list[str],
    columns: list[list[str]],
) -> tuple[int, int, list[float]]:
    """Content-based width, height, and Plotly columnwidth ratios."""
    col_lens = _column_display_lengths(headers, columns)
    col_widths = [_column_width_px(n) for n in col_lens]
    width = max(MIN_FIG_WIDTH, min(MAX_FIG_WIDTH, sum(col_widths) + MARGIN * 2))
    row_count = len(columns[0]) if columns else 0
    height = MARGIN * 2 + HEADER_HEIGHT + CELL_HEIGHT * max(row_count, 1)
    total = sum(col_widths) or 1
    ratios = [w / total for w in col_widths]
    return width, height, ratios


def render_table_image(
    path: Path | str,
    headers: list[str],
    columns: list[list[str]],
    *,
    width: int | None = None,
    height: int | None = None,
    scale: int = 2,
) -> None:
    if len(columns) != len(headers):
        raise ValueError("headers and columns length mismatch")
    row_count = len(columns[0]) if columns else 0
    for col in columns:
        if len(col) != row_count:
            raise ValueError("column row count mismatch")

    auto_width, auto_height, columnwidth = _figure_dimensions(headers, columns)
    fig_width = width if width is not None else auto_width
    fig_height = height if height is not None else auto_height

    fig = go.Figure(
        data=[
            go.Table(
                columnwidth=columnwidth,
                header=dict(
                    values=headers,
                    fill_color=HEADER_FILL,
                    font=dict(color="white", size=FONT_SIZE),
                    align="left",
                    height=HEADER_HEIGHT,
                ),
                cells=dict(
                    values=columns,
                    align="left",
                    font=dict(size=FONT_SIZE),
                    height=CELL_HEIGHT,
                ),
            )
        ]
    )
    fig.update_layout(
        margin=dict(l=MARGIN, r=MARGIN, t=MARGIN, b=MARGIN),
        width=fig_width,
        height=fig_height,
    )
    fig.write_image(str(path), width=fig_width, height=fig_height, scale=scale)


def render_table_from_rows(
    path: Path | str,
    headers: list[str],
    rows: list[list[str]],
    **kwargs,
) -> None:
    render_table_image(path, headers, rows_to_columns(headers, rows), **kwargs)
