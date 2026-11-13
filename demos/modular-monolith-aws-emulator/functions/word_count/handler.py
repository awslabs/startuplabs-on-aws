"""Lambda handler deployed by `just bootstrap` (python3.12). Pure stdlib."""

import re
from collections import Counter


def handler(event, _context):
    words = re.findall(r"\w+", str(event.get("text", "")).lower())
    return {
        "words": len(words),
        "unique": len(set(words)),
        "top": Counter(words).most_common(5),
    }
