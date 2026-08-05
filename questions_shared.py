"""Question-rendering logic shared between Telegram (bot.py) and Chainlit (chainlit_app.py)."""
from typing import Any


def option_pairs(q: dict[str, Any]) -> list[tuple[str, str]]:
    """Returns (label, callback_value) pairs for scale/rating/choice questions."""
    cfg = q["config"]
    qtype = q["type"]
    if qtype == "scale":
        return [(str(v), str(v)) for v in range(cfg["min"], cfg["max"] + 1)]
    if qtype in ("rating", "choice"):
        return [(opt, str(i)) for i, opt in enumerate(cfg["options"])]
    raise ValueError(f"option_pairs: not applicable for type {qtype}")
