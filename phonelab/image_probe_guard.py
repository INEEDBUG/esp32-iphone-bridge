"""Fail-closed pixel guard for one synthetic PhoneLab button.

This validates image geometry only. It does not establish device unlock,
trusted input, a click, or any other input effect.
"""
from __future__ import annotations

from collections import deque
import hashlib
import math
import re

from .synthetic_vision import _srgb_image, detect_pointer
from .visual_marker import decode_visual_marker


_SCREEN = (828, 1792)
_PROBE_ID = re.compile(r"^(0[1-9]|1[0-9]|2[0-5])$")
_LABEL = re.compile(r"^Probe (0[1-9]|1[0-9]|2[0-5]) \(([0-9]{1,6})\)$")
_BUTTON_FILL = (237, 244, 255)  # Synthetic page CSS #edf4ff after sRGB conversion.


def _ocr_target(probe_id, fields):
    if not isinstance(fields, dict) or fields.get("status") not in {"OK", "PARTIAL"}:
        raise ValueError("target OCR unavailable")
    rows = fields.get("visible_probes")
    if not isinstance(rows, list):
        raise ValueError("target OCR unavailable")
    matches = [row for row in rows if isinstance(row, dict) and row.get("id") == probe_id]
    if len(matches) != 1:
        raise ValueError("target OCR missing or ambiguous")
    row = matches[0]
    count, label, box = row.get("count"), row.get("image_text"), row.get("image_text_bounds")
    if type(count) is not int or not 0 <= count <= 999999:
        raise ValueError("target OCR count invalid")
    # Production OCR returns sanitized id/count/bounds. If a caller supplies
    # actual recognized text too, cross-check it; never synthesize that field.
    if label is not None:
        if not isinstance(label, str):
            raise ValueError("target OCR label invalid")
        match = _LABEL.fullmatch(label)
        if not match or match.group(1) != probe_id or int(match.group(2)) != count:
            raise ValueError("target OCR label mismatch")
    if not isinstance(box, dict):
        raise ValueError("target text bounds unavailable")
    values = [box.get(key) for key in ("x", "y", "width", "height")]
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
        raise ValueError("target text bounds invalid")
    x, y, width, height = map(float, values)
    if (x < 0 or y < 0 or width < 24 or height < 8 or x + width > _SCREEN[0]
            or y + height > _SCREEN[1]):
        raise ValueError("target text bounds outside image")
    return count, (x, y, width, height)



def _blue_mask(image):
    width, height = image.size
    pixels = image.load()
    mask = bytearray(width * height)
    for y in range(height):
        base = y * width
        for x in range(width):
            r, g, b = pixels[x, y]
            if 24 <= r <= 62 and 76 <= g <= 112 and 170 <= b <= 218:
                mask[base + x] = 1
    return mask


def _line_runs(values, minimum=3):
    runs = []
    start = None
    previous = None
    for value in values:
        if start is None or value > previous + 1:
            if start is not None and previous - start + 1 >= minimum:
                runs.append((start, previous + 1))
            start = value
        previous = value
    if start is not None and previous - start + 1 >= minimum:
        runs.append((start, previous + 1))
    return runs


def _blue_button_rectangles(image, text_bounds, pointer):
    """Recover closed outlines, allowing only a gap covered by the cursor disk."""
    width, height = image.size
    mask = _blue_mask(image)
    text_x, text_y, text_w, text_h = text_bounds
    y0 = max(0, int(text_y - 180))
    y1 = min(height, int(text_y + text_h + 181))
    radius = float(pointer.diameter) / 2.0 + 2.0
    cursor_x, cursor_y = float(pointer.center_x), float(pointer.center_y)

    def row_runs(y):
        base = y * width
        xs = [x for x in range(width) if mask[base + x]]
        near_cursor = abs(y - cursor_y) <= radius
        # Keep shorter fragments only while testing whether this row's gap is
        # fully explained by the verified cursor. They cannot stand alone.
        runs = _line_runs(xs, minimum=3 if near_cursor else 80)
        if not near_cursor:
            return runs
        if len(runs) < 2:
            return [run for run in runs if run[1] - run[0] >= 80]
        merged = []
        for run in runs:
            if merged:
                left, right = merged[-1]
                gap = run[0] - right
                gap_pixels = range(right, run[0])
                covered_by_cursor = all(
                    abs((x + 0.5) - cursor_x) <= radius
                    for x in gap_pixels)
                span = run[1] - left
                external = (right - left) + (run[1] - run[0])
                if 0 < gap <= 35 and covered_by_cursor and external / span >= 0.70:
                    merged[-1] = (left, run[1])
                    continue
            merged.append(run)
        return [run for run in merged if run[1] - run[0] >= 80]

    rows = {}
    for y in range(y0, y1):
        runs = row_runs(y)
        if runs:
            rows[y] = runs
    columns = {}
    x0 = max(0, int(text_x - 760))
    x1 = min(width, int(text_x + text_w + 760))
    for x in range(x0, x1):
        runs = _line_runs([y for y in range(y0, y1) if mask[y * width + x]], minimum=60)
        if runs:
            columns[x] = runs

    def edge_groups(mapping):
        groups = []
        for value in sorted(mapping):
            if not groups or value > groups[-1][-1] + 3:
                groups.append([value])
            else:
                groups[-1].append(value)
        return groups

    vertical_edges = []
    for group in edge_groups(columns):
        x = round(sum(group) / len(group))
        segments = [seg for col in group for seg in columns[col]]
        vertical_edges.append((x, segments))
    candidates = set()
    top_rows = [y for y in rows if text_y - 180 <= y <= text_y - 5]
    bottom_rows = [y for y in rows if text_y + text_h + 5 <= y <= text_y + text_h + 180]
    for top in top_rows:
        for bottom in bottom_rows:
            span = bottom - top
            if not 60 <= span <= 180:
                continue
            core_top, core_bottom = top + 10, bottom - 10
            if core_bottom <= core_top:
                continue
            side_x = []
            for edge_x, segments in vertical_edges:
                overlap = max((min(end, core_bottom) - max(start, core_top)
                               for start, end in segments), default=0)
                if overlap >= (core_bottom - core_top) * 0.72:
                    side_x.append(edge_x)
            for left in side_x:
                for right in side_x:
                    box_w = right - left + 1
                    if not 80 <= box_w <= 760:
                        continue
                    supported = 0.70 * box_w
                    if not any(max(0, min(b, right + 1) - max(a, left)) >= supported
                               for a, b in rows[top]):
                        continue
                    if not any(max(0, min(b, right + 1) - max(a, left)) >= supported
                               for a, b in rows[bottom]):
                        continue
                    candidates.add((left, top, right + 1, bottom + 1))
    # Multiple adjacent rows/columns describe the same 1-2px outline. Collapse
    # only near-identical edge estimates; genuinely nested buttons stay distinct.
    clusters = []
    for box in sorted(candidates):
        for cluster in clusters:
            if all(abs(box[i] - cluster[0][i]) <= 5 for i in range(4)):
                cluster.append(box)
                break
        else:
            clusters.append([box])
    return [tuple(round(sum(box[i] for box in cluster) / len(cluster))
                  for i in range(4)) for cluster in clusters]


def _blue_button_components(image):
    """Find connected blue border components; each candidate must form a box."""
    width, height = image.size
    mask = _blue_mask(image)
    seen = bytearray(width * height)
    components = []
    for start, active in enumerate(mask):
        if not active or seen[start]:
            continue
        queue = deque([start])
        seen[start] = 1
        min_x = max_x = start % width
        min_y = max_y = start // width
        points = []
        while queue:
            index = queue.popleft()
            x, y = index % width, index // width
            points.append((x, y))
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
            for dy in (-1, 0, 1):
                ny = y + dy
                if not 0 <= ny < height:
                    continue
                row = ny * width
                for dx in (-1, 0, 1):
                    nx = x + dx
                    neighbor = row + nx
                    if (0 <= nx < width and mask[neighbor] and not seen[neighbor]):
                        seen[neighbor] = 1
                        queue.append(neighbor)
        box_w, box_h = max_x - min_x + 1, max_y - min_y + 1
        if 80 <= box_w <= 760 and 60 <= box_h <= 180:
            # Reject isolated blue text or partial borders: each side needs
            # sustained border pixels, with rounded corners allowed to be open.
            top = sum(1 for x, y in points if y <= min_y + 3)
            bottom = sum(1 for x, y in points if y >= max_y - 3)
            left = sum(1 for x, y in points if x <= min_x + 3)
            right = sum(1 for x, y in points if x >= max_x - 3)
            if min(top, bottom) >= box_w * 0.35 and min(left, right) >= box_h * 0.35:
                components.append((min_x, min_y, max_x + 1, max_y + 1))
    return components


def _target_fill_box(image, text_bounds):
    """Find one bounded button-fill component seeded inside the OCR label box."""
    x, y, text_width, text_height = text_bounds
    if text_width > 320 or text_height > 50:
        return None
    width, height = image.size
    pixels = image.load()

    def is_fill(px, py):
        color = pixels[px, py]
        return all(abs(int(color[channel]) - _BUTTON_FILL[channel]) <= 3
                   for channel in range(3))

    left = max(0, int(math.floor(x)))
    top = max(0, int(math.floor(y)))
    right = min(width, int(math.ceil(x + text_width)))
    bottom = min(height, int(math.ceil(y + text_height)))
    seeds = [(sx, sy) for sy in range(top, bottom) for sx in range(left, right)
             if is_fill(sx, sy)]
    if not seeds:
        return None

    visited = bytearray(width * height)
    components = []
    max_component_pixels = 760 * 180
    for sx, sy in seeds:
        start = sy * width + sx
        if visited[start]:
            continue
        queue = deque([start])
        visited[start] = 1
        min_x = max_x = sx
        min_y = max_y = sy
        points = 0
        too_large = False
        while queue:
            index = queue.popleft()
            px, py = index % width, index // width
            points += 1
            min_x, max_x = min(min_x, px), max(max_x, px)
            min_y, max_y = min(min_y, py), max(max_y, py)
            if (max_x - min_x + 1 > 760 or max_y - min_y + 1 > 180
                    or points > max_component_pixels):
                too_large = True
                break
            for dy in (-1, 0, 1):
                ny = py + dy
                if not 0 <= ny < height:
                    continue
                row = ny * width
                for dx in (-1, 0, 1):
                    nx = px + dx
                    if not 0 <= nx < width:
                        continue
                    neighbor = row + nx
                    if not visited[neighbor] and is_fill(nx, ny):
                        visited[neighbor] = 1
                        queue.append(neighbor)
        if too_large:
            return None
        box_width, box_height = max_x - min_x + 1, max_y - min_y + 1
        area = box_width * box_height
        if (box_width < 70 or box_height < 50 or points < 2500
                or points / area < 0.65):
            continue
        # The image-text ROI must be fully inside this fill region's bounds;
        # letter strokes themselves may of course be dark holes in the region.
        if (x < min_x or y < min_y or x + text_width > max_x + 1
                or y + text_height > max_y + 1):
            continue
        components.append((min_x, min_y, max_x + 1, max_y + 1))
        if len(components) > 1:
            return None
    return components[0] if len(components) == 1 else None


def _outline_matches_fill(outline, fill_box):
    """Require every inferred outline edge to closely surround the fill ROI."""
    left, top, right, bottom = outline
    fill_left, fill_top, fill_right, fill_bottom = fill_box
    margins = (fill_left - left, fill_top - top, right - fill_right, bottom - fill_bottom)
    return all(1 <= margin <= 8 for margin in margins)


def _find_button(image, text_bounds, pointer):
    x, y, width, height = text_bounds
    component_candidates = []
    for left, top, right, bottom in _blue_button_components(image):
        if (left + 5 <= x and top + 5 <= y and right - 5 >= x + width
                and bottom - 5 >= y + height):
            component_candidates.append((left, top, right, bottom))
    candidates = component_candidates
    if len(component_candidates) != 1:
        # Border components can merge where adjacent buttons nearly touch.
        # Re-detect closed outlines from independent row/column runs.
        candidates = [box for box in _blue_button_rectangles(image, text_bounds, pointer)
                      if box[0] + 5 <= x and box[1] + 5 <= y
                      and box[2] - 5 >= x + width and box[3] - 5 >= y + height]
    # The lab's CSS centers button labels; reject adjacent outline hypotheses
    # whose center is inconsistent with the OCR text center.
    text_center = x + width / 2.0
    candidates = [box for box in candidates
                  if abs((box[0] + box[2]) / 2.0 - text_center) <= 6.0]
    if len(candidates) != 1:
        # Only the merged-component case has evidence suitable for this second,
        # independent geometry channel. Multiple complete border components
        # stay ambiguous (including genuinely nested candidate buttons).
        fill_box = _target_fill_box(image, text_bounds) if not component_candidates else None
        if fill_box is not None:
            candidates = [box for box in candidates if _outline_matches_fill(box, fill_box)]
    if len(candidates) != 1:
        raise ValueError("button border missing or ambiguous")
    left, top, right, bottom = candidates[0]
    if not (35 < (left + right) / 2 < 793 and 180 < (top + bottom) / 2 < 1450):
        raise ValueError("button is outside the safe visible region")
    return candidates[0]


def _label_signature(image, bounds):
    x, y, width, height = bounds
    # Hash the full OCR text ROI, including its visible count, to detect any
    # label change or occlusion between reference and current frames.
    left = max(0, int(math.floor(x)) - 2)
    top = max(0, int(math.floor(y)) - 2)
    right = min(image.width, int(math.ceil(x + width)) + 2)
    bottom = min(image.height, int(math.ceil(y + height)) + 2)
    if right <= left or bottom <= top:
        raise ValueError("label ROI invalid")
    return hashlib.sha256(image.crop((left, top, right, bottom)).tobytes()).hexdigest()


class ImageProbeGuard:
    """Bind a current, unique pointer candidate to one OCR-labeled page button."""

    def __init__(self, probe_id, reference_png, nonce, ocr_fields):
        if not isinstance(probe_id, str) or not _PROBE_ID.fullmatch(probe_id):
            raise ValueError("invalid synthetic probe id")
        try:
            marker = decode_visual_marker(reference_png, nonce)
        except Exception:
            raise ValueError("reference run marker invalid") from None
        image = _srgb_image(reference_png)
        if image is None or image.size != _SCREEN:
            raise ValueError("reference image invalid")
        count, text_bounds = _ocr_target(probe_id, ocr_fields)
        pointer = detect_pointer(reference_png, verified_marker=marker)
        if pointer is None:
            raise ValueError("reference pointer is missing or ambiguous")
        button = _find_button(image, text_bounds, pointer)
        goal = ((text_bounds[0] + text_bounds[2] / 2.0), text_bounds[1] + text_bounds[3] + 22.0)
        if not (35 < goal[0] < 793 and 180 < goal[1] < 1450):
            raise ValueError("click goal outside safe visible region")
        self.probe_id = probe_id
        self.nonce = nonce
        self._marker = marker
        self._reference_pointer = pointer
        self._button = button
        self._count = count
        self._text_bounds = text_bounds
        self._goal = goal
        self._label_signature = _label_signature(image, text_bounds)

    def verify(self, png):
        try:
            marker = decode_visual_marker(png, self.nonce)
        except Exception:
            raise ValueError("current run marker invalid") from None
        image = _srgb_image(png)
        if image is None or image.size != _SCREEN:
            raise ValueError("current image invalid")
        pointer = detect_pointer(png, verified_marker=marker)
        if pointer is None:
            raise ValueError("pointer is missing or ambiguous")
        button = _find_button(image, self._text_bounds, pointer)
        if button != self._button:
            raise ValueError("target button moved or changed")
        if _label_signature(image, self._text_bounds) != self._label_signature:
            raise ValueError("target label changed or is occluded")
        error = math.hypot(pointer.center_x - self._goal[0], pointer.center_y - self._goal[1])
        left, top, right, bottom = button
        if (error > 10 or not (left + 6 <= pointer.center_x <= right - 6
                               and top + 6 <= pointer.center_y <= bottom - 6)):
            raise ValueError("pointer is not safely aligned inside target button")
        x, y, width, height = self._text_bounds
        if (x - 6 <= pointer.center_x <= x + width + 6
                and y - 6 <= pointer.center_y <= y + height + 6):
            raise ValueError("pointer overlaps target label")
        return {"probe_id": self.probe_id, "count": self._count,
                "pointer_xy": (pointer.center_x, pointer.center_y), "goal_xy": self._goal,
                "pixel_error": error, "button_bounds": self._button,
                "lock_state": "UNKNOWN", "input_effect_verified": False}
