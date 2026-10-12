#!/usr/bin/env python3.12
"""Generate the reference architecture diagrams in this directory.

Writes one draw.io file per diagram and, with --drawio PATH, renders each to PNG with the
draw.io desktop app (headless under xvfb-run when no display is set). Standard library only.

    python3.12 docs/architecture/generate.py [--drawio /path/to/drawio]

Layout model. Every node sits in one cell of a 7-column by 4-row grid: an icon with its label
underneath, or a labelled box. Edges never pass over a node: an edge leaves a node from its
left or right side, runs vertically only in the gutters between columns and horizontally only
in the bands above and below each row of nodes, and enters the target from the side. Every
segment gets its own track in its gutter or band, so no two edges overlap. `Page.check`
re-verifies both rules on the finished geometry and generation fails if either is broken.
Edge labels are step numbers; the numbered step list is in the notes panel on the right.
The overview pages (section 5) give every edge an explicit route instead, and add their own
checks (check_overview).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent

# ------------------------------------------------------------------ grid and style

COLS, ROWS = 7, 4
CX = [120 + 200 * c for c in range(COLS)]  # column centre x
RY = [180 + 205 * r for r in range(ROWS)]  # row centre y (icon centre)
LANE_X0, LANE_X1 = 20, 1420
NOTES_X, NOTES_W = 1440, 470
ICON = 48  # icon edge length
LABEL_W, LABEL_H = 164, 46  # label box under an icon
BOX_W, BOX_H = 150, 56  # labelled box node
TOP_BAND = [-70, -62, -54, -46, -38]  # horizontal tracks above a row, relative to RY
BOT_BAND = [88, 96]  # horizontal tracks below a row's labels, relative to RY
GUTTER = [88, 109, 95, 102, 116]  # vertical tracks right of a column, relative to CX

LANES = [
    ("Front door", "#F5F5F5", "#7D8998"),
    ("Agent boundary: read and propose only", "#E6F6F4", "#01A88D"),
    ("Deterministic control", "#FDEBF3", "#E7157B"),
    ("Execution and state", "#FFF3E6", "#ED7100"),
]

_RES = (
    "sketch=0;outlineConnect=0;fontColor=#232F3E;fillColor={fill};strokeColor=#ffffff;"
    "dashed=0;html=1;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.{res};"
)
_SHAPE = (
    "sketch=0;outlineConnect=0;fontColor=#232F3E;fillColor={fill};strokeColor=none;dashed=0;"
    "html=1;aspect=fixed;shape=mxgraph.aws4.{res};"
)
# kind -> (template, resIcon or shape name, AWS category colour)
ICONS = {
    "user": (_SHAPE, "user", "#232F3E"),
    "cognito": (_RES, "cognito", "#DD344C"),
    "apigw": (_RES, "api_gateway", "#E7157B"),
    "bedrock": (_RES, "bedrock", "#01A88D"),
    "lambda": (_RES, "lambda", "#ED7100"),
    "sfn": (_RES, "step_functions", "#E7157B"),
    "ddb": (_RES, "dynamodb", "#C925D1"),
    "s3": (_RES, "s3", "#7AA116"),
    "secrets": (_RES, "secrets_manager", "#DD344C"),
    "kms": (_RES, "key_management_service", "#DD344C"),
    "cw": (_RES, "cloudwatch_2", "#E7157B"),
}
BOXES = {
    "agentcore": ("#01A88D", "#E6F6F4"),
    "ext": ("#232F3E", "#FFFFFF"),
}


@dataclass(frozen=True)
class Geo:
    """The grid and sizes a page lays out on. DETAIL is the reference architecture pages;
    the overview pages use larger type on a wider grid (see OVERVIEW and ALL_KITS)."""

    cx: list  # column centre x
    ry: list  # row centre y (icon centre)
    icon: int
    label_w: int
    label_h: int
    box_w: int
    box_h: int
    top_band: list
    bot_band: list
    gutter: list
    guess: int = 100  # rough gutter offset used to pick a band track
    font: int = 11  # node label size
    max_box: int = 60  # label length that keeps a box label to three lines
    max_icon: int = 74  # label length that keeps an icon label to three lines
    lab_font: int = 10  # edge label size
    lab_cw: float = 7  # edge label width per character
    lab_pad: int = 8
    lab_h: int = 15
    page_w: int = 1930


DETAIL = Geo(CX, RY, ICON, LABEL_W, LABEL_H, BOX_W, BOX_H, TOP_BAND, BOT_BAND, GUTTER)


@dataclass
class Node:
    key: str
    kind: str
    col: int
    row: int
    label: str
    cid: str = ""
    sides: dict = field(default_factory=dict)  # side -> ports used
    geo: Geo = DETAIL
    span: int = 1  # columns a box covers (a kit group on the all-kits page)
    w: int | None = None  # box size when it differs from the page's box size
    h: int | None = None
    lw: int | None = None  # icon label width when it differs from the page's label width
    xc: float | None = None  # centre x when the node sits off the page's columns

    @property
    def is_box(self) -> bool:
        return self.kind in BOXES or self.kind == "group"

    @property
    def rcol(self) -> int:
        """The column whose right-hand gutter the node's right side faces."""
        return self.col + self.span - 1

    @property
    def x(self) -> float:
        if self.xc is not None:
            return self.xc
        if self.span > 1:
            return (self.geo.cx[self.col] + self.geo.cx[self.rcol]) / 2
        return self.geo.cx[self.col]

    @property
    def y(self) -> float:
        return self.geo.ry[self.row]

    @property
    def bw(self) -> int:
        return self.w or self.geo.box_w

    @property
    def bh(self) -> int:
        return self.h or self.geo.box_h

    def half_w(self) -> float:
        return self.bw / 2 if self.is_box else self.geo.icon / 2

    def half_h(self) -> float:
        return self.bh / 2 if self.is_box else self.geo.icon / 2

    def rects(self) -> list[tuple[float, float, float, float]]:
        """Areas no edge may cross: the icon and its label, or the box."""
        g = self.geo
        if self.is_box:
            return [(self.x - self.bw / 2, self.y - self.bh / 2, self.bw, self.bh)]
        lw = self.lw or g.label_w
        return [
            (self.x - g.icon / 2, self.y - g.icon / 2, g.icon, g.icon),
            (self.x - lw / 2, self.y + g.icon / 2 + 2, lw, g.label_h),
        ]


class Page:
    PORTS = (0.5, 0.2, 0.8, 0.35, 0.65, 0.05, 0.95)

    def __init__(self, slug: str, title: str, height: int = 1000, geo: Geo = DETAIL) -> None:
        self.slug, self.title, self.height, self.geo = slug, title, height, geo
        self.nodes: dict[str, Node] = {}
        self.cells: list[str] = []
        self.edge_cells: list[str] = []
        self.label_cells: list[str] = []
        self.tracks: dict[tuple, list[tuple[float, float]]] = {}
        self.segments: list[tuple[str, tuple[float, float], tuple[float, float]]] = []
        self.label_rects: list[tuple[float, float, float, float]] = []
        self.ports: dict[tuple[int, int], set[float]] = {}
        self.pending: list = []
        self.n = 0

    def _id(self, p: str) -> str:
        self.n += 1
        return f"{p}{self.n}"

    # ---------------------------------------------------------------- primitives

    def text(self, x, y, w, h, html, style="") -> None:
        st = "text;html=1;whiteSpace=wrap;verticalAlign=top;align=left;fontSize=11;" + style
        self.cells.append(
            f'<mxCell id="{self._id("t")}" value="{escape(html)}" style="{st}" vertex="1" '
            f'parent="1"><mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/>'
            "</mxCell>"
        )

    def rect(self, x, y, w, h, label, fill, stroke, extra="") -> None:
        st = (
            f"rounded=1;arcSize=2;whiteSpace=wrap;html=1;fillColor={fill};strokeColor={stroke};"
            "verticalAlign=top;align=left;spacingLeft=8;spacingTop=2;fontSize=12;fontStyle=1;"
            f"fontColor={stroke};" + extra
        )
        self.cells.append(
            f'<mxCell id="{self._id("g")}" value="{escape(label)}" style="{st}" vertex="1" '
            f'parent="1"><mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/>'
            "</mxCell>"
        )

    def node(self, key, kind, col, row, label, w=None, h=None, lw=None) -> None:
        g = self.geo
        limit = g.max_box if kind in BOXES else g.max_icon * (lw or g.label_w) // g.label_w
        if len(label) > limit:  # three lines at most
            raise ValueError(f"{self.slug}: label too long for {key}: {label!r}")
        nd = Node(key, kind, col, row, label, cid=self._id("n"), geo=g, w=w, h=h, lw=lw)
        if nd.is_box:
            stroke, fill = BOXES[kind]
            st = (
                f"rounded=1;whiteSpace=wrap;html=1;fillColor={fill};strokeColor={stroke};"
                f"strokeWidth=2;fontSize={g.font};fontColor=#232F3E;spacing=4;"
            )
            x, y, w, h = nd.rects()[0]
            self.cells.append(
                f'<mxCell id="{nd.cid}" value="{escape(escape(label))}" style="{st}" vertex="1" '
                f'parent="1"><mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" '
                'as="geometry"/></mxCell>'
            )
        else:
            tmpl, res, colour = ICONS[kind]
            (ix, iy, iw, ih), (lx, ly, lw, lh) = nd.rects()
            self.cells.append(
                f'<mxCell id="{nd.cid}" value="" style="{tmpl.format(fill=colour, res=res)}" '
                f'vertex="1" parent="1"><mxGeometry x="{ix}" y="{iy}" width="{iw}" '
                f'height="{ih}" as="geometry"/></mxCell>'
            )
            size = "" if g.font == 11 else f"fontSize={g.font};"
            html = escape(label)
            if g is not DETAIL:  # overviews: never break a line inside a hyphenated name
                html = re.sub(r"(\S*\w-\w\S*)", r"<span style='white-space:nowrap'>\1</span>", html)
            self.text(lx, ly, lw, lh, html, "align=center;verticalAlign=top;spacing=0;" + size)
        self.nodes[key] = nd

    # ---------------------------------------------------------------- routing

    def _track(self, key: tuple, choices: list[float], span: tuple[float, float]) -> float:
        """The first track in `choices` whose used spans do not overlap `span`."""
        lo, hi = sorted(span)
        used = self.tracks.setdefault(key, [])
        for i, t in enumerate(choices):
            if all(hi + 6 < a or lo - 6 > b for (tr, a, b) in used if tr == i):
                used.append((i, lo, hi))
                return t
        raise ValueError(f"{self.slug}: no free track in {key} for span {span}")

    def _port(self, nd: Node, side: str) -> float:
        """A port fraction on one side of a node: 0.5 first, then outwards. Fractions are
        shared by every node side facing the same gutter in the same row, so the short stubs
        between a node side and its gutter never lie on top of each other."""
        g = nd.rcol if side == "R" else nd.col - 1
        used = self.ports.setdefault((nd.row, g), set())
        for f in self.PORTS:
            if f not in used:
                used.add(f)
                return f
        raise ValueError(f"{self.slug}: no free port beside {nd.key} ({side})")

    def edge(self, src, dst, num, *, dashed=False, color="#232F3E", band=None) -> None:
        s, t = self.nodes[src], self.nodes[dst]
        geo = self.geo
        if t.col > s.col:
            s_side, t_side = "R", "L"
        elif t.col < s.col:
            s_side, t_side = "L", "R"
        else:
            s_side = t_side = "R"
        straight = s.row == t.row and (t.col - s.rcol == 1 or s.col - t.rcol == 1)
        if straight:
            sp = tp = self._port(s, s_side)
        else:
            sp, tp = self._port(s, s_side), self._port(t, t_side)

        def side_pt(nd: Node, side: str, frac: float) -> tuple[float, float]:
            x = nd.x + (nd.half_w() if side == "R" else -nd.half_w())
            return x, nd.y - nd.half_h() + 2 * nd.half_h() * frac

        p0, p1 = side_pt(s, s_side, sp), side_pt(t, t_side, tp)
        pts: list[tuple[float, float]] = []
        if not straight:
            g1 = s.rcol if s_side == "R" else s.col - 1
            g2 = t.rcol if t_side == "R" else t.col - 1
            cx, gut = geo.cx, geo.gutter
            if g1 == g2:
                gx = cx[g1] + self._track(("g", g1), gut, (p0[1], p1[1]))
                pts = [(gx, p0[1]), (gx, p1[1])]
            else:
                row = t.row if band is None else band[0]
                where = ("top" if t.row >= s.row else "bot") if band is None else band[1]
                choices = geo.top_band if where == "top" else geo.bot_band
                gx1_guess, gx2_guess = cx[g1] + geo.guess, cx[g2] + geo.guess
                by = geo.ry[row] + self._track((where, row), choices, (gx1_guess, gx2_guess))
                gx1 = cx[g1] + self._track(("g", g1), gut, (p0[1], by))
                gx2 = cx[g2] + self._track(("g", g2), gut, (by, p1[1]))
                pts = [(gx1, p0[1]), (gx1, by), (gx2, by), (gx2, p1[1])]
        path = [p0, *pts, p1]
        for a, b in pairwise(path):
            self.segments.append((f"{src}->{dst}", a, b))
        ex, ey = (1 if s_side == "R" else 0), sp
        nx, ny = (1 if t_side == "R" else 0), tp
        st = (
            "edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;endArrow=block;endFill=1;"
            f"strokeWidth=1.5;strokeColor={color};exitX={ex};exitY={ey};entryX={nx};"
            f"entryY={ny};exitDx=0;exitDy=0;entryDx=0;entryDy=0;" + ("dashed=1;" if dashed else "")
        )
        wp = "".join(f'<mxPoint x="{x}" y="{y}"/>' for x, y in pts)
        arr = f'<Array as="points">{wp}</Array>' if pts else ""
        self.edge_cells.append(
            f'<mxCell id="{self._id("e")}" value="" style="{st}" edge="1" parent="1" '
            f'source="{s.cid}" target="{t.cid}"><mxGeometry relative="1" as="geometry">{arr}'
            "</mxGeometry></mxCell>"
        )
        self.pending.append((f"{src}->{dst}", path, str(num), color))

    def wire(
        self, src, dst, text, s_end, t_end, via=(), *, color="#232F3E", dashed=False, prefer=None
    ) -> None:
        """An overview edge on an explicit route. `s_end` and `t_end` are (side, fraction) with
        side L, R, T or B; `via` lists the bend points. Every segment must be horizontal or
        vertical. `prefer` lists the segment indexes to try first for the label."""
        s, t = self.nodes[src], self.nodes[dst]

        def end(nd: Node, side: str, f: float) -> tuple[float, float]:
            x0, y0 = nd.x - nd.half_w(), nd.y - nd.half_h()
            w, h = 2 * nd.half_w(), 2 * nd.half_h()
            return {
                "L": (x0, y0 + h * f),
                "R": (x0 + w, y0 + h * f),
                "T": (x0 + w * f, y0),
                "B": (x0 + w * f, y0 + h),
            }[side]

        def frac(side: str, f: float) -> tuple[float, float]:
            return {"L": (0, f), "R": (1, f), "T": (f, 0), "B": (f, 1)}[side]

        name = f"{src}->{dst}"
        path = [end(s, *s_end), *via, end(t, *t_end)]
        for a, b in pairwise(path):
            if a[0] != b[0] and a[1] != b[1]:
                raise ValueError(f"{self.slug}: edge {name} has a diagonal segment {a} {b}")
            self.segments.append((name, a, b))
        (ex, ey), (nx, ny) = frac(*s_end), frac(*t_end)
        st = (
            "edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;endArrow=block;endFill=1;"
            f"strokeWidth=2;strokeColor={color};exitX={ex};exitY={ey};entryX={nx};"
            f"entryY={ny};exitDx=0;exitDy=0;entryDx=0;entryDy=0;" + ("dashed=1;" if dashed else "")
        )
        wp = "".join(f'<mxPoint x="{x}" y="{y}"/>' for x, y in via)
        arr = f'<Array as="points">{wp}</Array>' if via else ""
        self.edge_cells.append(
            f'<mxCell id="{self._id("e")}" value="" style="{st}" edge="1" parent="1" '
            f'source="{s.cid}" target="{t.cid}"><mxGeometry relative="1" as="geometry">{arr}'
            "</mxGeometry></mxCell>"
        )
        self.pending.append((name, path, text, color, prefer))

    def place_labels(self) -> None:
        for name, path, text, color, *prefer in self.pending:
            self._label(name, path, text, color, *prefer)

    def _label(self, name, path, text, color, prefer=None) -> None:
        """A step number on its edge, placed where it overlaps no node, no other label and no
        other edge. An overview edge (`prefer` given, possibly empty) must touch no other edge
        and tries its preferred segments, or segment positions, first."""
        if not text:  # an overview edge drawn without a label
            return
        g = self.geo
        w, h = g.lab_pad + g.lab_cw * len(text), g.lab_h
        segs = sorted(
            pairwise(path), key=lambda ab: -abs(ab[0][0] - ab[1][0]) - abs(ab[0][1] - ab[1][1])
        )
        fracs = (0.5, 0.35, 0.65, 0.2, 0.8, 0.1, 0.9)
        tries = [(a, b, fracs) for a, b in segs]
        if prefer is not None:  # an entry is a segment index, or (index, fraction) to try first
            edge_segs = list(pairwise(path))
            first = [
                (*edge_segs[i[0]], (i[1],)) if isinstance(i, tuple) else (*edge_segs[i], fracs)
                for i in prefer
            ]
            tries = first + tries
        blocked = [_grow(r, 4) for nd in self.nodes.values() for r in nd.rects()] + self.label_rects
        others = [(a, b) for n2, a, b in self.segments if n2 != name]

        def parallel(a, b, sa, sb) -> bool:
            return (a[0] == b[0]) == (sa[0] == sb[0])

        # First choice: touch no other edge. Second: cross other edges only at right angles.
        # Last: avoid only nodes and labels.
        for strict in (True,) if prefer is not None else (True, False, None):
            for a, b, fs in tries:
                for f in fs:
                    cx, cy = a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f
                    r = (cx - w / 2, cy - h / 2, w, h)
                    if any(_overlap(r, o, 2) for o in blocked):
                        continue
                    if any(
                        _seg_hits(sa, sb, r, shrink=-1)
                        for sa, sb in others
                        if strict or (strict is False and parallel(a, b, sa, sb))
                    ):
                        continue
                    self.label_rects.append(r)
                    st = (
                        f"text;html=1;align=center;verticalAlign=middle;fontSize={g.lab_font};"
                        "fontStyle=1;"
                        f"fontColor=#FFFFFF;fillColor={color};strokeColor=none;rounded=1;"
                    )
                    self.label_cells.append(
                        f'<mxCell id="{self._id("l")}" value="{text}" style="{st}" vertex="1" '
                        f'parent="1"><mxGeometry x="{r[0]}" y="{r[1]}" width="{w}" height="{h}" '
                        'as="geometry"/></mxCell>'
                    )
                    return
        raise ValueError(f"{self.slug}: no free place for label {text}")

    # ---------------------------------------------------------------- checks

    def check(self) -> None:
        """No edge segment crosses a node other than at its own end points; no two collinear
        segments of different edges overlap."""
        for name, a, b in self.segments:
            src, dst = name.split("->")
            for nd in self.nodes.values():
                for r in nd.rects():
                    if _seg_hits(a, b, r, shrink=1 if nd.key in (src, dst) else -2):
                        raise ValueError(f"{self.slug}: edge {name} crosses node {nd.key}")
        for i, (n1, a1, b1) in enumerate(self.segments):
            for n2, a2, b2 in self.segments[i + 1 :]:
                if n1 != n2 and _collinear_overlap(a1, b1, a2, b2):
                    raise ValueError(f"{self.slug}: edges {n1} and {n2} overlap")

    # ---------------------------------------------------------------- output

    def frame(self, notes_html: str, lanes: bool = True) -> None:
        body, self.cells = self.cells, []
        self.text(
            20, 14, 1890, 34, f"<b>{escape(self.title)}</b>", "fontSize=18;verticalAlign=middle;"
        )
        if lanes:
            for r, (_label, fill, stroke) in enumerate(LANES):
                self.rect(LANE_X0, RY[r] - 92, LANE_X1 - LANE_X0, 186, "", fill, stroke)
        self.rect(NOTES_X, 88, NOTES_W, self.height - 108, "", "#FFFFFF", "#545B64", "fontStyle=0;")
        self.text(NOTES_X + 12, 96, NOTES_W - 24, self.height - 124, notes_html, "fontSize=11;")
        self.cells.extend(body)
        if lanes:  # titles on the lane colour, emitted after the edges so no line runs through one
            start = len(self.cells)
            for r, (label, fill, stroke) in enumerate(LANES):
                self.text(
                    LANE_X0 + 8,
                    RY[r] - 90,
                    320,
                    18,
                    label,
                    f"fontSize=12;fontStyle=1;fontColor={stroke};whiteSpace=nowrap;"
                    f"labelBackgroundColor={fill};",
                )
            self.label_cells[:0] = self.cells[start:]  # xml(): cells, then edges, then labels
            del self.cells[start:]

    def xml(self) -> str:
        body = "".join(self.cells + self.edge_cells + self.label_cells)
        return (
            '<mxfile host="generate.py" type="device">'
            f'<diagram id="{self.slug}" name="{escape(self.slug)}"><mxGraphModel dx="1600" '
            'dy="900" grid="0" gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" '
            f'fold="1" page="1" pageScale="1" pageWidth="{self.geo.page_w}" '
            f'pageHeight="{self.height}" '
            'math="0" shadow="0"><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
            f"{body}</root></mxGraphModel></diagram></mxfile>"
        )


def _grow(r, d):
    return (r[0] - d, r[1] - d, r[2] + 2 * d, r[3] + 2 * d)


def _overlap(r1, r2, pad=0) -> bool:
    return not (
        r1[0] + r1[2] + pad <= r2[0]
        or r2[0] + r2[2] + pad <= r1[0]
        or r1[1] + r1[3] + pad <= r2[1]
        or r2[1] + r2[3] + pad <= r1[1]
    )


def _seg_hits(a, b, r, shrink=0) -> bool:
    x0, y0, w, h = r[0] + shrink, r[1] + shrink, r[2] - 2 * shrink, r[3] - 2 * shrink
    lx, hx = sorted((a[0], b[0]))
    ly, hy = sorted((a[1], b[1]))
    return not (hx < x0 or lx > x0 + w or hy < y0 or ly > y0 + h)


def _collinear_overlap(a1, b1, a2, b2) -> bool:
    if a1[0] == b1[0] == a2[0] == b2[0]:
        lo1, hi1 = sorted((a1[1], b1[1]))
        lo2, hi2 = sorted((a2[1], b2[1]))
        return min(hi1, hi2) - max(lo1, lo2) > 1
    if a1[1] == b1[1] == a2[1] == b2[1]:
        lo1, hi1 = sorted((a1[0], b1[0]))
        lo2, hi2 = sorted((a2[0], b2[0]))
        return min(hi1, hi2) - max(lo1, lo2) > 1
    return False


# ------------------------------------------------------------------ notes panel


def ul(items) -> str:
    li = "".join(f"<li>{i}</li>" for i in items)
    return f"<ul style='margin:1px 0 5px 0;padding-left:16px'>{li}</ul>"


def ol(items) -> str:
    li = "".join(f"<li>{i}</li>" for i in items)
    return f"<ol style='margin:1px 0 5px 0;padding-left:20px'>{li}</ol>"


def section(title: str, body: str) -> str:
    return f"<b>{title}</b>{body}"


def code(s: str) -> str:
    return f"<font face='monospace'>{escape(s)}</font>"


# ------------------------------------------------------------------ shared spine layout

FRONT_STEPS = [
    "Sign in to the Cognito user pool (the pre-token Lambda adds the tenant claim)",
    "Chat: the CLI calls AgentCore Runtime directly with the access token (Runtime JWT auth)",
    "The agent calls the Bedrock model",
    "The agent calls tools through AgentCore Gateway with the same token",
    "The Gateway REQUEST interceptor verifies the caller and writes it into the tool call",
    "The Gateway invokes the kit's tool Lambda (one Gateway target per kit)",
]


def front(
    p: Page,
    *,
    tool_label: str,
    reader_label: str,
    approval_label: str,
    stepfn_label: str,
    machines_label: str,
    adapter_label: str,
    ddb_label: str,
    awx_label: str,
    kms: bool = True,
    cw: bool = True,
    s3_label: str | None = None,
) -> None:
    p.node("user", "user", 0, 0, "Requester or approver using the awxpoc CLI")
    p.node("cognito", "cognito", 1, 0, "Amazon Cognito user pool (requester and approver groups)")
    p.node(
        "api", "apigw", 3, 0, "Approval API: Amazon API Gateway HTTP API, Cognito JWT authorizer"
    )
    p.node("model", "bedrock", 0, 1, "Amazon Bedrock model (Claude Sonnet, with a fallback)")
    p.node("runtime", "agentcore", 1, 1, "Amazon Bedrock AgentCore Runtime: Strands agent")
    p.node("gateway", "agentcore", 2, 1, "AgentCore Gateway (MCP, Cognito JWT inbound)")
    p.node("icpt", "lambda", 3, 1, "Gateway REQUEST interceptor Lambda: verifies the caller")
    p.node("tools", "lambda", 4, 1, tool_label)
    p.node("step", "lambda", 1, 2, stepfn_label)
    p.node("sfn", "sfn", 2, 2, machines_label)
    p.node("appr", "lambda", 3, 2, approval_label)
    p.node("reader", "lambda", 5, 2, reader_label)
    if cw:
        p.node(
            "cw", "cw", 0, 3, "Amazon CloudWatch alarms to an Amazon SNS topic; AWS Budgets alarm"
        )
    if kms:
        p.node("kms", "kms", 1, 3, "AWS KMS customer managed key (tables, bucket, secret)")
    if s3_label:
        p.node("s3", "s3", 2, 3, s3_label)
    p.node("ddb", "ddb", 3, 3, ddb_label)
    p.node("adapter", "lambda", 4, 3, adapter_label)
    p.node("secret", "secrets", 5, 3, "AWS Secrets Manager: Airwallex client credentials")
    p.node("awx", "ext", 6, 3, awx_label)


def front_edges(p: Page) -> int:
    p.edge("user", "cognito", 1)
    p.edge("user", "runtime", 2)
    p.edge("runtime", "model", 3)
    p.edge("runtime", "gateway", 4)
    p.edge("gateway", "icpt", 5)
    p.edge("gateway", "tools", 6)
    return 7


# ------------------------------------------------------------------ 1. stacks


def stacks_page() -> Page:
    p = Page("stacks", "CDK stacks: AwxSpine, one AwxKitK<N> per kit, AwxAgentStack", height=760)

    def box(x, y, w, h, title, lines, stroke, fill):
        html = f"<b>{title}</b>" + ul(lines)
        st = (
            f"rounded=1;arcSize=3;whiteSpace=wrap;html=1;fillColor={fill};strokeColor={stroke};"
            "strokeWidth=2;verticalAlign=top;align=left;spacingLeft=8;spacingTop=4;spacingRight=6;"
            "fontSize=11;fontColor=#232F3E;"
        )
        cid = p._id("b")
        p.cells.append(
            f'<mxCell id="{cid}" value="{escape(html)}" style="{st}" vertex="1" parent="1">'
            f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>'
        )
        return cid

    spine = box(
        30,
        110,
        330,
        600,
        "AwxSpine (shared, deployed first)",
        [
            "Amazon Cognito user pool: requester and approver groups, app client, "
            "pre-token Lambda adding the tenant claim",
            "Approval API: Amazon API Gateway HTTP API with a Cognito JWT authorizer; routes "
            "GET /proposals, GET /proposals/{proposal_id}, "
            "POST /proposals/{proposal_id}/decision",
            "Approval Lambda: binds a decision to the proposal hash, then sends the task token",
            "Mutation adapter Lambda: the only path to an Airwallex write endpoint",
            "Reader Lambdas, one per kit: GET only, per-kit allowlist",
            "AWS Secrets Manager secret: Airwallex client credentials (adapter and readers only)",
            "Amazon DynamoDB tables: operations, proposals, decisions, tenants, ledger, audit",
            "Amazon S3 artifacts bucket (evidence under evidence/)",
            "AWS KMS customer managed key",
            "Amazon CloudWatch alarms (adapter and reader errors and throttles) to an Amazon "
            "SNS topic; AWS Budgets monthly cost budget",
        ],
        "#ED7100",
        "#FFF3E6",
    )
    kits = [
        ("AwxKitK1", "treasury", "Machines: lock_rate, execute_plan, release_obligation"),
        (
            "AwxKitK2",
            "purchase",
            "Machines: issue_card, update_card_controls, freeze_card, "
            "unfreeze_card, close_card, run_limit_test",
        ),
        (
            "AwxKitK3",
            "incident",
            "Machines: replace_failed_transfer, close_incident, simulate_transfer_failure",
        ),
        (
            "AwxKitK4",
            "dispute",
            "Machines: accept_dispute, challenge_dispute, refer_dispute, "
            "close_dispute, clear_dispute_response; evidence upload Lambda",
        ),
        (
            "AwxKitK5",
            "platform spend",
            "Machines: register_shortfall, bridge_shortfall, "
            "reconcile_bridge, collect_fee, collect_repayment, issue_card, simulate_card_spend",
        ),
        (
            "AwxKitK6",
            "payroll",
            "Root machines: run_payroll, lock_rates, release_run_claim; "
            "child machines: run_payroll-child, lock_rates-child",
        ),
        (
            "AwxKitK7",
            "lending",
            "Machines: collect_weekly_repayment, disburse_advance, reconcile_portfolio",
        ),
        (
            "AwxKitK8",
            "settlement",
            "Machines: settle_seller_cycle, recalculate_seller_reserve, "
            "recover_shortfall, reconcile_seller_money, resolve_in_flight_money",
        ),
    ]
    p.rect(
        420,
        110,
        640,
        600,
        "Kit stacks: one generic KitStack per kit, built from the kit's manifest",
        "#FDEBF3",
        "#E7157B",
    )
    kit_ids = []
    for i, (name, slug, machines) in enumerate(kits):
        col, row = i % 2, i // 2
        x, y = 432 + col * 316, 140 + row * 141
        kit_ids.append(
            box(
                x,
                y,
                300,
                130,
                f"{name} (kit {i + 1}, {slug})",
                [
                    machines,
                    "one kit-step Lambda per action (no Airwallex secret)",
                    "CloudWatch alarms on its state machines (failed, timed out)",
                ],
                "#E7157B",
                "#FFFFFF",
            )
        )
    agent = box(
        1120,
        110,
        300,
        600,
        "AwxAgentStack (deployed last)",
        [
            "Amazon Bedrock AgentCore Runtime: the Strands agent, Cognito JWT inbound auth, "
            "Bedrock model invocation only",
            "AgentCore Gateway (MCP protocol, Cognito JWT inbound auth)",
            "Gateway REQUEST interceptor Lambda: writes the verified caller into each tool call",
            "Gateway targets, one tool Lambda per kit (read and propose only): invokes "
            "its kit's reader, starts its kit's state machines, reads only its kit's ledger "
            "rows and the shared tenants and proposals tables",
            "Outputs: Runtime ARN, Gateway URL, approval API URL, user pool client id, Region "
            "(read by the awxpoc CLI)",
        ],
        "#01A88D",
        "#E6F6F4",
    )

    def arrow(src, dst, label, pts=(), exit_=(0, 0.5), entry=(1, 0.5)):
        st = (
            "edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;endArrow=block;endFill=1;strokeWidth=2;"
            f"strokeColor=#232F3E;fontSize=11;labelBackgroundColor=#FFFFFF;exitX={exit_[0]};"
            f"exitY={exit_[1]};entryX={entry[0]};entryY={entry[1]};exitDx=0;exitDy=0;entryDx=0;"
            "entryDy=0;"
        )
        wp = "".join(f'<mxPoint x="{x}" y="{y}"/>' for x, y in pts)
        arr = f'<Array as="points">{wp}</Array>' if pts else ""
        p.edge_cells.append(
            f'<mxCell id="{p._id("e")}" value="{escape(label)}" style="{st}" edge="1" parent="1" '
            f'source="{src}" target="{dst}"><mxGeometry relative="1" as="geometry">{arr}'
            "</mxGeometry></mxCell>"
        )

    # Dependencies: an arrow points from a stack to a stack whose exports it imports.
    p.cells.append(
        '<mxCell id="kitgroup" value="" style="rounded=0;fillColor=none;strokeColor=none;" '
        'vertex="1" parent="1"><mxGeometry x="420" y="110" width="640" height="600" '
        'as="geometry"/></mxCell>'
    )
    arrow("kitgroup", spine, "imports<br>spine<br>exports", entry=(1, 0.5), exit_=(0, 0.5))
    arrow(agent, "kitgroup", "imports<br>machine<br>ARNs", entry=(1, 0.5), exit_=(0, 0.5))
    arrow(
        agent,
        spine,
        "imports spine exports",
        pts=[(1270, 85), (195, 85)],
        exit_=(0.5, 0),
        entry=(0.5, 0),
    )
    notes = (
        section(
            "Deploy order",
            ol(["AwxSpine", "AwxKitK1 to AwxKitK8 (independent of each other)", "AwxAgentStack"]),
        )
        + "An arrow points from a stack to the stack whose CloudFormation exports it imports. "
        "No kit stack imports from the agent stack, so the agent stack deploys last.<br><br>"
        + section(
            "Stack sources",
            ul(
                [
                    f"{code('infra/app.py')}: builds every stack",
                    f"{code('infra/stacks/')}: AwxSpine, the generic KitStack, AwxAgentStack",
                    f"{code('infra/constructs/kit_workflow.py')}: one state machine per kit action",
                    f"{code('infra/kit_manifest.py')}: loads each kit's manifest in a subprocess, "
                    "so kit "
                    "code never runs in the CDK process",
                ]
            ),
        )
        + section(
            "A kit writes no CDK",
            ul(
                [
                    "Its manifest comes from its Python package: workflow specs (SPECS), tools "
                    "(TOOLS), "
                    "reads (READS), extra functions (FUNCTIONS, kit 4 only) and operator commands",
                    "Each action becomes a Step Functions Standard machine named "
                    f"{code('awx-<kit>-<action>')}; a fan-out action also gets "
                    f"{code('awx-<kit>-<action>-child')} (kit 6 only)",
                    "Each tool becomes an entry in the kit's Gateway target in AwxAgentStack",
                ]
            ),
        )
        + section(
            "Call permissions (IAM)",
            ul(
                [
                    "Only the kit state machine roles may invoke the adapter Lambda",
                    "Only the adapter and the readers may read the Airwallex secret",
                    "A kit's tool Lambda may invoke only its own reader and start only its own "
                    "machines",
                    "The Runtime role can invoke Bedrock models; it has no Lambda, Step "
                    "Functions, DynamoDB "
                    "or secret access",
                ]
            ),
        )
    )
    p.frame(notes, lanes=False)
    return p


# ------------------------------------------------------------------ 2. spine


def spine_page() -> Page:
    p = Page(
        "spine",
        "Shared spine: the agent reads and proposes, only a Step Functions workflow moves money",
    )
    front(
        p,
        tool_label="Tool Lambda per kit: read_* and propose_* (Gateway target)",
        reader_label="Reader Lambda per kit: GET only, per-kit allowlist",
        approval_label="Approval Lambda: checks the decision, sends the task token",
        stepfn_label="Kit-step Lambda per action: validate, policy, build, postcondition",
        machines_label="AWS Step Functions Standard: awx-<kit>-<action>",
        adapter_label="Mutation adapter Lambda: the only Airwallex writer",
        ddb_label="DynamoDB tables: operations, proposals, decisions, tenants, ledger, audit",
        awx_label="Airwallex sandbox API",
        s3_label="Amazon S3 artifacts bucket: evidence/",
    )
    p.node("op", "user", 6, 0, "Operator using awx-deploy")
    front_edges(p)
    p.edge("tools", "reader", 7)
    p.edge("reader", "awx", 8)
    p.edge("tools", "sfn", 9)
    p.edge("sfn", "step", 10)
    p.edge("step", "reader", 11, dashed=True, band=(2, "bot"))
    p.edge("step", "ddb", 12)
    p.edge("user", "api", 13, color="#DD344C")
    p.edge("api", "appr", 14, color="#DD344C")
    p.edge("appr", "ddb", 15, color="#DD344C")
    p.edge("appr", "sfn", 16, color="#DD344C")
    p.edge("sfn", "adapter", 17, color="#ED7100")
    p.edge("adapter", "ddb", 18, color="#ED7100")
    p.edge("adapter", "secret", 19, color="#ED7100", dashed=True)
    p.edge("adapter", "awx", 20, color="#ED7100", band=(3, "bot"))
    p.edge("adapter", "s3", 21, color="#ED7100", dashed=True, band=(3, "bot"))
    p.edge("op", "sfn", 22, dashed=True)
    steps = [
        *FRONT_STEPS,
        "A read tool invokes the kit's own reader Lambda",
        "The reader sends the GET to Airwallex with the secret it holds. Tools also read the "
        "kit's ledger rows and the shared proposals and tenants tables, and write nothing",
        "A propose tool starts the kit's state machine with the canonical intent",
        "The machine runs each phase in the kit-step Lambda: validate, policy (code computes "
        "every amount and the risk tier), builds, finality checks, postcondition, audit",
        "Policy and finality reads go through the same reader",
        "The kit-step Lambda writes the proposal, ledger and audit rows",
        "If a person must approve, the machine waits (waitForTaskToken). The approver lists and "
        f"decides with {code('awxpoc proposals')} and {code('awxpoc approve')}",
        "The approval API invokes the approval Lambda",
        "It refuses the proposer and a stale or wrong proposal hash, then stores the decision",
        "It sends the task token back to the waiting execution",
        "Each operation goes to the adapter Lambda",
        "The adapter writes the operation record before it sends (reconcile before any resend)",
        "It reads the Airwallex credentials from Secrets Manager",
        "It sends the write. Customer-scope operations in kits 5 and 6 carry x-on-behalf-of; "
        "platform charges and connected-account transfers name the connected account in the body",
        "For a file upload it reads the evidence bytes from S3 (kit 4)",
        f"{code('awx-deploy trigger')} starts a schedule-driven action. {code('awx-deploy')} "
        "also writes tenants rows and runs operator commands",
    ]
    notes = (
        section("Flow", ol(steps))
        + "<b>Model owns:</b> reading, extracting, comparing, explaining, choosing which "
        "proposal to make.<br><b>Code owns:</b> every amount, fee, limit, tenant and connected "
        "account, approval tier, request id, finality decision and Airwallex call.<br><br>"
        + section(
            "Schedules:",
            " The deployment has no Amazon EventBridge schedules. An "
            f"operator starts scheduled actions with {code('awx-deploy trigger')}. In "
            "production, Amazon EventBridge Scheduler would start the same machines (not "
            "deployed).<br><br>",
        )
        + section(
            "Acceptance checks",
            ul(
                [
                    "The Runtime and tool roles cannot invoke the adapter or read the Airwallex "
                    "secret",
                    "An approver cannot approve their own proposal, and a decision must carry the "
                    "reviewed proposal hash",
                    "A send that may have landed is read back before any resend",
                ]
            ),
        )
        + section(
            "Code locations",
            ul(
                [
                    f"{code('packages/awx_agent')}: agent, tool and interceptor handlers, awxpoc "
                    "CLI",
                    f"{code('packages/awx_spine_aws')}: adapter, reader, approval and kit-step "
                    "handlers, workflow engine, awx-deploy",
                    f"{code('packages/awx_spine')}: Airwallex client, mutation executor, endpoint "
                    "table",
                    f"{code('infra/')}: AwxSpine, KitStack, AwxAgentStack",
                ]
            ),
        )
    )
    p.frame(notes)
    return p


# ------------------------------------------------------------------ 3. kit workflow


def kit_workflow_page() -> Page:
    p = Page(
        "kit-workflow", "Generic kit state machine (infra/constructs/kit_workflow.py)", height=900
    )
    S = {}

    def st(key, x, y, label, kind="task", w=150, h=44):
        base = "whiteSpace=wrap;html=1;"
        styles = {
            "task": base + "rounded=1;fillColor=#FDEBF3;strokeColor=#E7157B;",
            "choice": base + "rhombus;fillColor=#FFFFFF;strokeColor=#E7157B;",
            "choice_box": base + "rounded=0;fillColor=#FFFFFF;strokeColor=#E7157B;",
            "adapter": base + "rounded=1;fillColor=#FFF3E6;strokeColor=#ED7100;strokeWidth=2;",
            "wait": base + "rounded=1;fillColor=#FFFFFF;strokeColor=#7D8998;dashed=1;",
            "human": base + "rounded=1;fillColor=#FDECEE;strokeColor=#DD344C;strokeWidth=2;",
            "end": base + "ellipse;fillColor=#232F3E;strokeColor=#232F3E;fontColor=#FFFFFF;",
            "map": base + "rounded=1;fillColor=#E6F6F4;strokeColor=#01A88D;strokeWidth=2;dashed=1;",
        }
        cid = p._id("s")
        p.cells.append(
            f'<mxCell id="{cid}" value="{escape(escape(label))}" '
            f'style="{styles[kind]}fontSize=11;" '
            f'vertex="1" parent="1"><mxGeometry x="{x - w / 2}" y="{y - h / 2}" width="{w}" '
            f'height="{h}" as="geometry"/></mxCell>'
        )
        S[key] = (cid, x, y, w, h)

    def ln(a, b, label="", pts=(), ex=None, en=None, dashed=False, color="#232F3E"):
        ca, cb = S[a][0], S[b][0]
        style = (
            "edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;endArrow=block;endFill=1;"
            f"strokeWidth=1.5;strokeColor={color};fontSize=10;labelBackgroundColor=#FFFFFF;"
            + (f"exitX={ex[0]};exitY={ex[1]};exitDx=0;exitDy=0;" if ex else "")
            + (f"entryX={en[0]};entryY={en[1]};entryDx=0;entryDy=0;" if en else "")
            + ("dashed=1;" if dashed else "")
        )
        wp = "".join(f'<mxPoint x="{x}" y="{y}"/>' for x, y in pts)
        arr = f'<Array as="points">{wp}</Array>' if pts else ""
        p.edge_cells.append(
            f'<mxCell id="{p._id("e")}" value="{escape(label)}" style="{style}" edge="1" '
            f'parent="1" source="{ca}" target="{cb}"><mxGeometry relative="1" as="geometry">'
            f"{arr}</mxGeometry></mxCell>"
        )

    # Columns: approval (150), recheck and fan-out (420), per-step loop (700), finality and
    # results (950), close (1200). Every edge below is routed by hand around the states.
    A, B, C, D, E = 150, 420, 700, 950, 1200
    st("start", A, 120, "Start: validate, policy, store proposal (risk tier)")
    st("tier", A, 230, "RiskTier?", "choice", 120, 70)
    st("req", A, 350, "RequestApproval: waitForTaskToken, timeout = approval TTL", "human", 170, 56)
    st("bind", A, 470, "Binding: decision matches proposal hash, not the proposer")
    st("approved", A, 590, "Approved?", "choice", 120, 70)
    st("recheck", B, 120, "Recheck: policy again on fresh reads")
    st("rechecked", B, 230, "Rechecked?", "choice", 120, 70)
    st(
        "fan",
        B,
        700,
        "FanOut, then Children: inline Map (max 5) starting one child "
        "execution each, then ChildrenDone",
        "map",
        210,
        64,
    )
    st("build", C, 120, "Build<N>: operations for step N")
    st("route", C, 230, "Route<N>? (choice)", "choice_box", 200, 50)
    st("dispatch", C, 350, "Dispatch<N>?", "choice", 120, 70)
    st("resend", 520, 470, "ResendWait<N>", "wait", 110, 40)
    st("adapter", C, 470, "Adapter<N>: mutation adapter Lambda (never retried)", "adapter")
    st("crashed", C, 590, "Crashed<N>: reconcile before any resend")
    st("fcheck", D, 120, "FinalityCheck<N>: read until terminal")
    st("fwait", D, 230, "FinalityWait<N>", "wait", 120, 40)
    st(
        "complete",
        D,
        350,
        "AdapterComplete<N> (adapter), then Completed<N> (kit-step)",
        "adapter",
        170,
        56,
    )
    st("result", D, 470, "Result<N>: record the reply")
    st("error", E, 120, "Error: record the failure")
    st("after", E, 230, "AfterStop?", "choice", 120, 70)
    st("post", E, 350, "Postcondition")
    st("audit", E, 470, "Audit")
    st("done", E, 590, "Done", "end", 80, 44)

    ln("start", "tier")
    ln("tier", "recheck", "AUTO", ex=(1, 0.5), en=(0, 0.25), pts=[(260, 230), (260, 109)])
    ln("tier", "req", "SINGLE or DUAL (root only)")
    ln(
        "tier",
        "audit",
        "BLOCKED",
        ex=(0, 0.5),
        en=(1, 0.75),
        pts=[(50, 230), (50, 800), (1340, 800), (1340, 481)],
    )
    ln("req", "bind", "token or timeout")
    ln("bind", "approved")
    ln("approved", "recheck", "APPROVED", ex=(1, 0.5), en=(0, 0.75), pts=[(330, 590), (330, 131)])
    ln(
        "approved",
        "audit",
        "rejected or expired",
        ex=(0.5, 1),
        en=(0, 0.75),
        pts=[(A, 760), (1080, 760), (1080, 481)],
    )
    ln("recheck", "rechecked")
    ln("rechecked", "build", "steps", ex=(1, 0.5), en=(0, 0.5), pts=[(540, 230), (540, 120)])
    ln("rechecked", "fan", "fan-out", ex=(0.5, 1), en=(0.5, 0))
    ln(
        "rechecked",
        "after",
        "stopped",
        ex=(0.75, 0.25),
        en=(0, 0.25),
        pts=[(520, 212.5), (520, 65), (1110, 65), (1110, 212.5)],
    )
    ln("build", "route")
    ln(
        "route",
        "build",
        "next step",
        ex=(0, 0.3),
        en=(0.5, 0),
        pts=[(575, 220), (575, 80), (C, 80)],
    )
    ln("route", "dispatch", "pending", ex=(0.5, 1), en=(0.5, 0))
    ln("route", "fwait", "finality", ex=(1, 0.3), en=(0, 0.5), pts=[(850, 220), (850, 230)])
    ln("fwait", "fcheck")
    ln("fcheck", "route", ex=(0, 0.5), en=(1, 0.7), pts=[(830, 120), (830, 240)])
    ln(
        "route",
        "after",
        "last step done, or stop",
        ex=(0.85, 0),
        en=(0, 0.5),
        pts=[(770, 170), (1100, 170), (1100, 230)],
    )
    ln("dispatch", "adapter", "send")
    ln("dispatch", "resend", "resend", ex=(0, 0.5), en=(0.5, 0), pts=[(520, 350)])
    ln("resend", "adapter")
    ln("dispatch", "complete", "complete", ex=(1, 0.5), en=(0, 0.5))
    ln("complete", "route", ex=(0.2, 0), en=(0.9, 1), pts=[(882, 275), (780, 275)])
    ln("adapter", "result")
    ln("result", "route", ex=(1, 0.5), en=(0.8, 1), pts=[(1060, 470), (1060, 290), (760, 290)])
    ln("adapter", "crashed", "possible send")
    ln("crashed", "route", ex=(0, 0.5), en=(0, 0.8), pts=[(590, 590), (590, 245)])
    ln("fan", "after", "children done", ex=(1, 0.5), en=(0, 0.75), pts=[(1120, 700), (1120, 247)])
    ln("error", "after")
    ln("after", "post", "postcondition due")
    ln("after", "audit", "otherwise", ex=(1, 0.5), en=(1, 0.3), pts=[(1300, 230), (1300, 459)])
    ln("post", "audit")
    ln("audit", "done")

    notes = (
        section(
            "The machine",
            ul(
                [
                    "One Step Functions Standard machine per kit action, named "
                    f"{code('awx-<kit>-<action>')}, laid out from the action's manifest: step "
                    "order, "
                    "finality loops, approval timeout and fan-out come from the kit's workflow "
                    "spec",
                    "Pink states invoke the kit-step Lambda, which runs the workflow engine phase "
                    "by "
                    "phase. Orange states invoke the spine's mutation adapter Lambda. The kit-step "
                    "Lambda holds no Airwallex secret",
                    f"{code('local_runner')} runs the same phases in the same order; a test "
                    "interprets "
                    "the synthesised ASL and checks both runners agree",
                ]
            ),
        )
        + section(
            "Approval",
            ul(
                [
                    "Every proposal gets its policy tier. A model-started proposal is raised to "
                    "at least SINGLE unless the action's spec sets agent_min_tier=AUTO; "
                    "rule 11 in docs/workflow-rules.md lists the actions that do",
                    "AUTO goes straight to Recheck. BLOCKED goes to Audit and moves nothing",
                    "SINGLE or DUAL waits on waitForTaskToken until the approval Lambda sends the "
                    "token "
                    "back or the approval TTL ends",
                    "Binding accepts only a decision that names the stored proposal hash and "
                    "comes from "
                    "someone other than the proposer",
                    "A child machine has no approval states: its root already holds the approval",
                ]
            ),
        )
        + section(
            "Sending",
            ul(
                [
                    "The Adapter task never retries a send. Only a throttle on the invoke itself "
                    "(the "
                    "Lambda never ran) is retried",
                    "A timeout, crash or any other error after a possible send goes to Crashed, "
                    "which "
                    "reconciles before the engine decides whether to resend",
                    "A refusal (not authorised, tenant scope, request id reuse) goes to Error with "
                    "nothing sent",
                    "Finality steps wait, read and route until the object reaches a terminal "
                    "status",
                ]
            ),
        )
        + section(
            "Closing",
            ul(
                [
                    "AfterStop runs the postcondition when one is due, then Audit writes the "
                    "outcome",
                    "Postcondition and Audit have no automatic retry, so each runs once",
                    "Error states are omitted from the drawing for clarity. Recheck, Binding, "
                    "Build, Result, Crashed, FinalityCheck, AdapterComplete, Completed, and in "
                    "kit 6 FanOut, Children and ChildrenDone, catch into Error, then "
                    "AfterStop; RequestApproval sends a timeout to Binding and "
                    "any other failure to Error",
                    "Postcondition catches into PostconditionError, then Audit; a failure in "
                    "Start fails the execution",
                ]
            ),
        )
        + section(
            "Fan-out (kit 6)",
            ul(
                [
                    "FanOut computes one child intent per employer; the inline Map starts "
                    f"{code('<machine>-child')} for each (startExecution.sync, at most 5 at once)",
                    "ChildrenDone collects the child outcomes before AfterStop",
                ]
            ),
        )
        + section(
            "Code locations",
            ul(
                [
                    f"{code('infra/constructs/kit_workflow.py')}",
                    f"{code('packages/awx_spine_aws')}: engine and kit-step handler",
                    f"{code('packages/awx_common')}: workflow spec and runner rules",
                ]
            ),
        )
    )
    p.frame(notes, lanes=False)
    return p


# ------------------------------------------------------------------ 4. kits


@dataclass
class Kit:
    slug: str
    title: str
    tools_label: str
    machines_label: str
    approval_label: str
    adapter_label: str
    ddb_label: str
    awx_label: str
    tools: list[str]
    machines: list[str]
    approval: list[str]
    tenancy: str
    model: str
    code_owns: str
    test: str
    where: list[str]
    flow_extra: list[str] = field(default_factory=list)
    trigger: str | None = None
    s3_label: str | None = None


def kit_page(k: Kit) -> Page:
    p = Page(k.slug, k.title)
    n = k.slug.split("_")[0][1:]
    front(
        p,
        tool_label=k.tools_label,
        reader_label=f"Kit {n} reader Lambda: GET only, kit {n} allowlist",
        approval_label=k.approval_label,
        stepfn_label=f"Kit {n} kit-step Lambdas: validate, policy, build, postcondition",
        machines_label=k.machines_label,
        adapter_label=k.adapter_label,
        ddb_label=k.ddb_label,
        awx_label=k.awx_label,
        kms=False,
        cw=False,
        s3_label=k.s3_label,
    )
    steps = list(FRONT_STEPS)
    nxt = front_edges(p)

    def add(src, dst, text, **kw):
        nonlocal nxt
        p.edge(src, dst, nxt, **kw)
        steps.append(text)
        nxt += 1

    add("tools", "reader", "A read tool invokes the kit's reader Lambda")
    add("reader", "awx", "The reader sends the GET to Airwallex")
    add("tools", "sfn", "A propose tool starts the action's state machine")
    if k.slug.startswith("k6"):
        p.node(
            "child",
            "sfn",
            4,
            2,
            "Child machines: awx-k6-run_payroll-child, awx-k6-lock_rates-child",
        )
        add(
            "sfn",
            "step",
            "The root machine runs validate, policy and fan-out in the kit-step Lambda",
        )
        add("step", "ddb", "The kit-step Lambda writes the proposal, ledger and audit rows")
    else:
        add(
            "sfn",
            "step",
            "Each phase runs in the kit-step Lambda: validate, policy, build, "
            "finality check, postcondition",
        )
        add("step", "ddb", "The kit-step Lambda writes the proposal, ledger and audit rows")
    add("user", "api", "An approver decides with awxpoc approve or awxpoc reject", color="#DD344C")
    add("api", "appr", "The approval API invokes the approval Lambda", color="#DD344C")
    add(
        "appr",
        "sfn",
        "It binds the decision to the proposal hash and sends the task token",
        color="#DD344C",
    )
    if k.slug.startswith("k6"):
        add(
            "sfn",
            "child",
            "The inline Map starts one child execution per employer (at most 5 "
            "at once); each child runs its phases in the same kit-step Lambda",
        )
        add(
            "child",
            "adapter",
            "Each child sends its employer's operations to the adapter",
            color="#ED7100",
        )
    else:
        add("sfn", "adapter", "Each operation goes to the mutation adapter Lambda", color="#ED7100")
    add(
        "adapter", "ddb", "The adapter writes the operation record before it sends", color="#ED7100"
    )
    add("adapter", "secret", "It reads the Airwallex credentials", color="#ED7100", dashed=True)
    add(
        "adapter",
        "awx",
        "It sends the write and returns the reply",
        color="#ED7100",
        band=(3, "bot"),
    )
    if k.slug.startswith("k4"):
        p.node("op", "user", 6, 0, "Operator")
        p.node("upload", "lambda", 6, 2, "Evidence upload Lambda: types, hashes and indexes a file")
        add(
            "op",
            "upload",
            "An operator invokes the evidence upload Lambda with one JPG or PDF",
            dashed=True,
        )
        add(
            "upload",
            "s3",
            f"It writes the bytes under {code('evidence/k4/<tenant>/')}",
            dashed=True,
            band=(3, "bot"),
        )
        add(
            "upload",
            "ddb",
            "It writes the evidence index to the kit's ledger",
            dashed=True,
            band=(3, "top"),
        )
        add(
            "adapter",
            "s3",
            "For files.upload the adapter reads the evidence bytes",
            color="#ED7100",
            dashed=True,
        )
    if k.trigger:
        p.node("op", "user", 6, 0, "Operator using awx-deploy trigger")
        add("op", "sfn", k.trigger, dashed=True)
    steps += k.flow_extra
    notes = (
        section("Flow", ol(steps))
        + section("Tools", ul(k.tools))
        + section("State machines and what they write", ul(k.machines))
        + section("Approvers", ul(k.approval))
        + f"<b>Tenancy:</b> {k.tenancy}<br><br>"
        + f"<b>Model owns:</b> {k.model}<br><b>Code owns:</b> {k.code_owns}<br><br>"
        + f"<b>Acceptance test:</b> {k.test}<br><br>"
        + section("Code locations", ul(k.where))
    )
    p.frame(notes)
    return p


def _where(n: int, pkg: str, deploy: str | None) -> list[str]:
    items = [
        f"{code(f'kits/{pkg}/')}: workflow specs, tools, reads, policy, ledger, tests",
        f"Stack AwxKitK{n}: the generic KitStack built from the kit's manifest",
        f"Tool Lambda: Gateway target k{n} in AwxAgentStack",
    ]
    if deploy:
        items.append(f"{code(f'deploy/k{n}.yaml')}: {deploy}")
    return items


PLATFORM = (
    "one Airwallex platform account. No x-on-behalf-of header; tenancy is the "
    "platform's own tenant."
)

KITS = [
    Kit(
        "k1_treasury",
        "Kit 1 Adaptive Treasury Controller: contradicting evidence changes the approval tier",
        tools_label="Kit 1 tool Lambda: read and propose tools",
        machines_label="Machines: lock_rate, execute_plan, release_obligation",
        approval_label="Approval Lambda: records a platform-tenant approver's decision",
        adapter_label="Adapter: FX quotes, conversions, transfers",
        ddb_label="Shared DynamoDB: obligations book, rate locks, proposals, operations",
        awx_label="Airwallex sandbox API (platform account)",
        tools=[
            "Read: read_balances, read_obligations, read_fx_rate, read_treasury_plan",
            "Propose: propose_lock_rate, propose_execute_plan, propose_release_obligation",
        ],
        machines=[
            "awx-k1-lock_rate: quote, POST /api/v1/fx/quotes/create",
            "awx-k1-execute_plan: convert, POST /api/v1/fx/conversions/create; then "
            "transfer, POST /api/v1/transfers/create",
            "awx-k1-release_obligation: apply, a store-only step that sends nothing",
        ],
        approval=[
            "The policy tier decides: a contradicted forecast lowers confidence and needs "
            "one approver from the platform tenant",
            "When the agent proposes them, lock_rate (creates a quote) and execute_plan "
            "(converts and pays within the policy's autonomous limits) may run AUTO; "
            "release_obligation always needs an approver",
        ],
        tenancy=PLATFORM,
        model="reads the customer email, decides whether it contradicts a receipt forecast, "
        "cites the line, explains the plan.",
        code_owns="every amount, fee, rate ceiling, reserve floor, priority, confidence band, "
        "approval tier, quote burn and request id.",
        test="supported and contradicted forecasts produce the same conversion; the "
        "contradicted one needs an approver, the fee-inclusive plan stays above the reserve "
        "floor, and a replay sends no second conversion or transfer.",
        where=_where(1, "k1_treasury", "the operator command that seeds the book"),
    ),
    Kit(
        "k2_purchase",
        "Kit 2 Intent-Bound Purchase Agent: a commercial choice becomes enforceable card controls",
        tools_label="Kit 2 tool Lambda: read and propose tools",
        machines_label="Machines: issue_card, run_limit_test and the card changes",
        approval_label="Approval Lambda: records a platform-tenant approver's decision",
        adapter_label="Adapter: cardholders, cards, card updates, simulated authorisations",
        ddb_label="Shared DynamoDB: purchase plan, card state, proposals, operations",
        awx_label="Airwallex sandbox API (platform account)",
        tools=[
            "Read: read_purchase_plan, read_card_controls, read_card_transactions",
            "Propose: propose_issue_card, propose_update_card_controls, propose_freeze_card, "
            "propose_unfreeze_card, propose_close_card, propose_run_limit_test",
        ],
        machines=[
            "awx-k2-issue_card: cardholder, POST /api/v1/issuing/cardholders/create; "
            "card, POST /api/v1/issuing/cards/create",
            "awx-k2-update_card_controls, awx-k2-freeze_card, awx-k2-unfreeze_card, "
            "awx-k2-close_card: POST /api/v1/issuing/cards/{id}/update",
            "awx-k2-run_limit_test: limit probes, POST /api/v1/simulation/issuing/create "
            "(currency, merchant category, over limit, at limit, cumulative)",
        ],
        approval=[
            "An approver of the platform tenant approves the exact control set before a "
            "card exists",
            "issue_card, unfreeze_card, close_card and run_limit_test always need an "
            "approver; update_card_controls and freeze_card may run AUTO",
        ],
        tenancy=PLATFORM,
        model="extracts price, discount, cancellation notice, start and renewal dates, explains "
        "the result.",
        code_owns="the cash curve against the reserve floor, the plan choice, every card limit, "
        "merchant categories, currencies, probe amounts and request ids.",
        test="exactly the limit clears, the limit plus 0.01 is declined LIMIT_EXCEEDED, a "
        "disallowed currency and merchant category are declined with their own reasons, "
        "and the cumulative limit then blocks.",
        where=_where(2, "k2_purchase", None),
    ),
    Kit(
        "k3_incident",
        "Kit 3 Payment Ops Incident Commander: one replacement, only after the transfer has failed",
        tools_label="Kit 3 tool Lambda: read and propose tools",
        machines_label="Machines: replace transfer, close incident, simulate failure",
        approval_label="Approval Lambda: records a platform-tenant approver's decision",
        adapter_label="Adapter: one replacement transfer, simulated transitions",
        ddb_label="Shared DynamoDB: incident, duplicate lock, proposals, operations",
        awx_label="Airwallex sandbox API (platform account)",
        tools=[
            "Read: read_transfer, read_incident_assessment",
            "Propose: propose_replace_failed_transfer, propose_close_incident, "
            "propose_simulate_transfer_failure",
        ],
        machines=[
            "awx-k3-replace_failed_transfer: replace, POST /api/v1/transfers/create "
            "(exactly one, under a duplicate lock)",
            "awx-k3-simulate_transfer_failure: to_sent then to_failed, POST "
            "/api/v1/simulation/transfers/{id}/transition",
            "awx-k3-close_incident: record, sends nothing",
        ],
        approval=[
            "Every replacement and every simulated failure needs one approver of the "
            "platform tenant; close_incident may run AUTO",
            "The finality allowlist refuses a replacement unless the original is CANCELLED "
            "with a returned-by-beneficiary-bank failure",
        ],
        tenancy=PLATFORM,
        model="summarises the supplier message, reads the assessment, recommends wait, replace "
        "or escalate, explains.",
        code_owns="the finality allowlist, the duplicate lock, fees, approval, the replacement "
        "transfer and its request id.",
        test="at SENT the kit waits, takes no duplicate lock and sends no transfer; after the "
        "original moves to CANCELLED with a bank-returned failure and a person approves, "
        "exactly one replacement is sent with one new request id.",
        where=_where(3, "k3_incident", None),
    ),
    Kit(
        "k4_dispute",
        "Kit 4 Dispute Response Agent: evidence, economics and a deadline decide the response",
        tools_label="Kit 4 tool Lambda: read and propose tools",
        machines_label="Machines: accept, challenge, refer, close, clear_dispute_response",
        approval_label="Approval Lambda: records a platform-tenant approver's decision",
        adapter_label="Adapter: evidence upload, challenge, accept",
        ddb_label="Shared DynamoDB: case state, evidence index, proposals, operations",
        awx_label="Airwallex sandbox API (platform account)",
        s3_label="Amazon S3: evidence/k4/<tenant>/ (JPG and PDF)",
        tools=[
            "Read: read_dispute_queue, read_dispute, read_dispute_economics, read_refunds",
            "Propose: propose_accept_dispute, propose_challenge_dispute, "
            "propose_refer_dispute, propose_clear_dispute_response, propose_close_dispute",
        ],
        machines=[
            "awx-k4-challenge_dispute: upload_evidence, POST /api/v1/files/upload; "
            "submit_challenge, POST /api/v1/pa/payment_disputes/{id}/challenge",
            "awx-k4-accept_dispute: accept, POST /api/v1/pa/payment_disputes/{id}/accept",
            "awx-k4-refer_dispute, awx-k4-close_dispute, awx-k4-clear_dispute_response: "
            "record a referral, outcome or clearance; send nothing",
        ],
        approval=[
            "accept, challenge and clear_dispute_response need one approver of the "
            "platform tenant; refer and close may run AUTO",
            "A proposer cannot approve their own accept",
        ],
        tenancy=PLATFORM,
        model="reads JPG and PDF evidence, returns typed facts citing an evidence id and an exact "
        "quote, writes the narrative for the approver.",
        code_owns="the evidence index (type from magic bytes, size, hash), citation checks, "
        "economics, deadline, approval tier, case state and request ids.",
        test="the strong fraud case is challenged with JPG and PDF evidence, resolves to "
        "rejection, records the fee and outcome, and sends no second challenge; the small "
        "case cannot be accepted without the bound approval.",
        where=[
            *_where(4, "k4_dispute", None),
            "Evidence upload Lambda: declared in the kit's FUNCTIONS, built by KitStack",
        ],
        flow_extra=[],
    ),
    Kit(
        "k5_platform_spend",
        "Kit 5 Platform Spend Controller: cards and bridges over connected accounts",
        tools_label="Kit 5 tool Lambda: read and propose tools",
        machines_label="Machines: cards, simulated spend, shortfalls, bridge, fee, repayment",
        approval_label="Approval Lambda: tenant approver, or kit 5 operator for a bridge",
        adapter_label="Adapter: cards, simulated spend, bridge transfers, charges",
        ddb_label="Shared DynamoDB: tenants, cards, bridge queue, capital ledger, operations",
        awx_label="Airwallex sandbox: connected accounts (header or body)",
        tools=[
            "Read: read_tenant_summary, read_cards, read_card_transactions, read_bridge_position",
            "Propose: propose_issue_card, propose_simulate_card_spend, "
            "propose_register_shortfall, propose_bridge_shortfall, propose_reconcile_bridge, "
            "propose_collect_fee, propose_collect_repayment",
        ],
        machines=[
            "awx-k5-issue_card: POST /api/v1/issuing/cardholders/create, then "
            "POST /api/v1/issuing/cards/create",
            "awx-k5-simulate_card_spend: POST /api/v1/simulation/issuing/create",
            "awx-k5-bridge_shortfall: POST /api/v1/connected_account_transfers/create",
            "awx-k5-collect_fee, awx-k5-collect_repayment: POST /api/v1/charges/create",
            "awx-k5-register_shortfall, awx-k5-reconcile_bridge: record in the kit's "
            "ledger; send nothing",
        ],
        approval=[
            "bridge_shortfall moves platform capital, so the kit 5 operator's approver decides it",
            "Every other action is approved in the customer's own tenant; "
            "register_shortfall and reconcile_bridge may run AUTO",
        ],
        tenancy="each customer is a connected account. The tenant comes from the verified "
        "token and the tenants table, never from the model; the adapter sends "
        "x-on-behalf-of for customer-scope operations.",
        model="picks the obligation, card, test scenario or card program by name and explains "
        "the customer context and priority.",
        code_owns="tenant and account, shortfall, fee, repayment, card limits and merchant "
        "categories, eligibility, the reserve allocator, approval tier and request ids.",
        test="with a binding platform reserve: issue a customer card over the tenant path, "
        "bridge one shortfall to SETTLED, collect an approved fee, deny a second advance "
        "that would breach the floor, and replay the card simulation without a second "
        "transaction.",
        where=_where(5, "k5_platform_spend", "tenants rows and operator commands"),
    ),
    Kit(
        "k6_payroll",
        "Kit 6 Multi-Employer Payroll Executor: one employer per child execution",
        tools_label="Kit 6 tool Lambda: read and propose tools",
        machines_label="Root machines: run_payroll, lock_rates, release_run_claim",
        approval_label="Approval Lambda: records a payroll operator approver's decision",
        adapter_label="Adapter: beneficiaries, quotes, conversions, payouts, fee",
        ddb_label="Shared DynamoDB: per-employer book and ledger, run claims, operations",
        awx_label="Airwallex sandbox: employer accounts (header or body)",
        tools=[
            "Read: read_payroll_plan, read_employer_balances, read_payout_status",
            "Propose: propose_run_payroll, propose_lock_rates, propose_release_run_claim",
            "Every tool runs as the payroll operator tenant k6:operator",
        ],
        machines=[
            "awx-k6-run_payroll: fan-out to awx-k6-run_payroll-child, one per employer: "
            "snapshot; POST /api/v1/beneficiaries/validate; POST /api/v1/beneficiaries/create; "
            "POST /api/v1/fx/conversions/create; POST /api/v1/transfers/create; "
            "POST /api/v1/charges/create (platform fee)",
            "awx-k6-lock_rates: fan-out to awx-k6-lock_rates-child: POST /api/v1/fx/quotes/create",
            "awx-k6-release_run_claim: release, a store-only step",
        ],
        approval=[
            "The root proposal is approved in the operator tenant: run_payroll needs one "
            "approver (two above the per-currency threshold); release_run_claim needs one; "
            "lock_rates (creates quotes) may run AUTO",
            "A child has no approval states; it runs AUTO only when its employer's own "
            "wallet funds it, otherwise it escalates",
        ],
        tenancy="each employer is a connected account; each child runs as that employer's "
        "tenant, resolved server-side, and never draws on another employer's wallet.",
        model="reads the payroll file into the tool input, names employers and contractors by "
        "code, explains shortfalls and statuses.",
        code_owns="every amount, fee, FX size, sell cap, funding decision, request id, connected "
        "account, tenant check, approval tier and postcondition.",
        test="fund employer A, leave B short, deposit into C mid-run: A completes, B escalates "
        "without cross-funding, C's deposit never changes B's decision, every FX delta "
        "lands in the intended wallet and every transfer is terminal.",
        where=_where(6, "k6_payroll", "tenants rows (no operator commands)"),
        trigger=f"{code('awx-deploy trigger')} starts run_payroll (the scheduled weekly run)",
    ),
    Kit(
        "k7_lending",
        "Kit 7 Portfolio Lending Agent: code computes every repayment and tranche",
        tools_label="Kit 7 tool Lambda: read and propose tools",
        machines_label="Machines: collect repayment, disburse advance, reconcile",
        approval_label="Approval Lambda: borrower approver, or kit 7 operator for a disbursement",
        adapter_label="Adapter: repayment charges, disbursements, platform report",
        ddb_label="Shared DynamoDB: loan subledger, portfolio, proposals, operations",
        awx_label="Airwallex sandbox: platform calls naming borrower accounts",
        tools=[
            "Read: read_borrower_position, read_weekly_collection_preview, read_portfolio_position",
            "Propose: propose_collect_weekly_repayment, propose_disburse_advance, "
            "propose_reconcile_portfolio",
        ],
        machines=[
            "awx-k7-collect_weekly_repayment: charge, POST /api/v1/charges/create",
            "awx-k7-disburse_advance: transfer, POST /api/v1/connected_account_transfers/create",
            "awx-k7-reconcile_portfolio: report, POST /api/v1/platform_reports/create",
        ],
        approval=[
            "disburse_advance moves lender money, so the kit 7 operator's approver decides "
            "every tranche (two above the threshold)",
            "collect_weekly_repayment is approved in the borrower's tenant; it runs AUTO "
            "only when the schedule starts it; reconcile_portfolio may run AUTO",
        ],
        tenancy="each borrower is a connected account; loans enter the book through operator "
        "commands with fixed terms.",
        model="picks a loan and an ISO week, reads positions and explains them; never an amount.",
        code_owns="the repayment formula, rounding, arrears, reserve floor, tranche sizing, "
        "ledger postings, approval tier, tenant binding and reconciliation.",
        test="make the reserve floor bind and underpay one borrower: the third disbursement "
        "shrinks so platform cash stays above the floor, a collection replay is rejected, "
        "approval tampering fails closed, and the ledger reconciles to settled movements.",
        where=_where(7, "k7_lending", "tenants rows and operator commands"),
        trigger=f"{code('awx-deploy trigger')} starts collect_weekly_repayment or "
        "reconcile_portfolio (the scheduled runs)",
    ),
    Kit(
        "k8_settlement",
        "Kit 8 Marketplace Settlement Agent: one seller's evidence changes only that seller",
        tools_label="Kit 8 tool Lambda: read and propose tools",
        machines_label="Machines: settle, recalculate reserve, recover, reconcile, resolve",
        approval_label="Approval Lambda: seller approver, or kit 8 operator",
        adapter_label="Adapter: seller payouts, recovery charges",
        ddb_label="Shared DynamoDB: per-seller reserve ledger, proposals, operations",
        awx_label="Airwallex sandbox: platform calls naming seller accounts",
        tools=[
            "Read: read_seller_balance, read_seller_ledger, read_carrier_event",
            "Propose: propose_settle_seller_cycle, propose_recalculate_seller_reserve, "
            "propose_reconcile_seller_money, propose_resolve_in_flight_money, "
            "propose_recover_shortfall",
        ],
        machines=[
            "awx-k8-settle_seller_cycle: payout, POST /api/v1/connected_account_transfers/create",
            "awx-k8-recover_shortfall: recover, POST /api/v1/charges/create",
            "awx-k8-recalculate_seller_reserve, awx-k8-resolve_in_flight_money: record in "
            "the seller's ledger; send nothing",
            "awx-k8-reconcile_seller_money: read back in-flight money; send nothing",
        ],
        approval=[
            "settle_seller_cycle, recalculate_seller_reserve and resolve_in_flight_money "
            "change what a seller is paid or owed, so the kit 8 operator's approver "
            "decides them",
            "recover_shortfall is approved in the seller's tenant; reconcile_seller_money "
            "may run AUTO (it reads back and reports, and sends nothing)",
        ],
        tenancy="each seller is a connected account; every spec runs in one seller tenant and "
        "reads and writes only that seller's ledger.",
        model="reads ledgers, balances and carrier events, summarises the fenced carrier "
        "narrative and explains.",
        code_owns="every reserve, payout, shortfall and charge amount, every account and every "
        "request id.",
        test="a carrier failure for seller X changes only X's reserve; X's payout falls by "
        "exactly that amount, other sellers' ledgers are unchanged, the cycle identity "
        "balances to zero, and when X lacks funds no recovery charge is attempted (the "
        "shortfall stays a receivable).",
        where=_where(8, "k8_settlement", "tenants rows and operator commands"),
        trigger=f"{code('awx-deploy trigger')} starts settle_seller_cycle or "
        "reconcile_seller_money (the scheduled runs)",
    ),
]


# ------------------------------------------------------------------ 5. overviews
#
# One lightweight overview per kit and one of every kit on the shared spine, drawn for the top
# of a README: larger type, no notes panel and no step numbers. Each edge carries a short label
# instead. They use the same router and the same checks as the reference architecture pages.

OVERVIEW = Geo(
    cx=[230, 670, 1110],
    ry=[180, 410, 640, 870],
    icon=56,
    label_w=380,
    label_h=62,
    box_w=340,
    box_h=90,
    top_band=[-86, -78, -70, -62, -54],
    bot_band=[104, 114, 124],
    gutter=[220, 208, 232, 214, 226, 202, 238],
    guess=220,
    font=14,
    max_box=130,
    max_icon=150,
    lab_font=14,
    lab_cw=8.2,
    lab_pad=14,
    lab_h=22,
    page_w=1400,
)
ALL_KITS = Geo(
    cx=[150, 430, 730, 1040, 1300],
    ry=[190, 420, 716, 1018, 1230],
    icon=56,
    label_w=230,
    label_h=62,
    box_w=230,
    box_h=84,
    top_band=[-86, -78, -70, -62, -54],
    bot_band=[104, 114],
    gutter=[128, 136, 144, 132, 140, 148],
    guess=136,
    font=14,
    max_box=76,
    max_icon=84,
    lab_font=14,
    lab_cw=8.2,
    lab_pad=14,
    lab_h=22,
    page_w=1460,
)
OVERVIEW_LANES = [
    ("People: no one approves their own proposal", "#F5F5F5", "#7D8998"),
    ("Agent: reads and proposes only", "#E6F6F4", "#01A88D"),
    ("Control: code sets every amount and the approval tier", "#FDEBF3", "#E7157B"),
    ("Execution: only the mutation adapter writes to Airwallex", "#FFF3E6", "#ED7100"),
]
ALL_KITS_LANES = [  # short titles where edges cross a lane's top border near the left
    OVERVIEW_LANES[0],
    OVERVIEW_LANES[1],
    ("Kits on the spine", "#FFFFFF", "#545B64"),
    OVERVIEW_LANES[2],
    ("Execution", "#FFF3E6", "#ED7100"),
]
APPROVE, WRITE = "#DD344C", "#ED7100"


def keep_clear(p: Page, lanes: list[tuple[str, str, str]], bounds: list[tuple]) -> None:
    """Keep edge labels off the lane titles and the lane borders."""
    for (label, _fill, _stroke), (top, height) in zip(lanes, bounds, strict=True):
        p.label_rects.append((28, top + 2, 10 + 8.6 * len(label), 22))
        p.label_rects.append((20, top - 6, p.geo.page_w - 40, 12))
        p.label_rects.append((20, top + height - 6, p.geo.page_w - 40, 12))


def overview_frame(p: Page, lanes: list[tuple[str, str, str]], bounds: list[tuple]) -> None:
    """Title, lane backgrounds behind the body, lane titles above the edges."""
    body, self_cells = p.cells, []
    p.cells = self_cells
    right = p.geo.page_w - 20
    p.text(20, 14, right - 20, 40, f"<b>{escape(p.title)}</b>", "fontSize=22;verticalAlign=middle;")
    for (_label, fill, stroke), (top, height) in zip(lanes, bounds, strict=True):
        p.rect(20, top, right - 20, height, "", fill, stroke)
    p.cells.extend(body)
    start = len(p.cells)
    for (label, fill, stroke), (top, _height) in zip(lanes, bounds, strict=True):
        p.text(
            28,
            top + 2,
            760,
            22,
            label,
            f"fontSize=14;fontStyle=1;fontColor={stroke};whiteSpace=nowrap;"
            f"labelBackgroundColor={fill};",
        )
    p.label_cells[:0] = p.cells[start:]
    del p.cells[start:]


def check_overview(
    p: Page, lanes: list[tuple[str, str, str]], bounds: list[tuple], labels: list[tuple]
) -> None:
    """Rules the overview pages add to Page.check: every edge label sits inside one lane, no
    edge runs through a lane title, and no two edges run side by side closer than 18 px (they
    read as one line)."""
    right = p.geo.page_w - 20
    for (title, _fill, _stroke), (top, _height) in zip(lanes, bounds, strict=True):
        r = (28, top + 2, 4 + 7.6 * len(title), 22)  # 14 px bold, about 7.2 px a character
        for name, a, b in p.segments:
            if _seg_hits(a, b, r):
                raise ValueError(f"{p.slug}: edge {name} runs through the lane title {title!r}")
    for x, y, w, h in labels:
        inside = any(top + 4 <= y and y + h <= top + height - 4 for top, height in bounds)
        if not inside or x < 24 or x + w > right - 4:
            raise ValueError(f"{p.slug}: an edge label crosses a lane border at ({x}, {y})")
    near = 18
    for i, (n1, a1, b1) in enumerate(p.segments):
        for n2, a2, b2 in p.segments[i + 1 :]:
            if n1 == n2:
                continue
            for ax in (0, 1):  # 0: vertical segments (same x), 1: horizontal (same y)
                if a1[ax] == b1[ax] and a2[ax] == b2[ax] and abs(a1[ax] - a2[ax]) < near:
                    lo1, hi1 = sorted((a1[1 - ax], b1[1 - ax]))
                    lo2, hi2 = sorted((a2[1 - ax], b2[1 - ax]))
                    if min(hi1, hi2) - max(lo1, lo2) > 0:
                        raise ValueError(f"{p.slug}: edges {n1} and {n2} run side by side")


@dataclass
class Overview:
    """What a kit's overview shows. Every name below comes from the kit's code: the people
    and their tenants from the specs' approver scopes (awx_common.workflow.ApproverScope) and
    awx_common.tenancy, the AUTO actions from each spec's agent_min_tier (docs/workflow-rules.md
    rule 11), the triggers from awx_spine_aws.deploy.SCHEDULED_ACTIONS and the writes from the
    endpoints the specs' steps send, including a finality's sandbox advance."""

    kit: Kit
    title: str  # the kit README's H1
    name: str  # the kit's name, for its card on the all-kits page
    requester: str
    approver: str
    writes: str  # every write the kit sends to Airwallex, on the overview and the kit's card
    connected: str | None = None  # kits 5 to 8: whose connected accounts
    operator: str | None = None  # the deployment operator's awx-deploy trigger actions

    @property
    def n(self) -> str:
        return self.kit.slug.split("_")[0][1:]

    @property
    def reads(self) -> str:
        return self.kit.tools[0]  # "Read: read_..., read_..."

    @property
    def actions(self) -> list[str]:
        propose = self.kit.tools[1].removeprefix("Propose: ").split(", ")
        return [t.removeprefix("propose_") for t in propose]


def overview_page(o: Overview) -> Page:
    """One kit on the overview lanes. Columns: x 230, 670 and 1110; rows: y 180, 410, 640 and 870.
    Every edge has an explicit route, so each runs in its own column or row."""
    slug = o.kit.slug
    k4, k6 = slug.startswith("k4"), slug.startswith("k6")
    p = Page(f"overview/{slug}", o.title, height=1000, geo=OVERVIEW)
    p.node("user", "user", 0, 0, o.requester, lw=340)
    p.node("approver", "user", 1, 0, o.approver, lw=420)
    p.node(
        "agent",
        "agentcore",
        0,
        1,
        "Amazon Bedrock AgentCore Runtime: the agent and its Amazon Bedrock model",
        w=300,
    )
    p.node(
        "tools",
        "lambda",
        1,
        1,
        f"Kit {o.n} tools (AWS Lambda). {o.reads}. Propose: one tool per action",
    )
    p.node("sfn", "sfn", 0, 2, "AWS Step Functions: " + ", ".join(o.actions))
    p.node("adapter", "lambda", 0, 3, "Mutation adapter (AWS Lambda): the only Airwallex writer")
    account = "" if o.connected else ", platform account"
    p.node("awx", "ext", 1, 3, f"Airwallex sandbox{account}: {o.writes}", w=380)
    if o.connected:
        p.node("accounts", "ext", 2, 3, o.connected, w=220)
    if o.operator:
        p.node("op", "user", 2, 0, f"{o.operator}. {SCHEDULED}", lw=440)
    if k4:
        p.node("op", "user", 2, 0, "Operator who uploads each evidence file")
        p.node("upload", "lambda", 2, 2, "Evidence upload Lambda: types, hashes and indexes a file")
        p.node("s3", "s3", 2, 3, "Amazon S3 artifacts bucket: evidence/k4/<tenant>/", lw=300)
    if k6:
        p.node(
            "child",
            "sfn",
            2,
            2,
            "One child workflow per employer, in that employer's tenant. A payroll child workflow "
            "runs AUTO only when its own wallet funds it",
        )

    p.wire("user", "agent", "asks", ("R", 0.5), ("R", 1 / 6), [(405, 180), (405, 380)], prefer=[0])
    p.wire("agent", "tools", "calls", ("R", 0.5), ("L", 0.5), prefer=[0])
    approve = [(440, 185.6), (440, 560), (213.2, 560)]
    p.wire(
        "approver",
        "sfn",
        "approves or rejects",
        ("L", 0.6),
        ("T", 0.2),
        approve,
        color=APPROVE,
        prefer=[0],
    )
    p.wire(
        "tools",
        "sfn",
        "proposes",
        ("R", 0.75),
        ("R", 0.2),
        [(880, 424), (880, 623.2)],
        prefer=[2],
    )
    p.wire(
        "tools",
        "awx",
        "reads (GET only)",
        ("R", 0.25),
        ("R", 0.3),
        [(905, 396), (905, 852)],
        prefer=[0],
    )
    if o.operator:
        p.wire(
            "op",
            "sfn",
            "starts",
            ("R", 0.5),
            ("T", 0.8),
            [(1350, 180), (1350, 590), (246.8, 590)],
            dashed=True,
            prefer=[0],
        )
    if k6:
        p.wire("sfn", "child", "fans out", ("R", 0.8), ("L", 0.8), prefer=[0])
        p.wire(
            "child",
            "adapter",
            "sends",
            ("R", 0.5),
            ("T", 0.5),
            [(1320, 640), (1320, 800), (230, 800)],
            color=WRITE,
            prefer=[2],
        )
    else:
        p.wire(
            "sfn",
            "adapter",
            "sends",
            ("R", 0.95),
            ("T", 0.5),
            [(480, 665.2), (480, 800), (230, 800)],
            color=WRITE,
            prefer=[2],
        )
    p.wire("adapter", "awx", "writes", ("R", 0.5), ("L", 0.5), color=WRITE, prefer=[0])
    if o.connected:
        p.wire(
            "awx",
            "accounts",
            "on the named connected account",
            ("B", 0.5),
            ("B", 0.5),
            [(670, 950), (1110, 950)],
            color=WRITE,
            prefer=[1],
        )
    if k4:
        p.wire(
            "op",
            "upload",
            "invokes",
            ("R", 0.5),
            ("T", 0.5),
            [(1320, 180), (1320, 580), (1110, 580)],
            dashed=True,
            prefer=[0],
        )
        p.wire(
            "upload",
            "s3",
            "stores the file",
            ("R", 0.5),
            ("R", 0.5),
            [(1350, 640), (1350, 870)],
            dashed=True,
            prefer=[2],
        )
        p.wire(
            "adapter",
            "s3",
            "reads the file",
            ("R", 0.85),
            ("L", 0.5),
            [(460, 889.6), (460, 945), (940, 945), (940, 870)],
            color=WRITE,
            dashed=True,
            prefer=[2],
        )
    bounds = [(y - 116, 224) for y in OVERVIEW.ry]
    keep_clear(p, OVERVIEW_LANES, bounds)
    n0 = len(p.label_rects)
    p.place_labels()
    p.check()
    check_overview(p, OVERVIEW_LANES, bounds, p.label_rects[n0:])
    overview_frame(p, OVERVIEW_LANES, bounds)
    return p


KIT = {k.slug: k for k in KITS}
PLATFORM_REQUESTER = "Requester in the platform tenant, using the awxpoc command line"
# deploy.py TRIGGERED_BY: the proposer on record for every run awx-deploy trigger starts
SCHEDULED = "Scheduled runs are recorded as proposed by awx-deploy:schedule."
PLATFORM_APPROVER = "Approver in the platform tenant."

OVERVIEWS = [
    Overview(
        KIT["k1_treasury"],
        "Kit 1: Adaptive Treasury Controller",
        "Adaptive Treasury Controller",
        PLATFORM_REQUESTER,
        f"{PLATFORM_APPROVER} lock_rate and execute_plan can run AUTO within the policy's limits",
        "foreign exchange quotes, conversions, transfers and simulated transfer transitions",
    ),
    Overview(
        KIT["k2_purchase"],
        "Kit 2: Intent-Bound Purchase Agent",
        "Intent-Bound Purchase Agent",
        PLATFORM_REQUESTER,
        f"{PLATFORM_APPROVER} update_card_controls and freeze_card can run AUTO when they only "
        "tighten a card",
        "cardholders, cards, card updates and simulated authorisations",
    ),
    Overview(
        KIT["k3_incident"],
        "Kit 3: Payment Ops Incident Commander",
        "Payment Ops Incident Commander",
        PLATFORM_REQUESTER,
        f"{PLATFORM_APPROVER} close_incident can run AUTO (it records a closure)",
        "one replacement transfer and simulated transfer transitions",
    ),
    Overview(
        KIT["k4_dispute"],
        "Kit 4: Dispute Response Agent",
        "Dispute Response Agent",
        PLATFORM_REQUESTER,
        f"{PLATFORM_APPROVER} refer_dispute and close_dispute can run AUTO (they record a "
        "referral or an outcome)",
        "evidence files, dispute challenges and acceptances",
    ),
    Overview(
        KIT["k5_platform_spend"],
        "Kit 5: Platform Spend Controller",
        "Platform Spend Controller",
        "Requester in a customer's tenant, using the awxpoc command line",
        "Approver in the customer's tenant, or in the kit 5 operator tenant (k5:operator) for "
        "bridge_shortfall. register_shortfall and reconcile_bridge can run AUTO",
        "cardholders, cards, simulated card spend, bridge transfers and charges",
        connected="Connected accounts, one per customer",
    ),
    Overview(
        KIT["k6_payroll"],
        "Kit 6: Multi-Employer Payroll Executor",
        "Multi-Employer Payroll Executor",
        "Payroll operator (tenant k6:operator), using the awxpoc command line",
        "Approver in the payroll operator tenant. lock_rates can run AUTO (it creates quotes)",
        "beneficiaries, quotes, conversions, payouts, simulated transfer transitions and the "
        "platform fee",
        connected="Connected accounts, one per employer",
        operator="Deployment operator: starts run_payroll with awx-deploy trigger",
    ),
    Overview(
        KIT["k7_lending"],
        "Kit 7: Portfolio Lending Agent",
        "Portfolio Lending Agent",
        "Requester in a borrower's tenant, or in the platform tenant for portfolio reads and "
        "reconciliation, using the awxpoc command line",
        "Approver in the borrower's tenant, or in the kit 7 operator tenant (k7:operator) for "
        "disburse_advance. reconcile_portfolio can run AUTO",
        "repayment charges, advance transfers and a balance report request",
        connected="Connected accounts, one per borrower",
        operator="Deployment operator: starts collect_weekly_repayment and reconcile_portfolio "
        "with awx-deploy trigger",
    ),
    Overview(
        KIT["k8_settlement"],
        "Kit 8: Marketplace Settlement Agent",
        "Marketplace Settlement Agent",
        "Requester in a seller's tenant, using the awxpoc command line",
        "Approver in the kit 8 operator tenant (k8:operator), or in the seller's tenant for "
        "recover_shortfall. reconcile_seller_money can run AUTO",
        "seller payouts and recovery charges",
        connected="Connected accounts, one per seller",
        operator="Deployment operator: starts settle_seller_cycle and reconcile_seller_money "
        "with awx-deploy trigger",
    ),
]


def group(p: Page, key: str, xc: float, title: str, kits: list[Overview]) -> None:
    """A kit group on the all-kits page: one box with a card per kit, centred at x `xc` in the
    kits lane. Edges route to the group, never to a card."""
    w, h = 600, 300
    nd = Node(key, "group", 0, 2, title, cid=p._id("n"), geo=p.geo, w=w, h=h, xc=xc)
    x, y = nd.x - w / 2, nd.y - h / 2
    p.cells.append(
        f'<mxCell id="{nd.cid}" value="{escape(escape(title))}" style="rounded=1;arcSize=4;'
        "whiteSpace=wrap;html=1;fillColor=#F5F5F5;strokeColor=#545B64;strokeWidth=2;"
        "verticalAlign=top;align=left;spacingLeft=10;spacingTop=4;fontSize=14;fontStyle=1;"
        f'fontColor=#232F3E;" vertex="1" parent="1"><mxGeometry x="{x}" y="{y}" width="{w}" '
        f'height="{h}" as="geometry"/></mxCell>'
    )
    cw, ch = 286, 124
    for i, o in enumerate(kits):
        cx, cy = x + 10 + (i % 2) * (cw + 8), y + 34 + (i // 2) * (ch + 8)
        html = f"<b>Kit {o.n}: {escape(o.name)}</b><br>Airwallex writes: {escape(o.writes)}"
        p.cells.append(
            f'<mxCell id="{p._id("c")}" value="{escape(html)}" style="rounded=1;arcSize=6;'
            "whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#E7157B;verticalAlign=middle;"
            'align=left;spacingLeft=8;spacingRight=4;fontSize=14;fontColor=#232F3E;" vertex="1" '
            f'parent="1"><mxGeometry x="{cx}" y="{cy}" width="{cw}" height="{ch}" '
            'as="geometry"/></mxCell>'
        )
    p.nodes[key] = nd


def all_kits_page() -> Page:
    """The spine once, with kits 1 to 8 as cards in their tenancy group. Columns: x 150, 430,
    730, 1040 and 1300; rows: y 190, 420, 716, 1018 and 1230. Every edge has an explicit route."""
    p = Page(
        "overview/all-kits",
        "Airwallex Agentic Banking on AWS: kits 1 to 8 and the shared spine",
        height=1390,
        geo=ALL_KITS,
    )
    p.node("user", "user", 0, 0, "Requester or approver using the awxpoc command line")
    p.node("cognito", "cognito", 1, 0, "Amazon Cognito: sign-in, groups and the tenant claim")
    p.node(
        "op",
        "user",
        3,
        0,
        f"Deployment operator: starts scheduled workflows with awx-deploy trigger. {SCHEDULED}",
        lw=440,
    )
    p.node("model", "bedrock", 0, 1, "Amazon Bedrock model (Claude Sonnet)")
    p.node("runtime", "agentcore", 1, 1, "Amazon Bedrock AgentCore Runtime: the agent", w=180)
    p.node("gateway", "agentcore", 3, 1, "AgentCore Gateway: one target per kit", w=200)
    group(p, "platform", 340, "Kits 1 to 4: the platform's own account", OVERVIEWS[:4])
    group(p, "connected", 1120, "Kits 5 to 8: customers' connected accounts", OVERVIEWS[4:])
    p.node("api", "apigw", 1, 3, "Approval application programming interface (API)")
    p.node("sfn", "sfn", 2, 3, "AWS Step Functions: one workflow per kit action", lw=160)
    p.node("ddb", "ddb", 0, 4, "Amazon DynamoDB: proposals, decisions, ledger, operations, audit")
    p.node("adapter", "lambda", 1, 4, "Mutation adapter (AWS Lambda): the only Airwallex writer")
    p.node("awx", "ext", 2, 4, "Airwallex sandbox", w=200, h=60)
    p.node("reader", "lambda", 3, 4, "Reader Lambda per kit: GET only")
    p.node("secret", "secrets", 4, 4, "AWS Secrets Manager: Airwallex credentials", lw=180)

    # People and the agent
    p.wire("user", "cognito", "signs in", ("R", 0.5), ("L", 0.5), prefer=[0])
    p.wire(
        "user",
        "runtime",
        "chats",
        ("R", 0.85),
        ("L", 0.1),
        [(290, 209.6), (290, 386.4)],
        prefer=[1],
    )
    p.wire("runtime", "model", "invokes", ("L", 0.45), ("R", 23.8 / 56), prefer=[0])
    p.wire("runtime", "gateway", "calls", ("R", 0.5), ("L", 0.5), prefer=[0])
    p.wire(
        "gateway",
        "platform",
        "calls tools",
        ("B", 0.2),
        ("T", 14 / 15),
        [(980, 500), (600, 500)],
        prefer=[0],
    )
    p.wire(
        "gateway",
        "connected",
        "calls tools",
        ("B", 0.8),
        ("T", 0.28 / 0.6),
        prefer=[(0, 0.2)],
    )
    p.wire(
        "user",
        "api",
        "approves or rejects",
        ("T", 0.5),
        ("T", 0.5),
        [(150, 120), (690, 120), (690, 950), (430, 950)],
        color=APPROVE,
        prefer=[1],
    )
    p.wire(
        "op",
        "sfn",
        "starts",
        ("L", 0.5),
        ("T", 0.8),
        [(790, 190), (790, 968), (746.8, 968)],
        dashed=True,
        prefer=[(0, 0.2)],
    )
    # Proposals, approvals and state
    p.wire(
        "platform",
        "sfn",
        "proposes",
        ("B", 0.9),
        ("T", 0.25),
        [(580, 975), (716, 975)],
        prefer=[(0, 0.2)],
    )
    p.wire("connected", "sfn", "proposes", ("B", 0.1), ("R", 0.75), [(880, 1032)], prefer=[0])
    p.wire("api", "sfn", "resumes", ("R", 0.2), ("L", 0.2), color=APPROVE, prefer=[0])
    p.wire(
        "api",
        "ddb",
        "records decisions",
        ("L", 0.5),
        ("T", 0.25),
        [(136, 1018)],
        color=APPROVE,
        prefer=[1],
    )
    p.wire(
        "sfn",
        "ddb",
        "records proposals and the ledger",
        ("L", 0.55),
        ("T", 0.75),
        [(592, 1020.8), (592, 1150), (164, 1150)],
        prefer=[2],
    )
    p.wire(
        "sfn",
        "adapter",
        "sends",
        ("L", 0.95),
        ("T", 0.25),
        [(620, 1043.2), (620, 1178), (416, 1178)],
        color=WRITE,
        prefer=[2],
    )
    # Writes, and the reads through each kit's reader
    p.wire("adapter", "ddb", "records each operation", ("L", 0.5), ("R", 0.5), color=WRITE)
    p.wire("adapter", "awx", "writes", ("R", 0.5), ("L", 0.5), color=WRITE, prefer=[0])
    p.wire(
        "adapter",
        "secret",
        "reads",
        ("R", 0.85),
        ("R", 0.5),
        [(560, 1249.6), (560, 1345), (1410, 1345), (1410, 1230)],
        prefer=[(2, 0.07)],
    )
    p.wire(
        "platform",
        "reader",
        "reads",
        ("B", 59 / 60),
        ("T", 0.5),
        [(630, 890), (1040, 890)],
        prefer=[(1, 110 / 410)],
    )
    p.wire("connected", "reader", "", ("B", 245.2 / 600), ("T", 0.95))
    p.wire(
        "sfn",
        "reader",
        "reads",
        ("R", 0.25),
        ("T", 0.05),
        [(1014.8, 1004)],
        prefer=[(0, 57 / 256.8)],
    )
    p.wire("reader", "awx", "reads (GET only)", ("L", 0.5), ("R", 0.5), prefer=[0])
    p.wire("reader", "secret", "reads", ("R", 0.5), ("L", 0.5), prefer=[0])
    bounds = [(64, 236), (308, 216), (532, 382), (922, 200), (1130, 240)]
    keep_clear(p, ALL_KITS_LANES, bounds)
    n0 = len(p.label_rects)
    p.place_labels()
    p.check()
    check_overview(p, ALL_KITS_LANES, bounds, p.label_rects[n0:])
    overview_frame(p, ALL_KITS_LANES, bounds)
    return p


# ------------------------------------------------------------------ main


def pages() -> list[Page]:
    out = [stacks_page(), spine_page(), kit_workflow_page()]
    out += [kit_page(k) for k in KITS]
    for p in out:
        p.place_labels()
        p.check()
    # The overview pages place their labels and check themselves before their frame is drawn.
    return out + [overview_page(o) for o in OVERVIEWS] + [all_kits_page()]


def render(drawio: str, src: Path) -> None:
    out = src.with_suffix(".png")
    cmd = [
        drawio,
        "--no-sandbox",
        "--disable-gpu",
        "-x",
        "-f",
        "png",
        "-s",
        "2",
        "-b",
        "20",
        "-o",
        str(out),
        str(src),
    ]
    if not os.environ.get("DISPLAY") and shutil.which("xvfb-run"):
        cmd = ["xvfb-run", "-a", *cmd]
    subprocess.run(  # noqa: S603 - fixed argv, the drawio path is the operator's own
        cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180
    )
    if not out.exists():
        raise SystemExit(f"draw.io did not write {out}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--drawio", help="path to the draw.io desktop executable; renders PNGs")
    ap.add_argument("--only", nargs="*", help="diagram names to write (default: all)")
    args = ap.parse_args(argv)
    for p in pages():
        if args.only and p.slug not in args.only:
            continue
        path = HERE / f"{p.slug}.drawio"
        path.parent.mkdir(exist_ok=True)
        path.write_text(p.xml(), encoding="utf-8")
        if args.drawio:
            render(args.drawio, path)
        print(path.relative_to(HERE))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
