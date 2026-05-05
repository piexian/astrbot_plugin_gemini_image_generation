"""自适应黑描边表情包切分器。

核心思路：
1. 从白底图中提取原始前景与结构前景。
2. 对大粘连连通域使用黑描边感知的 RAG 分裂。
3. 对紧邻的双角色局部再做一次 watershed 重分配，避免头饰/发丝误切。
4. 输出带透明通道的裁剪图。
"""

from __future__ import annotations

import heapq

import cv2
import numpy as np
from scipy.ndimage import distance_transform_edt, maximum_filter
from skimage import filters, graph
from skimage.segmentation import slic, watershed


class AdaptiveStickerSplitter:
    """针对白底动漫表情包的自适应分割器。"""

    def __init__(self) -> None:
        self.last_panel_labels: np.ndarray | None = None
        self.last_expanded_labels: np.ndarray | None = None

    @staticmethod
    def _ensure_bgr(image: np.ndarray) -> np.ndarray:
        if image.ndim == 2:
            return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        if image.shape[2] == 4:
            return cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
        return image.copy()

    @staticmethod
    def _build_debug_image(image: np.ndarray, labels: np.ndarray) -> np.ndarray:
        n_panels = int(labels.max())
        if n_panels == 0:
            return image.copy()

        vis = np.zeros_like(image)
        palette = np.array(
            [
                [255, 99, 132],
                [54, 162, 235],
                [255, 206, 86],
                [75, 192, 192],
                [153, 102, 255],
                [255, 159, 64],
                [199, 199, 199],
                [83, 102, 255],
            ],
            dtype=np.uint8,
        )
        for li in range(1, n_panels + 1):
            color = palette[(li - 1) % len(palette)].tolist()
            vis[labels == li] = color[::-1]
        return cv2.addWeighted(image, 0.4, vis, 0.6, 0)

    def _make_fg_raw(self, image: np.ndarray) -> np.ndarray:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape

        bg_cand = (gray >= 245).astype(np.uint8) * 255
        flood = bg_cand.copy()
        mask = np.zeros((h + 2, w + 2), dtype=np.uint8)
        for y in [0, h - 1]:
            for x in range(w):
                if flood[y, x] == 255:
                    cv2.floodFill(flood, mask, (x, y), 128)
        for x in [0, w - 1]:
            for y in range(h):
                if flood[y, x] == 255:
                    cv2.floodFill(flood, mask, (x, y), 128)

        bg = (flood == 128).astype(np.uint8) * 255
        fg_raw = cv2.bitwise_not(bg)
        kern3 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        fg_raw = cv2.morphologyEx(fg_raw, cv2.MORPH_OPEN, kern3, iterations=1)
        return fg_raw

    @staticmethod
    def _make_fg_detail(image: np.ndarray) -> np.ndarray:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape

        bg_cand = (gray >= 245).astype(np.uint8) * 255
        flood = bg_cand.copy()
        mask = np.zeros((h + 2, w + 2), dtype=np.uint8)
        for y in [0, h - 1]:
            for x in range(w):
                if flood[y, x] == 255:
                    cv2.floodFill(flood, mask, (x, y), 128)
        for x in [0, w - 1]:
            for y in range(h):
                if flood[y, x] == 255:
                    cv2.floodFill(flood, mask, (x, y), 128)

        bg = (flood == 128).astype(np.uint8) * 255
        fg_detail = cv2.bitwise_not(bg)
        n_cc, cc_labels, stats, _ = cv2.connectedComponentsWithStats(
            (fg_detail > 0).astype(np.uint8),
            8,
        )
        cleaned = np.zeros_like(fg_detail)
        for cc_id in range(1, n_cc):
            area = int(stats[cc_id, cv2.CC_STAT_AREA])
            if area >= 2:
                cleaned[cc_labels == cc_id] = 255
        return cleaned

    @staticmethod
    def _make_fg_struct(fg_raw: np.ndarray) -> np.ndarray:
        n_cc, cc_labels = cv2.connectedComponents(fg_raw)
        h, w = fg_raw.shape

        cc_areas: dict[int, int] = {}
        for cc_id in range(1, n_cc):
            area = int(np.count_nonzero(cc_labels == cc_id))
            if area >= 10:
                cc_areas[cc_id] = area

        if not cc_areas:
            return fg_raw.copy()

        areas_sorted = sorted(cc_areas.values())
        if len(areas_sorted) >= 2:
            gaps = [
                (areas_sorted[i + 1] / max(areas_sorted[i], 1), i)
                for i in range(len(areas_sorted) - 1)
            ]
            max_gap_ratio, max_gap_idx = max(gaps)
            if max_gap_ratio >= 2.0:
                threshold = (
                    areas_sorted[max_gap_idx] + areas_sorted[max_gap_idx + 1]
                ) / 2
            else:
                threshold = h * w * 0.01
        else:
            threshold = 0

        threshold = max(threshold, max(areas_sorted) * 0.02)
        fg_struct = np.zeros_like(fg_raw)
        for cc_id, area in cc_areas.items():
            if area >= threshold:
                fg_struct[cc_labels == cc_id] = 255

        kern = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        fg_struct = cv2.morphologyEx(fg_struct, cv2.MORPH_CLOSE, kern, iterations=1)
        return fg_struct

    @staticmethod
    def _nms(coords: np.ndarray, min_dist: int) -> list[tuple[int, int]]:
        if len(coords) == 0:
            return []

        kept: list[tuple[int, int]] = []
        cell_size = max(1, int(min_dist))
        buckets: dict[tuple[int, int], list[tuple[int, int]]] = {}
        min_dist_sq = float(min_dist * min_dist)
        for y, x in coords:
            yi = int(y)
            xi = int(x)
            cy = yi // cell_size
            cx = xi // cell_size
            too_close = False
            for ny in range(cy - 1, cy + 2):
                for nx in range(cx - 1, cx + 2):
                    for ky, kx in buckets.get((ny, nx), []):
                        dy = yi - ky
                        dx = xi - kx
                        if float(dy * dy + dx * dx) < min_dist_sq:
                            too_close = True
                            break
                    if too_close:
                        break
                if too_close:
                    break
            if not too_close:
                point = (yi, xi)
                kept.append(point)
                buckets.setdefault((cy, cx), []).append(point)
        return kept

    def _auto_peaks(
        self, dist: np.ndarray, cc_mask: np.ndarray
    ) -> list[tuple[int, int]]:
        h, w = dist.shape[:2]
        dt_max = float(dist.max())
        if dt_max < 3.0:
            return []

        dt_thresh = max(5.0, dt_max * 0.15)
        local_max = maximum_filter(dist, size=max(5, int(min(h, w) * 0.03)))
        all_peaks_mask = (dist == local_max) & (dist > dt_thresh) & (cc_mask > 0)
        n_cc, cc_labels = cv2.connectedComponents(
            all_peaks_mask.astype(np.uint8),
            connectivity=8,
        )
        peak_coords: list[tuple[int, int, float]] = []
        for cc_id in range(1, n_cc):
            ys, xs = np.where(cc_labels == cc_id)
            if len(ys) == 0:
                continue
            if len(ys) == 1:
                py = int(ys[0])
                px = int(xs[0])
            else:
                cy = float(np.mean(ys))
                cx = float(np.mean(xs))
                dist_to_center = (ys - cy) ** 2 + (xs - cx) ** 2
                best_idx = int(np.argmin(dist_to_center))
                py = int(ys[best_idx])
                px = int(xs[best_idx])
            peak_coords.append((py, px, float(dist[py, px])))

        all_coords = np.array(
            [
                (py, px)
                for py, px, _ in sorted(
                    peak_coords, key=lambda item: item[2], reverse=True
                )
            ],
            dtype=np.int32,
        )
        if len(all_coords) == 0:
            return []

        lo, hi = 20, int(min(h, w) * 0.45)
        step = max(3, (hi - lo) // 30)
        count_history: list[tuple[int, int, list[tuple[int, int]]]] = []
        for md in range(lo, hi + 1, step):
            peaks = self._nms(all_coords, md)
            if peaks:
                count_history.append((md, len(peaks), peaks))
        if not count_history:
            return self._nms(all_coords, lo)

        counts = [n for _, n, _ in count_history]
        best_count = int(np.median(counts))
        for _, n, peaks in count_history:
            if n == best_count:
                return peaks
        return self._nms(all_coords, lo)

    @staticmethod
    def _component_peaks(
        mask: np.ndarray, peaks: list[tuple[int, int]]
    ) -> list[tuple[int, int]]:
        return [(py, px) for py, px in peaks if mask[py, px]]

    @staticmethod
    def _largest_peak_component(
        mask: np.ndarray, peaks: list[tuple[int, int]]
    ) -> np.ndarray:
        binary = (mask > 0).astype(np.uint8)
        n_cc, cc_labels, stats, _ = cv2.connectedComponentsWithStats(binary, 8)
        best_mask = None
        best_area = 0
        for cc_id in range(1, n_cc):
            comp = cc_labels == cc_id
            if not any(comp[py, px] for py, px in peaks):
                continue
            area = int(stats[cc_id, cv2.CC_STAT_AREA])
            if area > best_area:
                best_area = area
                best_mask = comp
        return best_mask if best_mask is not None else (mask > 0)

    @staticmethod
    def _dijkstra_split(
        cc_mask: np.ndarray,
        peaks: list[tuple[int, int]],
        dt_max: float,
    ) -> np.ndarray:
        h, w = cc_mask.shape[:2]
        dist = cv2.distanceTransform(cc_mask, cv2.DIST_L2, 5)
        result = np.zeros((h, w), dtype=np.int32)
        cost = np.full((h, w), np.inf, dtype=np.float64)
        pq: list[tuple[float, int, int, int]] = []

        for i, (py, px) in enumerate(peaks):
            li = i + 1
            result[py, px] = li
            cost[py, px] = 0.0
            heapq.heappush(pq, (0.0, py, px, li))

        neighbors = [
            (-1, 0),
            (1, 0),
            (0, -1),
            (0, 1),
            (-1, -1),
            (-1, 1),
            (1, -1),
            (1, 1),
        ]

        while pq:
            current_cost, y, x, li = heapq.heappop(pq)
            if current_cost > cost[y, x]:
                continue
            for dy, dx in neighbors:
                ny, nx = y + dy, x + dx
                if 0 <= ny < h and 0 <= nx < w and cc_mask[ny, nx] > 0:
                    edge = max(0.1, dt_max - float(dist[ny, nx]))
                    new_cost = current_cost + edge
                    if new_cost < cost[ny, nx]:
                        cost[ny, nx] = new_cost
                        result[ny, nx] = li
                        heapq.heappush(pq, (new_cost, ny, nx, li))

        return result

    def _recursive_open(
        self,
        cc_mask: np.ndarray,
        peaks: list[tuple[int, int]],
    ) -> dict[int, np.ndarray]:
        h, w = cc_mask.shape[:2]
        binary = (cc_mask > 0).astype(np.uint8)
        if len(peaks) <= 1:
            return {1: binary > 0}

        for k in range(3, min(51, min(h, w) // 4), 2):
            kern = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
            opened = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kern, iterations=1)
            n_cc, labels = cv2.connectedComponents(opened)
            sig_ccs = {j for j in range(1, n_cc) if int((labels == j).sum()) > 50}
            if len(sig_ccs) < 2:
                continue

            seeds: dict[int, np.ndarray] = {}
            next_li = 1
            for j in sig_ccs:
                sub_mask = (labels == j).astype(np.uint8) * 255
                sub_peaks = [(py, px) for py, px in peaks if sub_mask[py, px] > 0]
                if not sub_peaks:
                    continue
                if len(sub_peaks) == 1:
                    seeds[next_li] = sub_mask > 0
                    next_li += 1
                else:
                    sub_seeds = self._recursive_open(sub_mask, sub_peaks)
                    for smask in sub_seeds.values():
                        seeds[next_li] = smask
                        next_li += 1
            return seeds

        return {1: binary > 0}

    def _fluid_split(
        self, cc_mask: np.ndarray, peaks: list[tuple[int, int]]
    ) -> np.ndarray:
        h, w = cc_mask.shape[:2]
        dist = cv2.distanceTransform(cc_mask, cv2.DIST_L2, 5)
        dt_max = float(dist.max())
        seeds = self._recursive_open(cc_mask, peaks)

        needs_split: dict[int, list[tuple[int, int]]] = {}
        for li, mask in list(seeds.items()):
            peaks_in = [(py, px) for py, px in peaks if mask[py, px]]
            if len(peaks_in) > 1:
                needs_split[li] = peaks_in

        for li, sub_peaks in needs_split.items():
            mask = seeds.pop(li)
            sub_mask = mask.astype(np.uint8) * 255
            dijk_labels = self._dijkstra_split(sub_mask, sub_peaks, dt_max)
            base_li = max(seeds.keys()) + 1 if seeds else 1
            for dli in range(1, int(dijk_labels.max()) + 1):
                seeds[base_li] = dijk_labels == dli
                base_li += 1

        for li, mask in list(seeds.items()):
            seed_peaks = self._component_peaks(mask, peaks)
            if seed_peaks:
                seeds[li] = self._largest_peak_component(mask, seed_peaks)

        result = np.zeros((h, w), dtype=np.int32)
        for li, mask in seeds.items():
            result[mask] = li

        kern3 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        while True:
            prev = result.copy()
            claim_count = np.zeros((h, w), dtype=np.int32)
            claim_label = np.zeros((h, w), dtype=np.int32)

            for li in seeds:
                mask_li = (result == li).astype(np.uint8)
                dilated = cv2.dilate(mask_li, kern3, iterations=1)
                new_px = (dilated > 0) & (cc_mask > 0) & (result == 0)
                claim_count[new_px] += 1
                claim_label[new_px] = li

            safe = (claim_count == 1) & (result == 0)
            result[safe] = claim_label[safe]
            if np.array_equal(result, prev):
                break

        unclaimed = (cc_mask > 0) & (result == 0)
        if np.any(unclaimed):
            centroids: dict[int, tuple[float, float]] = {}
            for li in seeds:
                ys, xs = np.where(result == li)
                if len(ys) > 0:
                    centroids[li] = (float(xs.mean()), float(ys.mean()))
            uy, ux = np.where(unclaimed)
            for y, x in zip(uy, ux):
                best_li = min(
                    centroids,
                    key=lambda li: (
                        (x - centroids[li][0]) ** 2 + (y - centroids[li][1]) ** 2
                    ),
                )
                result[y, x] = best_li

        return result

    @staticmethod
    def _grow_graph_labels(rag, seed_regions: dict[int, list[int]]) -> dict[int, int]:
        best: dict[int, float] = {}
        owner: dict[int, int] = {}
        pq: list[tuple[float, int, int]] = []

        for li, segment_ids in seed_regions.items():
            for sid in segment_ids:
                if sid <= 0:
                    continue
                if 0.0 < best.get(sid, float("inf")):
                    best[sid] = 0.0
                    owner[sid] = li
                    heapq.heappush(pq, (0.0, sid, li))

        while pq:
            cost, sid, li = heapq.heappop(pq)
            if owner.get(sid) != li or cost > best.get(sid, float("inf")):
                continue

            for nid in rag.neighbors(sid):
                if nid <= 0:
                    continue
                edge_w = float(rag[sid][nid].get("weight", 0.0))
                new_cost = cost + 1.0 + edge_w * 40.0
                if new_cost < best.get(nid, float("inf")):
                    best[nid] = new_cost
                    owner[nid] = li
                    heapq.heappush(pq, (new_cost, nid, li))

        return owner

    @staticmethod
    def _fill_sparse_holes(
        labels: np.ndarray, cc_mask: np.ndarray, n_labels: int
    ) -> np.ndarray:
        filled = labels.copy()
        kernel = np.ones((3, 3), np.uint8)
        for _ in range(12):
            prev = filled.copy()
            for li in range(1, n_labels + 1):
                dilated = cv2.dilate(
                    (filled == li).astype(np.uint8), kernel, iterations=1
                )
                filled[(dilated > 0) & cc_mask & (filled == 0)] = li
            if np.array_equal(prev, filled):
                break
        return filled

    def _derive_outline_seeds(
        self,
        cc_mask: np.ndarray,
        gray_norm: np.ndarray,
        target_regions: int,
    ) -> tuple[int | None, dict[int, np.ndarray] | None]:
        area = int(np.count_nonzero(cc_mask))
        min_seed_area = max(400, int(area * 0.03))
        best = None

        for thr in [25, 30, 35, 40, 45]:
            dark = (gray_norm <= (thr / 255.0)) & cc_mask
            carved = cc_mask & (~dark)
            n_cc, cc_labels, stats, _ = cv2.connectedComponentsWithStats(
                carved.astype(np.uint8), 8
            )

            components: list[tuple[int, int]] = []
            for cc_id in range(1, n_cc):
                comp_area = int(stats[cc_id, cv2.CC_STAT_AREA])
                if comp_area >= min_seed_area:
                    components.append((comp_area, cc_id))
            components.sort(reverse=True)

            if len(components) < target_regions:
                continue

            seeds: dict[int, np.ndarray] = {}
            for li, (_, cc_id) in enumerate(components[:target_regions], start=1):
                seeds[li] = cc_labels == cc_id

            kept_area = sum(comp_area for comp_area, _ in components[:target_regions])
            extras_penalty = max(0, len(components) - target_regions) * min_seed_area
            score = kept_area - extras_penalty
            if best is None or score > best[0]:
                best = (score, thr, seeds)

        if best is None:
            return None, None
        return best[1], best[2]

    @staticmethod
    def _carve_seed_saddles(
        cc_mask: np.ndarray,
        seeds: dict[int, np.ndarray],
        dist: np.ndarray,
    ) -> np.ndarray:
        if len(seeds) < 2:
            return cc_mask

        result = cc_mask.astype(np.uint8) * 255
        items: list[tuple[int, float, float]] = []
        for li, mask in seeds.items():
            ys, xs = np.where(mask)
            if len(ys) > 0:
                items.append((li, float(xs.mean()), float(ys.mean())))

        items.sort(key=lambda t: t[1])
        for idx in range(len(items) - 1):
            _, x1, _ = items[idx]
            _, x2, _ = items[idx + 1]
            x_lo = max(0, min(int(round(x1)), int(round(x2))) - 24)
            x_hi = min(result.shape[1] - 1, max(int(round(x1)), int(round(x2))) + 24)
            ys: list[tuple[int, int, float]] = []
            for x in range(x_lo, x_hi + 1):
                col = dist[:, x]
                nz = np.where(col > 0)[0]
                if len(nz) == 0:
                    continue
                y = int(nz[np.argmin(col[nz])])
                ys.append((x, y, float(col[y])))
            if not ys:
                continue
            ys.sort(key=lambda t: t[2])
            x_cut, y_cut, width = ys[0]
            if width > 22.0:
                continue
            radius = max(3, min(8, int(round(width * 0.75))))
            cv2.circle(result, (x_cut, y_cut), radius, 0, -1)

        return result > 0

    def _rag_split_cc(
        self,
        rgb_image: np.ndarray,
        gray_norm: np.ndarray,
        cc_mask: np.ndarray,
        target_regions: int,
    ) -> np.ndarray | None:
        outline_thr, seeds = self._derive_outline_seeds(
            cc_mask, gray_norm, target_regions
        )
        if seeds is None or outline_thr is None:
            return None

        dist = cv2.distanceTransform(cc_mask.astype(np.uint8) * 255, cv2.DIST_L2, 5)
        carved_mask = self._carve_seed_saddles(cc_mask, seeds, dist)
        area = int(np.count_nonzero(carved_mask))
        n_segments = int(np.clip(area / 900, 80, 260))
        segments = slic(
            rgb_image,
            n_segments=n_segments,
            compactness=12.0,
            sigma=1.0,
            start_label=1,
            mask=carved_mask,
            convert2lab=True,
        )

        sobel = filters.sobel(gray_norm)
        dark_strength = np.clip((0.20 - gray_norm) / 0.20, 0.0, 1.0)
        edge_map = sobel + 2.8 * dark_strength
        rag = graph.rag_boundary(segments, edge_map, connectivity=2)

        seg_usage: dict[int, list[tuple[int, float, int, int, float]]] = {}
        for li, seed_mask in seeds.items():
            seg_ids = [int(sid) for sid in np.unique(segments[seed_mask]) if sid > 0]
            for sid in seg_ids:
                sid_mask = segments == sid
                overlap = int(np.count_nonzero(sid_mask & seed_mask))
                ys, xs = np.where(sid_mask)
                if len(ys) == 0:
                    continue
                cy, cx = int(np.mean(ys)), int(np.mean(xs))
                center_score = float(dist[cy, cx])
                seg_area = int(len(ys))
                overlap_ratio = overlap / max(seg_area, 1)
                seg_usage.setdefault(sid, []).append(
                    (li, center_score, overlap, seg_area, overlap_ratio)
                )

        exclusive_pool: dict[int, list[tuple[float, float, int, int, int]]] = {
            li: [] for li in seeds
        }
        shared_pool: dict[int, list[tuple[float, float, int, int, int]]] = {
            li: [] for li in seeds
        }
        for sid, items in seg_usage.items():
            if len(items) == 1:
                li, center_score, overlap, seg_area, overlap_ratio = items[0]
                exclusive_pool[li].append(
                    (center_score, overlap_ratio, overlap, seg_area, sid)
                )
                continue

            items_sorted = sorted(
                items,
                key=lambda t: (t[4], t[2], t[1], -t[3]),
                reverse=True,
            )
            li, center_score, overlap, seg_area, overlap_ratio = items_sorted[0]
            shared_pool[li].append(
                (center_score, overlap_ratio, overlap, seg_area, sid)
            )

        seed_regions: dict[int, list[int]] = {}
        for li in seeds:
            exclusive = sorted(exclusive_pool[li], reverse=True)
            chosen = [sid for _, _, _, _, sid in exclusive[:4]]
            if len(chosen) < 2:
                shared = sorted(shared_pool[li], reverse=True)
                for _, _, _, _, sid in shared:
                    if sid not in chosen:
                        chosen.append(sid)
                    if len(chosen) >= 4:
                        break
            if not chosen:
                return None
            seed_regions[li] = chosen

        owner = self._grow_graph_labels(rag, seed_regions)
        labels = np.zeros(cc_mask.shape, dtype=np.int32)
        seg_ids = [int(sid) for sid in np.unique(segments[carved_mask]) if sid > 0]
        for sid in seg_ids:
            li = owner.get(sid, 0)
            if li > 0:
                labels[segments == sid] = li

        labels = self._fill_sparse_holes(labels, carved_mask, target_regions)
        labels = self._fill_sparse_holes(labels, cc_mask, target_regions)

        present = sorted(int(li) for li in np.unique(labels[cc_mask]) if li > 0)
        if len(present) != target_regions:
            return None

        remapped = np.zeros_like(labels)
        for new_li, old_li in enumerate(present, start=1):
            remapped[labels == old_li] = new_li
        return remapped

    def _candidate_touching_pairs(
        self, panel_labels: np.ndarray
    ) -> list[tuple[int, int]]:
        labels = sorted(int(li) for li in np.unique(panel_labels) if li > 0)
        boxes: dict[int, tuple[int, int, int, int, int, int]] = {}
        masks: dict[int, np.ndarray] = {}
        for li in labels:
            mask = panel_labels == li
            ys, xs = np.where(mask)
            if len(ys) == 0:
                continue
            masks[li] = mask
            boxes[li] = (
                int(xs.min()),
                int(ys.min()),
                int(xs.max()),
                int(ys.max()),
                int(xs.max() - xs.min() + 1),
                int(ys.max() - ys.min() + 1),
            )

        pairs: list[tuple[int, int, int]] = []
        kern = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
        dilated = {
            li: cv2.dilate(masks[li].astype(np.uint8), kern, iterations=1) > 0
            for li in masks
        }

        for i, li in enumerate(labels):
            if li not in boxes:
                continue
            x0i, y0i, x1i, y1i, wi, hi = boxes[li]
            for lj in labels[i + 1 :]:
                if lj not in boxes:
                    continue
                x0j, y0j, x1j, y1j, wj, hj = boxes[lj]
                y_overlap = max(0, min(y1i, y1j) - max(y0i, y0j))
                min_h = min(hi, hj)
                x_gap = max(0, max(x0i, x0j) - min(x1i, x1j))
                touching = np.any(dilated[li] & dilated[lj])
                if y_overlap < min_h * 0.35:
                    continue
                if x_gap > max(40, int(min(wi, wj) * 0.28)):
                    continue
                if not touching:
                    continue
                pairs.append((x_gap, li, lj))

        pairs.sort()
        return [(li, lj) for _, li, lj in pairs]

    def _candidate_vertical_touching_pairs(
        self,
        panel_labels: np.ndarray,
    ) -> list[tuple[int, int]]:
        labels = sorted(int(li) for li in np.unique(panel_labels) if li > 0)
        boxes: dict[int, tuple[int, int, int, int, int, int]] = {}
        masks: dict[int, np.ndarray] = {}
        for li in labels:
            mask = panel_labels == li
            ys, xs = np.where(mask)
            if len(ys) == 0:
                continue
            masks[li] = mask
            boxes[li] = (
                int(xs.min()),
                int(ys.min()),
                int(xs.max()),
                int(ys.max()),
                int(xs.max() - xs.min() + 1),
                int(ys.max() - ys.min() + 1),
            )

        pairs: list[tuple[int, int, int]] = []
        kern = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
        dilated = {
            li: cv2.dilate(masks[li].astype(np.uint8), kern, iterations=1) > 0
            for li in masks
        }

        for i, li in enumerate(labels):
            if li not in boxes:
                continue
            x0i, y0i, x1i, y1i, wi, hi = boxes[li]
            for lj in labels[i + 1 :]:
                if lj not in boxes:
                    continue
                x0j, y0j, x1j, y1j, wj, hj = boxes[lj]
                x_overlap = max(0, min(x1i, x1j) - max(x0i, x0j))
                min_w = min(wi, wj)
                y_gap = max(0, max(y0i, y0j) - min(y1i, y1j))
                touching = np.any(dilated[li] & dilated[lj])
                if x_overlap < min_w * 0.35:
                    continue
                if y_gap > max(40, int(min(hi, hj) * 0.22)):
                    continue
                if not touching:
                    continue
                pairs.append((y_gap, li, lj))

        pairs.sort()
        return [(li, lj) for _, li, lj in pairs]

    def _refine_touching_pair(
        self,
        panel_labels: np.ndarray,
        image: np.ndarray,
        left_li: int,
        right_li: int,
    ) -> np.ndarray:
        def overlap_score(labels: np.ndarray) -> tuple[int, int]:
            left_mask = labels == left_li
            right_mask = labels == right_li
            overlap_rows = 0
            overlap_width = 0
            for row in range(labels.shape[0]):
                left_x = np.where(left_mask[row])[0]
                right_x = np.where(right_mask[row])[0]
                if len(left_x) == 0 or len(right_x) == 0:
                    continue
                gap = int(right_x.min()) - int(left_x.max())
                if gap < 0:
                    overlap_rows += 1
                    overlap_width += -gap
            return overlap_rows, overlap_width

        def frag_penalty(mask: np.ndarray) -> tuple[int, int]:
            n_cc, _, stats, _ = cv2.connectedComponentsWithStats(
                mask.astype(np.uint8), 8
            )
            if n_cc <= 2:
                return 0, 0
            areas = sorted(
                (int(stats[i, cv2.CC_STAT_AREA]) for i in range(1, n_cc)), reverse=True
            )
            return n_cc - 2, sum(areas[1:])

        def candidate_score(labels: np.ndarray) -> tuple[float, int, int, float]:
            left_mask = labels == left_li
            right_mask = labels == right_li
            overlap_rows, overlap_width = overlap_score(labels)
            left_x = np.where(left_mask)[1]
            right_x = np.where(right_mask)[1]
            if len(left_x) == 0 or len(right_x) == 0:
                return (float("inf"), 10**9, 10**9, float("inf"))

            mid_x = 0.5 * (float(left_x.mean()) + float(right_x.mean()))
            _, xx = np.indices(labels.shape)
            wrong_left = np.maximum(np.where(left_mask, xx - mid_x, 0.0), 0.0)
            wrong_right = np.maximum(np.where(right_mask, mid_x - xx, 0.0), 0.0)
            wrong_sum = float(wrong_left.sum() + wrong_right.sum())

            left_frag_n, left_frag_area = frag_penalty(left_mask)
            right_frag_n, right_frag_area = frag_penalty(right_mask)
            frag_n = left_frag_n + right_frag_n
            frag_area = left_frag_area + right_frag_area

            score = (
                overlap_rows * 2000.0
                + overlap_width * 100.0
                + wrong_sum
                + frag_n * 500.0
                + frag_area * 20.0
            )
            return score, overlap_rows, overlap_width, wrong_sum

        result = panel_labels.copy()
        union = (result == left_li) | (result == right_li)
        if not np.any(union):
            return result

        ys, xs = np.where(union)
        x0 = max(0, int(xs.min()) - 20)
        y0 = max(0, int(ys.min()) - 20)
        x1 = min(image.shape[1], int(xs.max()) + 21)
        y1 = min(image.shape[0], int(ys.max()) + 21)

        union_roi = union[y0:y1, x0:x1]
        coords = np.column_stack(np.where(union_roi))
        if len(coords) < 64:
            return result

        xs_union = coords[:, 1]
        left_cut = int(np.quantile(xs_union, 0.28))
        right_cut = int(np.quantile(xs_union, 0.72))
        if right_cut - left_cut < 12:
            return result

        x_grid = np.arange(union_roi.shape[1], dtype=np.int32)[None, :]
        left_seed = union_roi & (x_grid <= left_cut)
        right_seed = union_roi & (x_grid >= right_cut)

        kernel_size = int(np.clip(min(union_roi.shape[:2]) * 0.045, 7, 13))
        if kernel_size % 2 == 0:
            kernel_size += 1
        kern = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
        left_core = cv2.erode(left_seed.astype(np.uint8), kern, iterations=1) > 0
        right_core = cv2.erode(right_seed.astype(np.uint8), kern, iterations=1) > 0
        if not np.any(left_core) or not np.any(right_core):
            return result

        markers = np.zeros(union_roi.shape, dtype=np.int32)
        markers[left_core] = 1
        markers[right_core] = 2

        gray_norm = (
            cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY).astype(np.float32)
            / 255.0
        )
        sobel = filters.sobel(gray_norm)
        dark_strength = np.clip((0.22 - gray_norm) / 0.22, 0.0, 1.0)
        elevation = sobel + 4.0 * dark_strength
        labels = watershed(elevation, markers, mask=union_roi)
        if not np.any(labels == 1) or not np.any(labels == 2):
            return result

        before_roi = result[y0:y1, x0:x1].copy()
        roi = before_roi.copy()
        roi[union_roi & (labels == 1)] = left_li
        roi[union_roi & (labels == 2)] = right_li
        candidates: list[np.ndarray] = [before_roi, roi]

        core_left = self._core_seed(before_roi == left_li)
        core_right = self._core_seed(before_roi == right_li)
        if np.any(core_left) and np.any(core_right):
            markers_core = np.zeros(union_roi.shape, dtype=np.int32)
            markers_core[core_left] = 1
            markers_core[core_right] = 2
            core_labels = watershed(elevation, markers_core, mask=union_roi)
            if np.any(core_labels == 1) and np.any(core_labels == 2):
                core_roi = before_roi.copy()
                core_roi[union_roi & (core_labels == 1)] = left_li
                core_roi[union_roi & (core_labels == 2)] = right_li
                candidates.append(core_roi)

        extra: list[np.ndarray] = []
        for cand in candidates[1:]:
            left_x = np.where(cand == left_li)[1]
            right_x = np.where(cand == right_li)[1]
            if len(left_x) == 0 or len(right_x) == 0:
                continue
            left_center = float(left_x.mean())
            right_center = float(right_x.mean())
            mid_x = 0.5 * (left_center + right_center)
            _, xx = np.indices(cand.shape)
            active = union_roi & ((cand == left_li) | (cand == right_li))
            keep_core = core_left | core_right
            span_x = max(right_center - left_center, 1.0)
            tol_values = sorted(
                {
                    int(round(max(4.0, min(8.0, ratio * span_x))))
                    for ratio in (0.02, 0.03, 0.045)
                }
            )
            for tol_x in tol_values:
                clamped = cand.copy()
                clamped[active & (~keep_core) & (xx < mid_x - tol_x)] = left_li
                clamped[active & (~keep_core) & (xx > mid_x + tol_x)] = right_li
                extra.append(clamped)

            # Reclaim tiny detached stray components that land on the wrong side
            # of the midline after pair refinement.
            for owner_li, other_li in ((left_li, right_li), (right_li, left_li)):
                owner_mask = cand == owner_li
                n_cc, cc_labels, stats, _ = cv2.connectedComponentsWithStats(
                    owner_mask.astype(np.uint8),
                    8,
                )
                if n_cc <= 2:
                    continue
                main_id = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
                for cc_id in range(1, n_cc):
                    if cc_id == main_id:
                        continue
                    area = int(stats[cc_id, cv2.CC_STAT_AREA])
                    if area > 64:
                        continue
                    comp = cc_labels == cc_id
                    comp_x = np.where(comp)[1]
                    if len(comp_x) == 0:
                        continue
                    comp_cx = float(comp_x.mean())
                    if owner_li == left_li:
                        wrong_side = comp_cx > mid_x + 2.0
                    else:
                        wrong_side = comp_cx < mid_x - 2.0
                    if not wrong_side:
                        continue
                    reclaimed = cand.copy()
                    reclaimed[comp] = other_li
                    extra.append(reclaimed)
        candidates.extend(extra)

        best_roi = min(candidates, key=candidate_score)

        def reclaim_stray_components(labels: np.ndarray) -> np.ndarray:
            fixed = labels.copy()
            _, xx = np.indices(labels.shape)
            left_x = np.where(fixed == left_li)[1]
            right_x = np.where(fixed == right_li)[1]
            if len(left_x) == 0 or len(right_x) == 0:
                return fixed
            mid_x = 0.5 * (float(left_x.mean()) + float(right_x.mean()))
            for owner_li, other_li in ((left_li, right_li), (right_li, left_li)):
                owner_mask = fixed == owner_li
                n_cc, cc_labels, stats, _ = cv2.connectedComponentsWithStats(
                    owner_mask.astype(np.uint8),
                    8,
                )
                if n_cc <= 2:
                    continue
                main_id = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
                for cc_id in range(1, n_cc):
                    if cc_id == main_id:
                        continue
                    area = int(stats[cc_id, cv2.CC_STAT_AREA])
                    if area > 16:
                        continue
                    comp = cc_labels == cc_id
                    comp_x = np.where(comp)[1]
                    if len(comp_x) == 0:
                        continue
                    comp_cx = float(comp_x.mean())
                    if owner_li == left_li:
                        wrong_side = comp_cx > mid_x + 2.0
                    else:
                        wrong_side = comp_cx < mid_x - 2.0
                    if wrong_side:
                        fixed[comp] = other_li
                        continue

                    # If an isolated tiny component is nearly background-colored,
                    # treat it as a residual island instead of foreground.
                    comp_pixels = image[y0:y1, x0:x1][comp]
                    if (
                        area <= 8
                        and len(comp_pixels) > 0
                        and float(np.mean(comp_pixels)) >= 236.0
                    ):
                        fixed[comp] = 0
            return fixed

        best_roi = reclaim_stray_components(best_roi)
        if not np.array_equal(best_roi, before_roi):
            result[y0:y1, x0:x1] = best_roi
        return result

    def _refine_vertical_touching_pair(
        self,
        panel_labels: np.ndarray,
        image: np.ndarray,
        top_li: int,
        bottom_li: int,
    ) -> np.ndarray:
        result = panel_labels.copy()
        union = (result == top_li) | (result == bottom_li)
        if not np.any(union):
            return result

        ys, xs = np.where(union)
        x0 = max(0, int(xs.min()) - 20)
        y0 = max(0, int(ys.min()) - 20)
        x1 = min(image.shape[1], int(xs.max()) + 21)
        y1 = min(image.shape[0], int(ys.max()) + 21)

        union_roi = union[y0:y1, x0:x1]
        coords = np.column_stack(np.where(union_roi))
        if len(coords) < 64:
            return result

        ys_union = coords[:, 0]
        top_cut = int(np.quantile(ys_union, 0.28))
        bottom_cut = int(np.quantile(ys_union, 0.72))
        if bottom_cut - top_cut < 12:
            return result

        y_grid = np.arange(union_roi.shape[0], dtype=np.int32)[:, None]
        top_seed = union_roi & (y_grid <= top_cut)
        bottom_seed = union_roi & (y_grid >= bottom_cut)

        kernel_size = int(np.clip(min(union_roi.shape[:2]) * 0.045, 7, 13))
        if kernel_size % 2 == 0:
            kernel_size += 1
        kern = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
        top_core = cv2.erode(top_seed.astype(np.uint8), kern, iterations=1) > 0
        bottom_core = cv2.erode(bottom_seed.astype(np.uint8), kern, iterations=1) > 0
        if not np.any(top_core) or not np.any(bottom_core):
            return result

        markers = np.zeros(union_roi.shape, dtype=np.int32)
        markers[top_core] = 1
        markers[bottom_core] = 2

        gray_norm = (
            cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY).astype(np.float32)
            / 255.0
        )
        sobel = filters.sobel(gray_norm)
        dark_strength = np.clip((0.22 - gray_norm) / 0.22, 0.0, 1.0)
        elevation = sobel + 4.0 * dark_strength
        labels = watershed(elevation, markers, mask=union_roi)
        if not np.any(labels == 1) or not np.any(labels == 2):
            return result

        roi = result[y0:y1, x0:x1].copy()
        roi[union_roi & (labels == 1)] = top_li
        roi[union_roi & (labels == 2)] = bottom_li
        result[y0:y1, x0:x1] = roi
        return result

    def _detect_panels(self, image: np.ndarray, fg_struct: np.ndarray) -> np.ndarray:
        h, w = fg_struct.shape[:2]
        n_cc, cc_labels = cv2.connectedComponents(fg_struct)
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        gray_norm = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0

        cc_info: dict[int, int] = {}
        for cc_id in range(1, n_cc):
            area = int(np.count_nonzero(cc_labels == cc_id))
            if area >= h * w * 0.003:
                cc_info[cc_id] = area
        if not cc_info:
            return np.zeros((h, w), dtype=np.int32)

        out_label = np.zeros((h, w), dtype=np.int32)
        next_li = 1

        for cc_id, area in cc_info.items():
            cc_mask = cc_labels == cc_id
            dist = cv2.distanceTransform(cc_mask.astype(np.uint8) * 255, cv2.DIST_L2, 5)
            peaks = self._auto_peaks(dist, cc_mask.astype(np.uint8) * 255)

            if len(peaks) <= 1:
                out_label[cc_mask] = next_li
                next_li += 1
                continue

            rag_labels = self._rag_split_cc(rgb, gray_norm, cc_mask, len(peaks))
            if rag_labels is not None:
                for ri in range(1, int(rag_labels.max()) + 1):
                    out_label[rag_labels == ri] = next_li
                    next_li += 1
                continue

            fluid_labels = self._fluid_split(cc_mask.astype(np.uint8) * 255, peaks)
            for ri in range(1, int(fluid_labels.max()) + 1):
                out_label[fluid_labels == ri] = next_li
                next_li += 1

        for li, lj in self._candidate_touching_pairs(out_label):
            left_li, right_li = li, lj
            cx_i = float(np.where(out_label == li)[1].mean())
            cx_j = float(np.where(out_label == lj)[1].mean())
            if cx_i > cx_j:
                left_li, right_li = lj, li
            out_label = self._refine_touching_pair(out_label, image, left_li, right_li)

        for li, lj in self._candidate_vertical_touching_pairs(out_label):
            top_li, bottom_li = li, lj
            cy_i = float(np.where(out_label == li)[0].mean())
            cy_j = float(np.where(out_label == lj)[0].mean())
            if cy_i > cy_j:
                top_li, bottom_li = lj, li
            out_label = self._refine_vertical_touching_pair(
                out_label,
                image,
                top_li,
                bottom_li,
            )

        return out_label

    @staticmethod
    def _group_text_words(text_ccs: list[dict[str, float]]) -> list[list[int]]:
        n = len(text_ccs)
        if n < 2:
            return [list(range(n))]

        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for i in range(n):
            for j in range(i + 1, n):
                ci, cj = text_ccs[i], text_ccs[j]
                y_overlap = max(0.0, min(ci["y1"], cj["y1"]) - max(ci["y0"], cj["y0"]))
                min_h = min(ci["y1"] - ci["y0"], cj["y1"] - cj["y0"])
                if min_h > 0 and y_overlap > min_h * 0.3:
                    x_gap = max(0.0, max(ci["x0"], cj["x0"]) - min(ci["x1"], cj["x1"]))
                    max_w = max(ci["x1"] - ci["x0"], cj["x1"] - cj["x0"])
                    if x_gap < max(max_w * 2.0, 15.0):
                        union(i, j)

        groups: dict[int, list[int]] = {}
        for i in range(n):
            root = find(i)
            groups.setdefault(root, []).append(i)
        return list(groups.values())

    def _expand_labels(
        self,
        panel_labels: np.ndarray,
        fg_raw: np.ndarray,
        fg_struct: np.ndarray,
    ) -> np.ndarray:
        n_panels = int(panel_labels.max())
        if n_panels == 0:
            return panel_labels

        h, w = fg_raw.shape
        centroids: dict[int, tuple[float, float]] = {}
        panel_boxes: dict[int, tuple[int, int, int, int]] = {}
        body_boxes: dict[int, tuple[int, int, int, int]] = {}
        for li in range(1, n_panels + 1):
            ys, xs = np.where(panel_labels == li)
            if len(ys) == 0:
                continue
            cx, cy = float(xs.mean()), float(ys.mean())
            centroids[li] = (cx, cy)
            x0, y0 = int(xs.min()), int(ys.min())
            x1, y1 = int(xs.max()), int(ys.max())
            bw, bh = x1 - x0, y1 - y0
            body_boxes[li] = (x0, y0, x1, y1)
            margin = int(round(max(bw, bh) * 0.22))
            margin = max(6, margin)
            panel_boxes[li] = (
                max(0, x0 - margin),
                max(0, y0 - margin),
                min(w - 1, x1 + margin),
                min(h - 1, y1 + margin),
            )

        unassigned = ((fg_raw > 0) & (fg_struct == 0)).astype(np.uint8) * 255
        n_cc, cc_labels = cv2.connectedComponents(unassigned)

        text_ccs: list[dict[str, float]] = []
        for cc_id in range(1, n_cc):
            ys, xs = np.where(cc_labels == cc_id)
            if len(ys) < 3:
                continue
            text_ccs.append(
                {
                    "id": float(cc_id),
                    "x0": float(xs.min()),
                    "y0": float(ys.min()),
                    "x1": float(xs.max()),
                    "y1": float(ys.max()),
                    "cx": float(xs.mean()),
                    "cy": float(ys.mean()),
                }
            )

        if not text_ccs:
            return panel_labels.copy()

        word_groups = self._group_text_words(text_ccs)
        result = panel_labels.copy()

        for group in word_groups:
            gx0 = min(text_ccs[i]["x0"] for i in group)
            gy0 = min(text_ccs[i]["y0"] for i in group)
            gx1 = max(text_ccs[i]["x1"] for i in group)
            gy1 = max(text_ccs[i]["y1"] for i in group)
            gcx = float(np.mean([text_ccs[i]["cx"] for i in group]))
            gcy = float(np.mean([text_ccs[i]["cy"] for i in group]))

            candidates: list[tuple[float, int]] = []
            for li, (bx0, by0, bx1, by1) in panel_boxes.items():
                if not (bx0 <= gcx <= bx1 and by0 <= gcy <= by1):
                    continue

                body_x0, body_y0, body_x1, body_y1 = body_boxes[li]
                body_w = max(1, body_x1 - body_x0)
                body_h = max(1, body_y1 - body_y0)
                x_overreach = max(0.0, body_x0 - gx0) + max(0.0, gx1 - body_x1)
                y_gap = max(0.0, gy0 - body_y1)
                y_above = max(0.0, body_y0 - gy1)
                score = (
                    (gcx - centroids[li][0]) ** 2
                    + (gcy - centroids[li][1]) ** 2
                    + (x_overreach**2) * 3.0
                    + (y_gap**2) * 0.5
                    + (y_above**2) * 0.2
                )

                # Keep nearby vertical text attached to its panel unless it is
                # clearly detached far below the body and far outside the body width.
                below_allow = max(6.0, body_h * 0.28)
                right_allow = max(6.0, body_w * 0.22)
                left_allow = max(4.0, body_w * 0.16)
                above_allow = max(4.0, body_h * 0.16)
                if gy0 > body_y1 + below_allow and x_overreach > body_w * 0.35:
                    continue
                if gx0 >= body_x1 and gx0 - body_x1 <= right_allow:
                    score *= 0.55
                if gx1 <= body_x0 and body_x0 - gx1 <= left_allow:
                    score *= 0.8
                if gy1 <= body_y0 and body_y0 - gy1 <= above_allow:
                    score *= 0.85
                candidates.append((score, li))

            if candidates:
                best_li = min(candidates)[1]
            else:
                best_li = min(
                    centroids,
                    key=lambda li: (
                        (gcx - centroids[li][0]) ** 2 + (gcy - centroids[li][1]) ** 2
                    ),
                )

            for i in group:
                result[cc_labels == int(text_ccs[i]["id"])] = best_li

        return result

    @staticmethod
    def _core_seed(mask: np.ndarray) -> np.ndarray:
        h, w = mask.shape
        for kernel_size in [11, 9, 7, 5, 3]:
            if kernel_size > min(h, w):
                continue
            kern = cv2.getStructuringElement(
                cv2.MORPH_ELLIPSE,
                (kernel_size, kernel_size),
            )
            core = cv2.erode(mask.astype(np.uint8), kern, iterations=1) > 0
            if np.any(core):
                return core
        return mask.copy()

    @staticmethod
    def _travel_costs(
        union_roi: np.ndarray,
        gray_norm: np.ndarray,
        seed_mask: np.ndarray,
    ) -> np.ndarray:
        sobel = filters.sobel(gray_norm)
        dark = np.clip((0.22 - gray_norm) / 0.22, 0.0, 1.0)
        cost = (1.0 + 10.0 * dark + 3.0 * sobel).astype(np.float32)
        cost[~union_roi] = 1e6

        starts = [tuple(pt) for pt in np.argwhere(seed_mask)]
        mcp = graph.MCP_Geometric(cost)
        costs, _ = mcp.find_costs(starts=starts)
        return costs

    def _refine_expanded_touching_band(
        self,
        expanded_labels: np.ndarray,
        panel_labels: np.ndarray,
        image: np.ndarray,
        first_li: int,
        second_li: int,
        *,
        vertical: bool = False,
    ) -> np.ndarray:
        result = expanded_labels.copy()
        exp_first = result == first_li
        exp_second = result == second_li
        core_first = panel_labels == first_li
        core_second = panel_labels == second_li
        union = exp_first | exp_second
        if not np.any(union) or not np.any(core_first) or not np.any(core_second):
            return result

        ys, xs = np.where(union)
        x0 = max(0, int(xs.min()) - 24)
        y0 = max(0, int(ys.min()) - 24)
        x1 = min(image.shape[1], int(xs.max()) + 25)
        y1 = min(image.shape[0], int(ys.max()) + 25)

        union_roi = union[y0:y1, x0:x1]
        core_first_roi = core_first[y0:y1, x0:x1]
        core_second_roi = core_second[y0:y1, x0:x1]
        if not np.any(core_first_roi) or not np.any(core_second_roi):
            return result

        first_ys, first_xs = np.where(core_first_roi)
        second_ys, second_xs = np.where(core_second_roi)
        if (
            len(first_ys) == 0
            or len(first_xs) == 0
            or len(second_ys) == 0
            or len(second_xs) == 0
        ):
            return result

        first_h = int(first_ys.max() - first_ys.min() + 1)
        first_w = int(first_xs.max() - first_xs.min() + 1)
        second_h = int(second_ys.max() - second_ys.min() + 1)
        second_w = int(second_xs.max() - second_xs.min() + 1)
        scale_x = max(1.0, float(min(first_w, second_w)))
        scale_y = max(1.0, float(min(first_h, second_h)))

        dist_first = distance_transform_edt(~core_first_roi)
        dist_second = distance_transform_edt(~core_second_roi)
        dist_cap = max(
            12.0,
            min(
                float(max(union_roi.shape[:2])),
                max(scale_x, scale_y) * 0.65,
            ),
        )
        work = union_roi & (dist_first <= dist_cap) & (dist_second <= dist_cap)

        if vertical:
            band_pad = max(6, int(round(scale_y * 0.12)))
            band_lo = max(int(first_ys.max()) - band_pad, 0)
            band_hi = min(int(second_ys.min()) + band_pad, union_roi.shape[0] - 1)
            yy = np.arange(union_roi.shape[0], dtype=np.int32)[:, None]
            work &= (yy >= band_lo) & (yy <= band_hi)
        else:
            band_pad = max(6, int(round(scale_x * 0.12)))
            band_lo = max(int(first_xs.max()) - band_pad, 0)
            band_hi = min(int(second_xs.min()) + band_pad, union_roi.shape[1] - 1)
            xx = np.arange(union_roi.shape[1], dtype=np.int32)[None, :]
            work &= (xx >= band_lo) & (xx <= band_hi)

        min_work = max(12, int(round(min(scale_x, scale_y) * 0.08)))
        if np.count_nonzero(work) < min_work:
            return result

        seed_first = self._core_seed(core_first_roi)
        seed_second = self._core_seed(core_second_roi)
        gray_norm = (
            cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY).astype(np.float32)
            / 255.0
        )
        costs_first = self._travel_costs(union_roi, gray_norm, seed_first)
        costs_second = self._travel_costs(union_roi, gray_norm, seed_second)

        roi = result[y0:y1, x0:x1].copy()
        movable = work & (~core_first_roi) & (~core_second_roi)
        cost_margin = (
            max(
                0.75,
                float(np.quantile(np.abs(costs_first - costs_second)[movable], 0.2)),
            )
            if np.any(movable)
            else 0.75
        )
        move_first = movable & (costs_first + cost_margin < costs_second)
        move_second = movable & (costs_second + cost_margin < costs_first)
        roi[move_first] = first_li
        roi[move_second] = second_li
        result[y0:y1, x0:x1] = roi
        return result

    @staticmethod
    def _estimate_crop_bg_color(
        crop_img: np.ndarray,
        local_labels: np.ndarray,
        hard_mask: np.ndarray,
    ) -> np.ndarray:
        border = np.zeros(hard_mask.shape, dtype=bool)
        border[0, :] = True
        border[-1, :] = True
        border[:, 0] = True
        border[:, -1] = True

        samples = crop_img[border & (local_labels == 0)]
        if len(samples) < 16:
            samples = crop_img[border & (~hard_mask)]
        if len(samples) == 0:
            samples = crop_img[~hard_mask]
        if len(samples) == 0:
            return np.array([255.0, 255.0, 255.0], dtype=np.float32)
        return np.median(samples.astype(np.float32), axis=0)

    @classmethod
    def _restore_soft_edge_rgba(
        cls,
        crop_img: np.ndarray,
        local_labels: np.ndarray,
        li: int,
        hard_alpha: np.ndarray,
        detail_mask: np.ndarray,
    ) -> np.ndarray:
        hard_mask = hard_alpha > 0
        if not np.any(hard_mask):
            rgba = cv2.cvtColor(crop_img, cv2.COLOR_BGR2BGRA)
            rgba[:, :, 3] = hard_alpha
            return rgba

        bg_color = cls._estimate_crop_bg_color(crop_img, local_labels, hard_mask)
        shell = (
            cv2.dilate(
                hard_alpha,
                cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)),
                iterations=1,
            )
            > 0
        )
        candidate = (
            shell
            & (~hard_mask)
            & ((local_labels == li) | (local_labels == 0))
            & (detail_mask > 0)
        )

        rgb = crop_img.astype(np.float32)
        gray = cv2.cvtColor(crop_img, cv2.COLOR_BGR2GRAY)
        bg_dist = np.linalg.norm(rgb - bg_color[None, None, :], axis=2)
        candidate &= (gray < 250) & (bg_dist > 6.0)

        _, (iy, ix) = distance_transform_edt(~hard_mask, return_indices=True)
        fg_est = rgb[iy, ix]

        den = bg_color[None, None, :] - fg_est
        num = bg_color[None, None, :] - rgb
        valid = np.abs(den) > 8.0
        alpha_by_channel = np.zeros_like(rgb, dtype=np.float32)
        alpha_by_channel[valid] = num[valid] / den[valid]
        alpha_by_channel = np.clip(alpha_by_channel, 0.0, 1.0)

        alpha_shell = alpha_by_channel.max(axis=2)
        alpha_shell = np.where(candidate, alpha_shell, 0.0)
        alpha_shell = np.where(alpha_shell > 0.02, alpha_shell, 0.0)

        alpha = np.maximum(hard_alpha.astype(np.float32) / 255.0, alpha_shell)
        # Preserve original crop background colors under transparent pixels so
        # callers can flatten back onto the source background later.
        out_rgb = rgb.copy()
        out_rgb[hard_mask] = rgb[hard_mask]

        soft_only = (alpha > 0.0) & (~hard_mask)
        out_rgb[soft_only] = fg_est[soft_only]

        rgba = np.dstack([out_rgb, alpha[:, :, None] * 255.0]).astype(np.uint8)
        return rgba

    @classmethod
    def _refine_crop_alpha(cls, crop_alpha: np.ndarray) -> np.ndarray:
        binary = (crop_alpha > 0).astype(np.uint8)
        n_cc, cc_labels, stats, _ = cv2.connectedComponentsWithStats(binary, 8)
        if n_cc <= 2:
            return crop_alpha

        areas = stats[1:, cv2.CC_STAT_AREA]
        main_id = 1 + int(np.argmax(areas))
        mx = int(stats[main_id, cv2.CC_STAT_LEFT])
        my = int(stats[main_id, cv2.CC_STAT_TOP])
        mw = int(stats[main_id, cv2.CC_STAT_WIDTH])
        mh = int(stats[main_id, cv2.CC_STAT_HEIGHT])
        main_area = int(stats[main_id, cv2.CC_STAT_AREA])

        aux_ccs: list[dict[str, float]] = []
        for cc_id in range(1, n_cc):
            if cc_id == main_id:
                continue
            x = int(stats[cc_id, cv2.CC_STAT_LEFT])
            y = int(stats[cc_id, cv2.CC_STAT_TOP])
            w = int(stats[cc_id, cv2.CC_STAT_WIDTH])
            h = int(stats[cc_id, cv2.CC_STAT_HEIGHT])
            area = int(stats[cc_id, cv2.CC_STAT_AREA])
            aux_ccs.append(
                {
                    "id": float(cc_id),
                    "x0": float(x),
                    "y0": float(y),
                    "x1": float(x + w - 1),
                    "y1": float(y + h - 1),
                    "cx": float(x + (w - 1) / 2),
                    "cy": float(y + (h - 1) / 2),
                    "area": float(area),
                }
            )

        if not aux_ccs:
            return crop_alpha

        keep_ids = {main_id}
        head_band_y = my + max(6, int(round(mh * 0.4)))
        x_margin = max(6, int(round(mw * 0.35)))
        min_group_area = max(6, int(round(main_area * 0.0008)))

        for group in cls._group_text_words(aux_ccs):
            x0 = min(aux_ccs[i]["x0"] for i in group)
            y1 = max(aux_ccs[i]["y1"] for i in group)
            x1 = max(aux_ccs[i]["x1"] for i in group)
            area = int(sum(aux_ccs[i]["area"] for i in group))
            near_head = y1 <= head_band_y
            near_main = x1 >= mx - x_margin and x0 <= mx + mw - 1 + x_margin
            if near_head and near_main and area >= min_group_area:
                for i in group:
                    keep_ids.add(int(aux_ccs[i]["id"]))

        refined = np.zeros_like(crop_alpha)
        for cc_id in keep_ids:
            refined[cc_labels == cc_id] = 255
        return refined

    def _extract_crops(
        self,
        image: np.ndarray,
        expanded_labels: np.ndarray,
        fg_raw: np.ndarray,
        fg_detail: np.ndarray,
    ) -> list[np.ndarray]:
        h, w = image.shape[:2]
        n_panels = int(expanded_labels.max())
        crops: list[np.ndarray] = []

        for li in range(1, n_panels + 1):
            panel_mask = expanded_labels == li
            ys, xs = np.where(panel_mask)
            if len(ys) == 0:
                continue

            rx, ry = int(xs.min()), int(ys.min())
            rw = int(xs.max() - xs.min() + 1)
            rh = int(ys.max() - ys.min() + 1)
            if rw < 30 or rh < 30:
                continue

            pad = int(min(rw, rh) * 0.05)
            rx0, ry0 = max(0, rx - pad), max(0, ry - pad)
            rx1, ry1 = min(w, rx + rw + pad), min(h, ry + rh + pad)

            crop_img = image[ry0:ry1, rx0:rx1]
            local_labels = expanded_labels[ry0:ry1, rx0:rx1]
            panel_crop = (local_labels == li).astype(np.uint8) * 255
            crop_alpha = cv2.bitwise_and(fg_raw[ry0:ry1, rx0:rx1], panel_crop)
            crop_alpha = self._refine_crop_alpha(crop_alpha)
            crop_rgba = self._restore_soft_edge_rgba(
                crop_img,
                local_labels,
                li,
                crop_alpha,
                fg_detail[ry0:ry1, rx0:rx1],
            )

            ys_a, xs_a = np.where(crop_rgba[:, :, 3] > 0)
            if len(ys_a) == 0:
                continue

            ax0, ay0 = int(xs_a.min()), int(ys_a.min())
            ax1, ay1 = int(xs_a.max()) + 1, int(ys_a.max()) + 1
            pad_alpha = max(2, int(round(min(ax1 - ax0, ay1 - ay0) * 0.04)))
            ax0 = max(0, ax0 - pad_alpha)
            ay0 = max(0, ay0 - pad_alpha)
            ax1 = min(crop_rgba.shape[1], ax1 + pad_alpha)
            ay1 = min(crop_rgba.shape[0], ay1 + pad_alpha)

            crop_rgba = crop_rgba[ay0:ay1, ax0:ax1]
            crop_alpha = crop_rgba[:, :, 3]

            # Enforce a minimum transparent breathing room so detached text and
            # thin outlines are not visually glued to the crop border.
            ys_m, xs_m = np.where(crop_alpha > 0)
            top_margin = int(ys_m.min()) if len(ys_m) else 0
            bottom_margin = (
                int(crop_alpha.shape[0] - 1 - ys_m.max()) if len(ys_m) else 0
            )
            left_margin = int(xs_m.min()) if len(xs_m) else 0
            right_margin = int(crop_alpha.shape[1] - 1 - xs_m.max()) if len(xs_m) else 0
            desired_margin = max(
                2,
                int(round(max(ax1 - ax0, ay1 - ay0) * 0.06)),
            )
            top_pad = max(0, desired_margin - top_margin)
            bottom_pad = max(0, desired_margin - bottom_margin)
            left_pad = max(0, desired_margin - left_margin)
            right_pad = max(0, desired_margin - right_margin)
            if top_pad or bottom_pad or left_pad or right_pad:
                crop_rgba = cv2.copyMakeBorder(
                    crop_rgba,
                    top_pad,
                    bottom_pad,
                    left_pad,
                    right_pad,
                    cv2.BORDER_CONSTANT,
                    value=(255, 255, 255, 0),
                )
            crops.append(crop_rgba)

        return crops

    def process_image(
        self,
        image: np.ndarray,
        debug: bool = False,
    ) -> tuple[list[np.ndarray], np.ndarray | None]:
        bgr = self._ensure_bgr(image)
        fg_raw = self._make_fg_raw(bgr)
        fg_detail = self._make_fg_detail(bgr)
        fg_struct = self._make_fg_struct(fg_raw)
        panel_labels = self._detect_panels(bgr, fg_struct)
        if int(panel_labels.max()) == 0:
            self.last_panel_labels = panel_labels
            self.last_expanded_labels = panel_labels
            return [], None

        expanded = self._expand_labels(panel_labels, fg_raw, fg_struct)
        for li, lj in self._candidate_touching_pairs(panel_labels):
            left_li, right_li = li, lj
            cx_i = float(np.where(panel_labels == li)[1].mean())
            cx_j = float(np.where(panel_labels == lj)[1].mean())
            if cx_i > cx_j:
                left_li, right_li = lj, li
            expanded = self._refine_expanded_touching_band(
                expanded,
                panel_labels,
                bgr,
                left_li,
                right_li,
                vertical=False,
            )
        for li, lj in self._candidate_vertical_touching_pairs(panel_labels):
            top_li, bottom_li = li, lj
            cy_i = float(np.where(panel_labels == li)[0].mean())
            cy_j = float(np.where(panel_labels == lj)[0].mean())
            if cy_i > cy_j:
                top_li, bottom_li = lj, li
            expanded = self._refine_expanded_touching_band(
                expanded,
                panel_labels,
                bgr,
                top_li,
                bottom_li,
                vertical=True,
            )

        crops = self._extract_crops(bgr, expanded, fg_raw, fg_detail)
        self.last_panel_labels = panel_labels
        self.last_expanded_labels = expanded
        debug_img = self._build_debug_image(bgr, expanded) if debug else None
        return crops, debug_img
