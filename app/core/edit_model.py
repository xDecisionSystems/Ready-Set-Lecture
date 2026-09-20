"""In-memory edit state for one video: markers, derived cut regions and split
points, and JSON (de)serialization to a project sidecar file.

Color-marker detection produces generic candidate markers (see
color_scanner.py). A candidate defaults to acting as a cut region on its
own — that's the whole point of holding up the colored paper — see
effective_cut_regions(). Classifying one via the timeline UI
(Cut-start/Cut-end/Split) overrides that default, e.g. to turn it into a
split point instead.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from pathlib import Path

from app.core.color_scanner import ColorSpec


CANDIDATE_BREAK_WINDOW_SECONDS = 30.0


class MarkerKind(str, Enum):
    CANDIDATE = "candidate"  # color-marker-detected, not yet classified
    CUT_START = "cut_start"
    CUT_END = "cut_end"
    SPLIT = "split"


@dataclass
class Marker:
    timestamp: float
    kind: MarkerKind
    id: int
    source: str = "manual"  # "manual" or "color"
    # The actual [range_start, range_end] the colored paper was visible for,
    # if known (timestamp is its midpoint, used for seeking/classification
    # as before — these are purely extra metadata for rendering the full span).
    range_start: float | None = None
    range_end: float | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["kind"] = self.kind.value
        return d

    @staticmethod
    def from_dict(d: dict) -> "Marker":
        return Marker(
            timestamp=d["timestamp"],
            kind=MarkerKind(d["kind"]),
            id=d["id"],
            source=d.get("source", "manual"),
            range_start=d.get("range_start"),
            range_end=d.get("range_end"),
        )


@dataclass
class CutRegion:
    start: float
    end: float


@dataclass
class EditProject:
    video_path: str
    markers: list[Marker] = field(default_factory=list)
    color_spec: ColorSpec | None = None
    _next_id: int = 1
    # Undo/redo history: snapshots of `markers` only (not color_spec, which
    # is closer to app configuration than an edit) — session-only, never
    # written to the sidecar. _next_id is deliberately NOT part of a
    # snapshot: it must keep climbing even across undo/redo, or a marker
    # created after an undo could collide with an id a later redo brings back.
    _undo_stack: list[list[Marker]] = field(default_factory=list, repr=False, compare=False)
    _redo_stack: list[list[Marker]] = field(default_factory=list, repr=False, compare=False)

    def checkpoint(self) -> None:
        """Snapshots the current markers so a later undo() can restore them.
        Call once immediately before each discrete user edit (a click, a
        drag's first move — not on every intermediate step of one drag).
        Starting a new edit discards any redo history, matching the usual
        editor convention that redo only replays undone steps, not
        alternate history after a fork."""
        self._undo_stack.append([replace(m) for m in self.markers])
        self._redo_stack.clear()

    def undo(self) -> bool:
        if not self._undo_stack:
            return False
        self._redo_stack.append([replace(m) for m in self.markers])
        self.markers = self._undo_stack.pop()
        return True

    def redo(self) -> bool:
        if not self._redo_stack:
            return False
        self._undo_stack.append([replace(m) for m in self.markers])
        self.markers = self._redo_stack.pop()
        return True

    @property
    def can_undo(self) -> bool:
        return bool(self._undo_stack)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo_stack)

    def add_marker(
        self,
        timestamp: float,
        kind: MarkerKind,
        source: str = "manual",
        range_start: float | None = None,
        range_end: float | None = None,
    ) -> Marker:
        marker = Marker(
            timestamp=timestamp, kind=kind, id=self._next_id, source=source,
            range_start=range_start, range_end=range_end,
        )
        self._next_id += 1
        self.markers.append(marker)
        self.markers.sort(key=lambda m: m.timestamp)
        return marker

    def clear_candidates(self) -> None:
        """Removes unclassified color candidates left over from a previous
        scan. Markers already classified (Cut-start/Cut-end/Split) are
        untouched, so re-scanning never disturbs edits you've already made."""
        self.markers = [m for m in self.markers if m.kind is not MarkerKind.CANDIDATE]

    def add_color_candidates(self, spans: list[tuple[float, float]]) -> list[Marker]:
        return [
            self.add_marker(
                (start + end) / 2, MarkerKind.CANDIDATE, source="color", range_start=start, range_end=end
            )
            for start, end in spans
        ]

    def remove_marker(self, marker_id: int) -> None:
        self.markers = [m for m in self.markers if m.id != marker_id]

    def set_marker_kind(self, marker_id: int, kind: MarkerKind) -> None:
        for m in self.markers:
            if m.id == marker_id:
                m.kind = kind
                return
        raise KeyError(f"No marker with id {marker_id}")

    def set_marker_range(self, marker_id: int, range_start: float, range_end: float) -> None:
        """Updates a candidate's start/end (e.g. after dragging its
        boundary in the UI), keeping timestamp as their midpoint."""
        for m in self.markers:
            if m.id == marker_id:
                m.range_start = range_start
                m.range_end = range_end
                m.timestamp = (range_start + range_end) / 2
                return
        raise KeyError(f"No marker with id {marker_id}")

    def cut_regions(self) -> tuple[list[CutRegion], list[Marker]]:
        """Pairs CUT_START/CUT_END markers in chronological order.

        Returns (regions, unmatched_markers). A CUT_START with no following
        CUT_END before the next CUT_START (or a stray CUT_END with nothing
        open) is reported as unmatched rather than silently dropped, so the
        UI can flag it instead of producing a wrong export.
        """
        cut_markers = sorted(
            (m for m in self.markers if m.kind in (MarkerKind.CUT_START, MarkerKind.CUT_END)),
            key=lambda m: m.timestamp,
        )
        regions: list[CutRegion] = []
        unmatched: list[Marker] = []
        open_start: Marker | None = None
        for m in cut_markers:
            if m.kind is MarkerKind.CUT_START:
                if open_start is not None:
                    unmatched.append(open_start)
                open_start = m
            else:  # CUT_END
                if open_start is None:
                    unmatched.append(m)
                else:
                    regions.append(CutRegion(open_start.timestamp, m.timestamp))
                    open_start = None
        if open_start is not None:
            unmatched.append(open_start)
        return regions, unmatched

    def split_points(self) -> list[float]:
        return sorted(m.timestamp for m in self.markers if m.kind is MarkerKind.SPLIT)

    def effective_cut_regions(self) -> list[CutRegion]:
        """Cut regions actually used for export: explicit Cut-start/Cut-end
        pairs, plus any still-unclassified color-marker candidates. A marker
        hold defaults to meaning "cut this out" (that's the whole point of
        holding one up) — you only need to classify it if you want something
        *other* than the default, e.g. reclassifying it as a Split point instead."""
        regions, _unmatched = self.cut_regions()
        regions = list(regions)
        for m in self.markers:
            if m.kind is MarkerKind.CANDIDATE and m.range_start is not None and m.range_end is not None:
                regions.append(CutRegion(m.range_start, m.range_end))
        return regions

    def kept_segments(self, duration: float) -> list[tuple[float, float]]:
        """The [start, end) ranges that survive after removing cut regions,
        clipped to [0, duration]. Does not yet apply split points."""
        regions = self.effective_cut_regions()
        cuts = sorted((max(0.0, r.start), min(duration, r.end)) for r in regions)
        segments: list[tuple[float, float]] = []
        cursor = 0.0
        for cut_start, cut_end in cuts:
            if cut_start > cursor:
                segments.append((cursor, cut_start))
            cursor = max(cursor, cut_end)
        if cursor < duration:
            segments.append((cursor, duration))
        return segments

    def save(self, path: str | Path) -> None:
        data = {
            "video_path": self.video_path,
            "markers": [m.to_dict() for m in self.markers],
            "color_spec": self.color_spec.to_dict() if self.color_spec else None,
        }
        Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")

    @staticmethod
    def load(path: str | Path) -> "EditProject":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        markers = [Marker.from_dict(d) for d in data["markers"]]
        next_id = max((m.id for m in markers), default=0) + 1
        color_spec_dict = data.get("color_spec")
        color_spec = ColorSpec.from_dict(color_spec_dict) if color_spec_dict else None
        project = EditProject(video_path=data["video_path"], markers=markers, color_spec=color_spec)
        project._next_id = next_id
        return project

    # Sidecars written before the program was renamed end in .videotrim.json. They are still opened, never rewritten in place.
    _SIDECAR_SUFFIX = ".readysetlecture.json"
    _LEGACY_SIDECAR_SUFFIX = ".videotrim.json"

    @staticmethod
    def sidecar_path_for(video_path: str | Path) -> Path:
        """Where the project for this video is saved."""
        return Path(video_path).with_suffix(Path(video_path).suffix + EditProject._SIDECAR_SUFFIX)

    @staticmethod
    def existing_sidecar_for(video_path: str | Path) -> Path | None:
        """The saved project to open for this video: the current sidecar, else one left by the program's earlier name."""
        current = EditProject.sidecar_path_for(video_path)
        if current.exists():
            return current
        legacy = Path(video_path).with_suffix(Path(video_path).suffix + EditProject._LEGACY_SIDECAR_SUFFIX)
        return legacy if legacy.exists() else None
