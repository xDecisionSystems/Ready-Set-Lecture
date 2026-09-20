"""Recording file names: the default, and validation of a name the user typed."""
from __future__ import annotations

from datetime import datetime

# Characters Windows does not allow in a file name (control characters are rejected separately).
ILLEGAL_NAME_CHARACTERS = '\\/:*?"<>|'
MAX_NAME_LENGTH = 150  # leaves room for the folder path and the extension within Windows' 260-character limit
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10))}


def default_recording_name(now: datetime) -> str:
    return f"recording_{now:%Y%m%d_%H%M%S}"


def clean_file_name(text: str) -> str | None:
    """The name to save under (no extension), or None if *text* can't be a Windows file name.

    Surrounding spaces and a typed '.mp4' are dropped, so 'Lecture 3.mp4' and 'Lecture 3' are the same recording.
    """
    name = text.strip()
    if name.lower().endswith(".mp4"):
        name = name[:-4].rstrip()
    if not name or len(name) > MAX_NAME_LENGTH or name.endswith("."):
        return None
    if any(character in ILLEGAL_NAME_CHARACTERS or ord(character) < 32 for character in name):
        return None
    if name.split(".")[0].rstrip().upper() in _RESERVED:  # 'CON' and 'CON.txt' are both reserved device names
        return None
    return name
