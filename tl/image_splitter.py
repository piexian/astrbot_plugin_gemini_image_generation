import zipfile
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from astrbot.api import logger

from .tl_utils import (
    get_plugin_data_dir,
    is_valid_base64_image_str,
    resolve_image_source_to_path,
)


class LegacySmartMemeSplitter:
    """
    旧版表情包切分器，保留兼容性（未使用）
    """

    def __init__(self, min_gap=5, edge_threshold=10):
        self.min_gap = min_gap
        self.edge_threshold = edge_threshold
        self.last_row_lines: list[int] = []
        self.last_col_lines: list[int] = []

    def detect_grid(
        self, image: np.ndarray, debug: bool = False
    ) -> list[tuple[int, int, int, int]]:
        logger.warning("LegacySmartMemeSplitter 已弃用，请使用新版 SmartMemeSplitter")
        return []


class SmartMemeSplitter:
    """
    v4 网格切分算法：结合颜色边缘突变、能量图与网格候选微调
    """

    def __init__(self, sensitivity: float = 0.2):
        self.sensitivity = sensitivity
        self.process_debug: dict[str, np.ndarray] = {}
        self.last_row_lines: list[int] = []
        self.last_col_lines: list[int] = []

    def compute_color_edge_mutation(self, img: np.ndarray) -> np.ndarray:
        """颜色边缘突变分析：彩色形态学梯度 + OTSU"""
        blurred = cv2.GaussianBlur(img, (3, 3), 0)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        gradient = cv2.morphologyEx(blurred, cv2.MORPH_GRADIENT, kernel)
        gray_grad = (
            cv2.cvtColor(gradient, cv2.COLOR_BGR2GRAY)
            if len(gradient.shape) == 3
            else gradient
        )
        mean_val = np.mean(gray_grad)
        _, binary = cv2.threshold(
            gray_grad, mean_val + 20, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU
        )
        morph_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 1))
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, morph_kernel, iterations=2)
        return binary

    def visualize_projection_analysis(self, edge_map: np.ndarray) -> np.ndarray:
        """XY 轮廓不连续性可视化（调试用）"""
        h, w = edge_map.shape
        plot_h, plot_w = 100, 100
        canvas = np.full((h + plot_h, w + plot_w, 3), 255, dtype=np.uint8)
        edge_bgr = cv2.cvtColor(edge_map, cv2.COLOR_GRAY2BGR)
        canvas[0:h, 0:w] = edge_bgr

        row_proj = np.sum(edge_map, axis=1)
        max_row = np.max(row_proj) if np.max(row_proj) > 0 else 1
        pts_y = []
        for r in range(h):
            val = row_proj[r]
            bar_len = int((val / max_row) * (plot_w - 5))
            pts_y.append((w + bar_len, r))
            if val < np.mean(row_proj) * 0.5:
                cv2.line(canvas, (w, r), (w + plot_w, r), (220, 220, 255), 1)
            else:
                cv2.line(canvas, (w, r), (w + bar_len, r), (200, 200, 200), 1)
        cv2.polylines(canvas, [np.array(pts_y)], False, (255, 0, 0), 1)

        col_proj = np.sum(edge_map, axis=0)
        max_col = np.max(col_proj) if np.max(col_proj) > 0 else 1
        pts_x = []
        for c in range(w):
            val = col_proj[c]
            bar_len = int((val / max_col) * (plot_h - 5))
            pts_x.append((c, h + bar_len))
            if val < np.mean(col_proj) * 0.5:
                cv2.line(canvas, (c, h), (c, h + plot_h), (220, 220, 255), 1)
            else:
                cv2.line(canvas, (c, h), (c, h + bar_len), (200, 200, 200), 1)
        cv2.polylines(canvas, [np.array(pts_x)], False, (0, 180, 0), 1)

        cv2.line(canvas, (w, 0), (w, h + plot_h), (0, 0, 0), 1)
        cv2.line(canvas, (0, h), (w + plot_w, h), (0, 0, 0), 1)
        return canvas

    def visualize_color_brightness_mutation_range(
        self, image: np.ndarray
    ) -> np.ndarray:
        """颜色/亮度突变范围图（调试用）"""
        h, w = image.shape[:2]
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        grad_x = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
        grad_y = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
        grad_magnitude = cv2.convertScaleAbs(cv2.magnitude(grad_x, grad_y))

        plot_h, plot_w = 100, 100
        canvas = np.full((h + plot_h, w + plot_w, 3), 255, dtype=np.uint8)
        grad_bgr = cv2.cvtColor(grad_magnitude, cv2.COLOR_GRAY2BGR)
        canvas[0:h, 0:w] = grad_bgr

        x_proj = np.sum(grad_magnitude, axis=0)
        max_x = np.max(x_proj) if np.max(x_proj) > 0 else 1
        for c in range(w):
            val = x_proj[c]
            bar_len = int((val / max_x) * (plot_h - 5))
            cv2.line(canvas, (c, h), (c, h + bar_len), (0, 0, 255), 1)

        y_proj = np.sum(grad_magnitude, axis=1)
        max_y = np.max(y_proj) if np.max(y_proj) > 0 else 1
        for r in range(h):
            val = y_proj[r]
            bar_len = int((val / max_y) * (plot_w - 5))
            cv2.line(canvas, (w, r), (w + bar_len, r), (255, 0, 0), 1)

        cv2.line(canvas, (w, 0), (w, h + plot_h), (0, 0, 0), 1)
        cv2.line(canvas, (0, h), (w + plot_w, h), (0, 0, 0), 1)
        return canvas

    def visualize_color_energy_map(self, image: np.ndarray) -> np.ndarray:
        """颜色能量图（调试用）"""
        h, w = image.shape[:2]
        grad_r = (
            cv2.Sobel(image[:, :, 0], cv2.CV_64F, 1, 0, ksize=3) ** 2
            + cv2.Sobel(image[:, :, 0], cv2.CV_64F, 0, 1, ksize=3) ** 2
        )
        grad_g = (
            cv2.Sobel(image[:, :, 1], cv2.CV_64F, 1, 0, ksize=3) ** 2
            + cv2.Sobel(image[:, :, 1], cv2.CV_64F, 0, 1, ksize=3) ** 2
        )
        grad_b = (
            cv2.Sobel(image[:, :, 2], cv2.CV_64F, 1, 0, ksize=3) ** 2
            + cv2.Sobel(image[:, :, 2], cv2.CV_64F, 0, 1, ksize=3) ** 2
        )
        energy = np.sqrt(grad_r + grad_g + grad_b)
        energy = cv2.convertScaleAbs(energy)

        plot_h, plot_w = 100, 100
        canvas = np.full((h + plot_h, w + plot_w, 3), 255, dtype=np.uint8)
        energy_bgr = cv2.cvtColor(energy, cv2.COLOR_GRAY2BGR)
        canvas[0:h, 0:w] = energy_bgr

        x_energy = np.sum(energy, axis=0)
        max_x = np.max(x_energy) if np.max(x_energy) > 0 else 1
        for c in range(w):
            val = x_energy[c]
            bar_len = int((val / max_x) * (plot_h - 5))
            cv2.line(canvas, (c, h), (c, h + bar_len), (0, 255, 0), 1)

        y_energy = np.sum(energy, axis=1)
        max_y = np.max(y_energy) if np.max(y_energy) > 0 else 1
        for r in range(h):
            val = y_energy[r]
            bar_len = int((val / max_y) * (plot_w - 5))
            cv2.line(canvas, (w, r), (w + bar_len, r), (0, 0, 255), 1)

        cv2.line(canvas, (w, 0), (w, h + plot_h), (0, 0, 0), 1)
        cv2.line(canvas, (0, h), (w + plot_w, h), (0, 0, 0), 1)
        return canvas

    def get_cut_points(self, projection: np.ndarray) -> list[int]:
        """在波谷附近寻找切割点"""
        smoothed = np.convolve(projection, np.ones(5) / 5, mode="same")
        content_thresh = np.mean(smoothed) * 0.5
        mask = smoothed > content_thresh
        cuts: list[int] = []
        i = 1
        while i < len(mask):
            if mask[i] and not mask[i - 1]:
                gap_start = i - 1
                while i < len(mask) and mask[i]:
                    i += 1
                gap_end = i
                gap_length = gap_end - gap_start
                if gap_length > 0:
                    cuts.append(gap_start + gap_length // 2)
            else:
                i += 1
        return cuts

    def analyze_grid_variations(
        self, image: np.ndarray, edge_map: np.ndarray | None = None, max_grid: int = 8
    ) -> list[dict]:
        """遍历 1..max_grid 的均分网格，基于覆盖率/中心距/闭环比例评分"""
        h, w = image.shape[:2]
        if edge_map is None:
            edge_map = self.compute_color_edge_mutation(image)

        candidates = []
        min_area = max(16, (w * h) // 10000)
        for rows in range(1, max_grid + 1):
            for cols in range(1, max_grid + 1):
                cell_w = w / cols
                cell_h = h / rows
                total_cells = rows * cols

                centroids = []
                closed_count = 0
                dist_list = []
                occupied = 0

                for r in range(rows):
                    for c in range(cols):
                        x0 = int(round(c * cell_w))
                        y0 = int(round(r * cell_h))
                        x1 = int(round((c + 1) * cell_w))
                        y1 = int(round((r + 1) * cell_h))
                        crop = edge_map[y0:y1, x0:x1]
                        if crop.size == 0:
                            continue
                        contours, _ = cv2.findContours(
                            crop.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
                        )
                        if not contours:
                            continue
                        largest = max(contours, key=cv2.contourArea)
                        area = cv2.contourArea(largest)
                        M = cv2.moments(largest)
                        if M.get("m00", 0) != 0:
                            cx = int(M["m10"] / M["m00"]) + x0
                            cy = int(M["m01"] / M["m00"]) + y0
                        else:
                            cx = (x0 + x1) // 2
                            cy = (y0 + y1) // 2
                        centroids.append((cx, cy))
                        occupied += 1
                        if area >= min_area:
                            closed_count += 1

                        center_x = (x0 + x1) / 2.0
                        center_y = (y0 + y1) / 2.0
                        diag = np.hypot(cell_w, cell_h)
                        dist_list.append(
                            np.hypot(cx - center_x, cy - center_y) / (diag + 1e-6)
                        )

                coverage = occupied / float(total_cells) if total_cells > 0 else 0.0
                mean_dist = float(np.mean(dist_list)) if dist_list else 1.0
                closed_ratio = (
                    closed_count / float(total_cells) if total_cells > 0 else 0.0
                )
                score = coverage * 0.6 + (1.0 - mean_dist) * 0.3 + closed_ratio * 0.1

                vis = image.copy()
                for r in range(1, rows):
                    ry = int(round(r * cell_h))
                    cv2.line(vis, (0, ry), (w, ry), (0, 200, 0), 1)
                for c in range(1, cols):
                    cx_cut = int(round(c * cell_w))
                    cv2.line(vis, (cx_cut, 0), (cx_cut, h), (0, 200, 0), 1)
                for cx, cy in centroids:
                    cv2.circle(vis, (int(cx), int(cy)), 4, (255, 0, 0), -1)
                if centroids:
                    pts = np.array(centroids)
                    if len(pts) >= 3:
                        hull = cv2.convexHull(pts.astype(np.int32))
                        hull_center = np.mean(hull.reshape(-1, 2), axis=0).astype(int)
                        cv2.drawContours(vis, [hull], -1, (0, 0, 255), 2)
                        cv2.circle(vis, tuple(hull_center), 6, (0, 0, 255), -1)
                    else:
                        avg = np.mean(pts, axis=0).astype(int)
                        cv2.circle(vis, tuple(avg), 6, (0, 0, 255), -1)

                candidates.append(
                    {
                        "rows": rows,
                        "cols": cols,
                        "score": float(score),
                        "coverage": float(coverage),
                        "mean_dist": float(mean_dist),
                        "closed_ratio": float(closed_ratio),
                        "vis": vis,
                    }
                )

        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates

    def _line_edge_sum(self, edge_map: np.ndarray, orientation: str, pos: int) -> int:
        """计算一条横/竖线上的边缘强度总和"""
        h, w = edge_map.shape[:2]
        if orientation == "h":
            pos = max(0, min(h - 1, int(round(pos))))
            return int(np.sum(edge_map[pos, :]))
        pos = max(0, min(w - 1, int(round(pos))))
        return int(np.sum(edge_map[:, pos]))

    def refine_grid_candidate(
        self,
        candidate: dict,
        edge_map: np.ndarray | None = None,
        extend_pct: float = 0.05,
    ) -> dict:
        """对单个候选网格做局部搜索微调"""
        if edge_map is None:
            vis_img = candidate.get("vis")
            edge_map = (
                self.compute_color_edge_mutation(vis_img)
                if vis_img is not None
                else None
            )
            if edge_map is None:
                raise ValueError("refine_grid_candidate requires an edge_map")

        rows = candidate["rows"]
        cols = candidate["cols"]
        h, w = edge_map.shape[:2]
        cell_h = float(h) / rows
        cell_w = float(w) / cols

        row_cuts = [0] + [int(round(r * cell_h)) for r in range(1, rows)] + [h]
        col_cuts = [0] + [int(round(c * cell_w)) for c in range(1, cols)] + [w]

        ext_row = max(2, int(extend_pct * h))
        ext_col = max(2, int(extend_pct * w))

        refined_row_cuts = [row_cuts[0]]
        for rc in row_cuts[1:-1]:
            best_pos = rc
            best_score = self._line_edge_sum(edge_map, "h", rc)
            for y in range(max(1, rc - ext_row), min(h - 2, rc + ext_row) + 1):
                s = self._line_edge_sum(edge_map, "h", y)
                if s < best_score:
                    best_score = s
                    best_pos = y
            refined_row_cuts.append(best_pos)
        refined_row_cuts.append(row_cuts[-1])

        refined_col_cuts = [col_cuts[0]]
        for cc in col_cuts[1:-1]:
            best_pos = cc
            best_score = self._line_edge_sum(edge_map, "v", cc)
            for x in range(max(1, cc - ext_col), min(w - 2, cc + ext_col) + 1):
                s = self._line_edge_sum(edge_map, "v", x)
                if s < best_score:
                    best_score = s
                    best_pos = x
            refined_col_cuts.append(best_pos)
        refined_col_cuts.append(col_cuts[-1])

        boxes = []
        for i in range(len(refined_row_cuts) - 1):
            for j in range(len(refined_col_cuts) - 1):
                x = refined_col_cuts[j]
                y = refined_row_cuts[i]
                box_w = refined_col_cuts[j + 1] - x
                box_h = refined_row_cuts[i + 1] - y
                boxes.append((x, y, box_w, box_h))

        row_edge_sums = [
            self._line_edge_sum(edge_map, "h", rc) for rc in refined_row_cuts[1:-1]
        ]
        col_edge_sums = [
            self._line_edge_sum(edge_map, "v", cc) for cc in refined_col_cuts[1:-1]
        ]
        total_edge_on_cuts = sum(row_edge_sums) + sum(col_edge_sums)
        denom = float(
            (len(refined_row_cuts) - 2) * w + (len(refined_col_cuts) - 2) * h + 1e-6
        )
        norm_penalty = float(total_edge_on_cuts) / denom

        all_sums = (
            np.array(row_edge_sums + col_edge_sums, dtype=float)
            if (row_edge_sums or col_edge_sums)
            else np.array([0.0])
        )
        mean_sum = float(np.mean(all_sums)) if all_sums.size else 0.0
        std_sum = float(np.std(all_sums)) if all_sums.size else 0.0
        rel_std = (std_sum / (mean_sum + 1e-6)) if mean_sum > 0 else 0.0
        penetration_flag = bool(
            all_sums.size > 0 and mean_sum > 0 and np.any(all_sums > mean_sum * 1.5)
        )
        non_uniform_penalty = min(
            1.0, rel_std * 0.7 + (0.2 if penetration_flag else 0.0)
        )

        base = candidate.get("score", 0.0)
        refined_score = (
            base * 0.55
            + (1.0 - norm_penalty) * 0.25
            + (1.0 - non_uniform_penalty) * 0.20
        )

        def fine_tune_cuts(r_cuts, c_cuts, edge_map):
            r_new = r_cuts.copy()
            c_new = c_cuts.copy()

            for idx in range(1, len(r_cuts) - 1):
                prev_y = r_cuts[idx - 1]
                cur_y = r_cuts[idx]
                next_y = r_cuts[idx + 1]
                gap = max(2, next_y - prev_y)
                delta = max(1, int(0.10 * gap))
                orig = self._line_edge_sum(edge_map, "h", cur_y)
                best_pos = cur_y
                best_val = orig
                for y in range(
                    max(prev_y + 1, cur_y - delta), min(next_y - 1, cur_y + delta) + 1
                ):
                    v = self._line_edge_sum(edge_map, "h", y)
                    if v < best_val:
                        best_val = v
                        best_pos = y
                improve = (orig - best_val) / (orig + 1e-9)
                move = abs(best_pos - cur_y)
                if best_pos != cur_y and (
                    improve > 0.05 or (move > 0 and (improve / move) >= 0.02)
                ):
                    r_new[idx] = best_pos

            for idx in range(1, len(c_cuts) - 1):
                prev_x = c_cuts[idx - 1]
                cur_x = c_cuts[idx]
                next_x = c_cuts[idx + 1]
                gap = max(2, next_x - prev_x)
                delta = max(1, int(0.10 * gap))
                orig = self._line_edge_sum(edge_map, "v", cur_x)
                best_pos = cur_x
                best_val = orig
                for x in range(
                    max(prev_x + 1, cur_x - delta), min(next_x - 1, cur_x + delta) + 1
                ):
                    v = self._line_edge_sum(edge_map, "v", x)
                    if v < best_val:
                        best_val = v
                        best_pos = x
                improve = (orig - best_val) / (orig + 1e-9)
                move = abs(best_pos - cur_x)
                if best_pos != cur_x and (
                    improve > 0.05 or (move > 0 and (improve / move) >= 0.02)
                ):
                    c_new[idx] = best_pos
            return r_new, c_new

        tuned_row_cuts, tuned_col_cuts = fine_tune_cuts(
            refined_row_cuts, refined_col_cuts, edge_map
        )
        row_edge_sums_tuned = [
            self._line_edge_sum(edge_map, "h", rc) for rc in tuned_row_cuts[1:-1]
        ]
        col_edge_sums_tuned = [
            self._line_edge_sum(edge_map, "v", cc) for cc in tuned_col_cuts[1:-1]
        ]
        total_edge_on_cuts_tuned = sum(row_edge_sums_tuned) + sum(col_edge_sums_tuned)
        denom_t = float(
            (len(tuned_row_cuts) - 2) * w + (len(tuned_col_cuts) - 2) * h + 1e-6
        )
        norm_penalty_tuned = float(total_edge_on_cuts_tuned) / denom_t

        per_box_sums = []
        for i in range(len(tuned_row_cuts) - 1):
            for j in range(len(tuned_col_cuts) - 1):
                x0 = tuned_col_cuts[j]
                x1 = tuned_col_cuts[j + 1]
                y0 = tuned_row_cuts[i]
                y1 = tuned_row_cuts[i + 1]
                region = edge_map[y0:y1, x0:x1]
                per_box_sums.append(float(np.sum(region)))
        mean_box = float(np.mean(per_box_sums)) if per_box_sums else 0.0
        std_box = float(np.std(per_box_sums)) if per_box_sums else 0.0
        non_uniform_penalty_tuned = min(1.0, (std_box / (mean_box + 1e-6)) * 0.7)

        refined_score_tuned = (
            base * 0.55
            + (1.0 - norm_penalty_tuned) * 0.25
            + (1.0 - non_uniform_penalty_tuned) * 0.20
        )
        if refined_score_tuned > refined_score + 1e-4:
            refined_score = refined_score_tuned
            refined_row_cuts = tuned_row_cuts
            refined_col_cuts = tuned_col_cuts
            row_edge_sums = row_edge_sums_tuned
            col_edge_sums = col_edge_sums_tuned

        vis = candidate.get("vis")
        if vis is None:
            vis = (
                cv2.cvtColor(edge_map, cv2.COLOR_GRAY2BGR)
                if len(edge_map.shape) == 2
                else edge_map.copy()
            )

        return {
            "rows": rows,
            "cols": cols,
            "refined_score": float(refined_score),
            "row_cuts": refined_row_cuts,
            "col_cuts": refined_col_cuts,
            "boxes": boxes,
            "vis": vis,
        }

    def select_and_refine_top(
        self, candidates: list[dict], edge_map: np.ndarray, top_n: int = 3
    ) -> list[dict]:
        """对前 top_n 候选进行微调并按 refined_score 排序"""
        refined_list = []
        for cand in candidates[:top_n]:
            refined_list.append(self.refine_grid_candidate(cand, edge_map=edge_map))
        refined_list.sort(key=lambda x: x["refined_score"], reverse=True)
        return refined_list

    def refine_boxes_by_similarity(
        self, boxes: list[tuple[int, int, int, int]], img_w: int, img_h: int
    ) -> list[tuple[int, int, int, int]]:
        """过滤异常框"""
        if not boxes:
            return []
        areas = [w * h for _, _, w, h in boxes]
        median_area = np.median(areas)
        valid_boxes: list[tuple[int, int, int, int]] = []
        for x, y, w, h in boxes:
            area = w * h
            if area < median_area * 0.1:
                continue
            if area > (img_w * img_h * 0.95):
                continue
            ratio = max(w, h) / max(1, min(w, h))
            if ratio > 10:
                continue
            valid_boxes.append((x, y, w, h))
        return valid_boxes

    def detect_grid(
        self, image: np.ndarray, debug: bool = False
    ) -> list[tuple[int, int, int, int]]:
        """主检测逻辑"""
        h, w = image.shape[:2]

        edge_map = self.compute_color_edge_mutation(image)

        grid_candidates = self.analyze_grid_variations(
            image, edge_map=edge_map, max_grid=8
        )

        grad_r = (
            cv2.Sobel(image[:, :, 0], cv2.CV_64F, 1, 0, ksize=3) ** 2
            + cv2.Sobel(image[:, :, 0], cv2.CV_64F, 0, 1, ksize=3) ** 2
        )
        grad_g = (
            cv2.Sobel(image[:, :, 1], cv2.CV_64F, 1, 0, ksize=3) ** 2
            + cv2.Sobel(image[:, :, 1], cv2.CV_64F, 0, 1, ksize=3) ** 2
        )
        grad_b = (
            cv2.Sobel(image[:, :, 2], cv2.CV_64F, 1, 0, ksize=3) ** 2
            + cv2.Sobel(image[:, :, 2], cv2.CV_64F, 0, 1, ksize=3) ** 2
        )
        energy = np.sqrt(grad_r + grad_g + grad_b)
        energy = cv2.convertScaleAbs(energy)

        row_cut_points = self.get_cut_points(np.sum(energy, axis=1))
        col_cut_points = self.get_cut_points(np.sum(energy, axis=0))
        if not row_cut_points or not col_cut_points:
            row_cut_points = self.get_cut_points(np.sum(edge_map, axis=1))
            col_cut_points = self.get_cut_points(np.sum(edge_map, axis=0))

        row_cuts = [0] + sorted(row_cut_points) + [h]
        col_cuts = [0] + sorted(col_cut_points) + [w]

        final_boxes = []
        for i in range(len(row_cuts) - 1):
            for j in range(len(col_cuts) - 1):
                x = col_cuts[j]
                y = row_cuts[i]
                final_boxes.append((x, y, col_cuts[j + 1] - x, row_cuts[i + 1] - y))

        refined_top3 = self.select_and_refine_top(
            grid_candidates, edge_map=edge_map, top_n=3
        )

        if refined_top3:
            best_ref = refined_top3[0]
            final_boxes = best_ref["boxes"]
            self.last_row_lines = best_ref.get("row_cuts", [])
            self.last_col_lines = best_ref.get("col_cuts", [])
        else:
            self.last_row_lines = row_cuts
            self.last_col_lines = col_cuts

        clean_boxes = self.refine_boxes_by_similarity(final_boxes, w, h)
        return clean_boxes


class AIMemeSplitter:
    """
    AI 辅助表情包切分器（集成版）
    接收 AI 识别的行列数后，按照行列智能优化网格并切图
    """

    def __init__(self, min_gap: int = 10, edge_threshold: int = 15):
        self.min_gap = min_gap
        self.edge_threshold = edge_threshold
        self.process_steps: dict[str, np.ndarray] = {}
        self.last_row_lines: list[int] = []
        self.last_col_lines: list[int] = []
        self.detected_rows = 0
        self.detected_cols = 0
        self.analysis_info = ""

    def dilate_diff(self, img: np.ndarray) -> np.ndarray:
        """膨胀差分提线稿，适合动漫风格"""
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if len(img.shape) == 3 else img
        kernel = np.ones((3, 3), np.uint8)
        dilated = cv2.dilate(gray, kernel, iterations=1)
        diff = cv2.absdiff(gray, dilated)
        result = 255 - diff
        _, result = cv2.threshold(result, 230, 255, cv2.THRESH_BINARY)
        return result

    def post_process(self, lineart: np.ndarray, threshold: int = 50) -> np.ndarray:
        """去除小连通域杂线"""
        binary = 255 - lineart
        num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
            binary, connectivity=8
        )
        new_mask = np.zeros_like(binary)
        for i in range(1, num_labels):
            area = stats[i, cv2.CC_STAT_AREA]
            if area >= threshold:
                new_mask[labels == i] = 255
        return 255 - new_mask

    def _optimize_grid_positions(
        self,
        initial_cuts: list[int],
        proj_values: np.ndarray,
        length: int,
        axis_name: str,
    ) -> list[int]:
        """在保证均匀性的前提下微调切割线，避开内容"""
        if len(initial_cuts) <= 2:
            return initial_cuts

        optimized_cuts = [initial_cuts[0]]
        for i in range(1, len(initial_cuts) - 1):
            current_pos = initial_cuts[i]
            ideal_interval = length / (len(initial_cuts) - 1)
            ideal_pos = int(i * ideal_interval)
            search_radius = min(int(ideal_interval * 0.3), 20)
            search_start = max(0, ideal_pos - search_radius)
            search_end = min(length, ideal_pos + search_radius)

            best_pos = current_pos
            best_score = float("-inf")
            for test_pos in range(search_start, search_end):
                gap_score = 1.0 / (1.0 + proj_values[test_pos])
                if i == 1:
                    prev_interval = test_pos - optimized_cuts[0]
                    next_interval = ideal_interval
                else:
                    prev_interval = test_pos - optimized_cuts[-1]
                    next_interval = ideal_interval
                intervals = [prev_interval, next_interval]
                mean_interval = np.mean(intervals)
                std_interval = np.std(intervals)
                uniformity_score = (
                    1.0 / (1.0 + std_interval / mean_interval)
                    if mean_interval > 0
                    else 0
                )
                distance_penalty = 1.0 / (
                    1.0 + abs(test_pos - ideal_pos) / search_radius
                )
                total_score = (
                    0.6 * uniformity_score + 0.3 * gap_score + 0.1 * distance_penalty
                )
                if total_score > best_score:
                    best_score = total_score
                    best_pos = test_pos

            optimized_cuts.append(best_pos)
            logger.debug(
                f"[{axis_name}] 位置{i}: {current_pos} -> {best_pos} (偏移{best_pos - current_pos})"
            )

        optimized_cuts.append(initial_cuts[-1])
        return optimized_cuts

    def _solve_axis(
        self,
        gap_proj: np.ndarray,
        struct_proj: np.ndarray,
        length: int,
        axis_name: str,
        manual_n: int,
    ) -> list[int]:
        """根据目标行/列数求切割线"""
        max_gap = np.max(gap_proj)
        norm_gap = gap_proj / max_gap if max_gap > 0 else gap_proj
        max_struct = np.max(struct_proj)
        norm_struct = struct_proj / max_struct if max_struct > 0 else struct_proj

        sorted_vals = np.sort(norm_gap)
        baseline = np.mean(sorted_vals[: int(length * 0.1) + 1])
        safe_threshold = baseline + 0.25

        best_score = -float("inf")
        best_cuts = [0, length]
        n = manual_n
        if n == 1:
            return [0, length]
        step = length / n

        modes = []
        gap_cuts = [0]
        gap_vals = []
        gap_displacements = []
        valid_gap = True
        for k in range(1, n):
            ideal = int(k * step)
            radius = int(step * 0.25)
            start = max(0, ideal - radius)
            end = min(length, ideal + radius)
            window = norm_gap[start:end]
            if len(window) == 0:
                valid_gap = False
                break
            idx = np.argmin(window)
            pos = start + idx
            val = window[idx]
            if val > safe_threshold * 1.5:
                valid_gap = False
                break
            gap_cuts.append(pos)
            gap_vals.append(val)
            gap_displacements.append(abs(pos - ideal))
        if valid_gap:
            gap_cuts.append(length)
            modes.append(("Gap", gap_cuts, gap_vals, gap_displacements))

        if max_struct > 0:
            struct_cuts = [0]
            struct_vals = []
            struct_displacements = []
            valid_struct = True
            for k in range(1, n):
                ideal = int(k * step)
                radius = int(step * 0.2)
                start = max(0, ideal - radius)
                end = min(length, ideal + radius)
                window = norm_struct[start:end]
                if len(window) == 0:
                    valid_struct = False
                    break
                idx = np.argmax(window)
                pos = start + idx
                val = window[idx]
                if val < 0.2:
                    valid_struct = False
                    break
                struct_cuts.append(pos)
                struct_vals.append(1.0 - val)
                struct_displacements.append(abs(pos - ideal))
            if valid_struct:
                struct_cuts.append(length)
                modes.append(("Struct", struct_cuts, struct_vals, struct_displacements))

        for mode_name, cuts, vals, displacements in modes:
            intervals = np.diff(cuts)
            mean_interval = np.mean(intervals)
            std_interval = np.std(intervals)
            cv = std_interval / mean_interval if mean_interval > 0 else 0
            score_uniformity = max(0, 1.0 - (cv / 0.3))
            avg_val = np.mean(vals)
            score_safety = max(0, 1.0 - avg_val)
            avg_disp = np.mean(displacements)
            max_disp = step * 0.3
            score_displacement = max(0, 1.0 - (avg_disp / max_disp))
            final_score = (
                0.5 * score_uniformity
                + 0.3 * score_safety
                + 0.2 * score_displacement
                + n * 0.05
            )
            if final_score > best_score:
                best_score = final_score
                best_cuts = cuts

        if len(best_cuts) > 2:
            optimized_cuts = self._optimize_grid_positions(
                best_cuts, norm_gap, length, axis_name
            )
            old_intervals = np.diff(best_cuts)
            new_intervals = np.diff(optimized_cuts)
            old_cv = (
                np.std(old_intervals) / np.mean(old_intervals)
                if np.mean(old_intervals) > 0
                else float("inf")
            )
            new_cv = (
                np.std(new_intervals) / np.mean(new_intervals)
                if np.mean(new_intervals) > 0
                else float("inf")
            )
            if new_cv <= old_cv * 1.1:
                best_cuts = optimized_cuts

        return best_cuts

    def detect_grid(
        self, lineart: np.ndarray, target_rows: int, target_cols: int
    ) -> tuple[list[int], list[int]]:
        """基于目标行列检测网格线"""
        h, w = lineart.shape
        edges = 255 - lineart

        k_w = max(3, w // 5)
        if k_w % 2 == 0:
            k_w += 1
        h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (k_w, 1))
        h_struct = cv2.morphologyEx(edges, cv2.MORPH_OPEN, h_kernel)
        h_struct_proj = np.sum(h_struct, axis=1)

        k_h = max(3, h // 5)
        if k_h % 2 == 0:
            k_h += 1
        v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, k_h))
        v_struct = cv2.morphologyEx(edges, cv2.MORPH_OPEN, v_kernel)
        v_struct_proj = np.sum(v_struct, axis=0)

        kernel_size = 3
        row_proj = np.convolve(
            np.sum(edges, axis=1), np.ones(kernel_size) / kernel_size, mode="same"
        )
        col_proj = np.convolve(
            np.sum(edges, axis=0), np.ones(kernel_size) / kernel_size, mode="same"
        )

        h_lines = self._solve_axis(row_proj, h_struct_proj, h, "水平", target_rows)
        v_lines = self._solve_axis(col_proj, v_struct_proj, w, "垂直", target_cols)
        logger.debug(f"最终网格: {len(h_lines) - 1}行 x {len(v_lines) - 1}列")
        return h_lines, v_lines

    def split(
        self,
        image_path: str,
        output_dir: str,
        rows: int,
        cols: int,
        debug: bool = False,
        file_prefix: str | None = None,
        base_image: np.ndarray | None = None,
    ) -> list[str]:
        """根据指定行列切分图像"""
        img_original = base_image if base_image is not None else cv2.imread(image_path)
        if img_original is None:
            raise ValueError(f"无法读取图像: {image_path}")

        self.process_steps["1_original"] = img_original.copy()

        if debug:
            logger.debug("正在提取线稿...")
        img_lineart = self.dilate_diff(img_original)
        self.process_steps["2_lineart"] = img_lineart.copy()

        if debug:
            logger.debug("正在去除杂线...")
        img_clean = self.post_process(img_lineart, threshold=50)
        self.process_steps["3_clean"] = img_clean.copy()

        if debug:
            logger.debug(f"正在按 {rows}行 x {cols}列 检测网格...")
        h_lines, v_lines = self.detect_grid(img_clean, rows, cols)
        self.last_row_lines = h_lines
        self.last_col_lines = v_lines

        boxes: list[tuple[int, int, int, int]] = []
        centers: list[tuple[int, int, int, int]] = []
        if len(h_lines) >= 2 and len(v_lines) >= 2:
            for i in range(len(h_lines) - 1):
                y1, y2 = h_lines[i], h_lines[i + 1]
                cy = (y1 + y2) // 2
                for j in range(len(v_lines) - 1):
                    x1, x2 = v_lines[j], v_lines[j + 1]
                    cx = (x1 + x2) // 2
                    w_box = x2 - x1
                    h_box = y2 - y1
                    centers.append((cx, cy, w_box, h_box))

        for cx, cy, w_box, h_box in centers:
            x1 = max(0, cx - w_box // 2)
            y1 = max(0, cy - h_box // 2)
            x2 = min(img_original.shape[1], x1 + w_box)
            y2 = min(img_original.shape[0], y1 + h_box)
            if x2 - x1 > 20 and y2 - y1 > 20:
                boxes.append((x1, y1, x2 - x1, y2 - y1))

        if not boxes:
            logger.debug("未检测到有效的表情包区域")
            return []

        Path(output_dir).mkdir(parents=True, exist_ok=True)
        saved_files: list[str] = []
        for idx, (x, y, w_box, h_box) in enumerate(boxes, 1):
            crop = img_original[y : y + h_box, x : x + w_box]
            prefix = file_prefix or "meme"
            filename = f"{prefix}_{idx:03d}.png"
            filepath = Path(output_dir) / filename
            cv2.imwrite(str(filepath), crop)
            saved_files.append(str(filepath))

        if debug:
            logger.debug(f"成功保存 {len(saved_files)} 个表情包到 {output_dir}")
        return saved_files


def ai_split_with_rows_cols(
    image_path: str,
    rows: int,
    cols: int,
    output_dir: Path,
    file_prefix: str,
    base_image: np.ndarray,
) -> list[str]:
    """基于指定行列切分，异常返回空列表"""
    try:
        splitter = AIMemeSplitter(min_gap=5, edge_threshold=10)
        files = splitter.split(
            image_path,
            str(output_dir),
            rows=rows,
            cols=cols,
            debug=False,
            file_prefix=file_prefix,
            base_image=base_image,
        )
        return files or []
    except Exception as e:
        logger.debug(f"AI 行列切分失败: {e}")
        return []


def _build_grid_debug_image(
    image: np.ndarray,
    boxes: list[tuple[int, int, int, int]],
    row_lines: list[int] | None = None,
    col_lines: list[int] | None = None,
) -> np.ndarray:
    """为网格类切分生成调试图，保留切割线与最终框。"""
    vis = image.copy()
    if vis.ndim == 2:
        vis = cv2.cvtColor(vis, cv2.COLOR_GRAY2BGR)

    if row_lines:
        for y in row_lines[1:-1]:
            cv2.line(
                vis,
                (0, int(y)),
                (vis.shape[1] - 1, int(y)),
                (64, 192, 64),
                2,
                lineType=cv2.LINE_AA,
            )
    if col_lines:
        for x in col_lines[1:-1]:
            cv2.line(
                vis,
                (int(x), 0),
                (int(x), vis.shape[0] - 1),
                (64, 192, 64),
                2,
                lineType=cv2.LINE_AA,
            )

    for idx, (x, y, w_box, h_box) in enumerate(boxes, 1):
        cv2.rectangle(
            vis,
            (int(x), int(y)),
            (int(x + w_box - 1), int(y + h_box - 1)),
            (48, 64, 255),
            2,
            lineType=cv2.LINE_AA,
        )
        cv2.putText(
            vis,
            str(idx),
            (int(x) + 8, int(y) + 28),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (48, 64, 255),
            2,
            lineType=cv2.LINE_AA,
        )
    return vis


def _collect_projection_bands(
    projection: np.ndarray,
    threshold: float,
    *,
    edge_margin: int = 0,
) -> list[tuple[int, int, int, float]]:
    """从一维投影里提取连续峰带，返回 start/end/center/score。"""
    if projection.size == 0:
        return []

    mask = projection >= threshold
    bands: list[tuple[int, int, int, float]] = []
    start: int | None = None

    for idx, flag in enumerate(mask):
        if flag and start is None:
            start = idx
        elif not flag and start is not None:
            end = idx
            peak_rel = int(np.argmax(projection[start:end]))
            center = start + peak_rel
            if edge_margin <= center < len(projection) - edge_margin:
                bands.append((start, end - 1, center, float(projection[center])))
            start = None

    if start is not None:
        end = len(mask)
        peak_rel = int(np.argmax(projection[start:end]))
        center = start + peak_rel
        if edge_margin <= center < len(projection) - edge_margin:
            bands.append((start, end - 1, center, float(projection[center])))

    return bands


def _detect_solid_grid_boxes(
    source_img: np.ndarray,
    *,
    min_rows: int = 2,
    min_cols: int = 2,
    max_rows: int = 6,
    max_cols: int = 6,
) -> tuple[list[tuple[int, int, int, int]], list[int], list[int], np.ndarray] | None:
    """检测带黑色实线分隔的规则网格图。"""
    if source_img.ndim != 3:
        return None

    h, w = source_img.shape[:2]
    if h < 200 or w < 200:
        return None

    gray = cv2.cvtColor(source_img[:, :, :3], cv2.COLOR_BGR2GRAY)
    border_samples = np.concatenate(
        [gray[0, :], gray[-1, :], gray[:, 0], gray[:, -1]]
    ).astype(np.float32)
    bg_level = float(np.quantile(border_samples, 0.75))
    ink_level = float(np.quantile(gray.astype(np.float32), 0.08))
    contrast = max(0.0, bg_level - ink_level)
    white_ratio = float((gray >= bg_level - max(8.0, contrast * 0.08)).mean())
    if white_ratio < 0.22 or contrast < 48.0:
        return None

    dark_thr = bg_level - max(10.0, contrast * 0.58)
    dark = ((gray <= dark_thr) * 255).astype(np.uint8)
    h_kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT,
        (max(31, w // 4), 1),
    )
    v_kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT,
        (1, max(31, h // 4)),
    )
    hori = cv2.morphologyEx(dark, cv2.MORPH_OPEN, h_kernel)
    vert = cv2.morphologyEx(dark, cv2.MORPH_OPEN, v_kernel)

    hproj = hori.mean(axis=1).astype(np.float32)
    vproj = vert.mean(axis=0).astype(np.float32)

    h_pos = hproj[hproj > 0]
    v_pos = vproj[vproj > 0]
    if len(h_pos) == 0 or len(v_pos) == 0:
        return None

    h_support = float(len(h_pos)) / float(len(hproj))
    v_support = float(len(v_pos)) / float(len(vproj))
    h_q = min(0.999, max(0.9, 1.0 - h_support * 0.75))
    v_q = min(0.999, max(0.9, 1.0 - v_support * 0.75))
    h_thresh = max(float(hproj.max()) * 0.5, float(np.quantile(hproj, h_q)))
    v_thresh = max(float(vproj.max()) * 0.5, float(np.quantile(vproj, v_q)))
    hbands = _collect_projection_bands(
        hproj,
        h_thresh,
    )
    vbands = _collect_projection_bands(
        vproj,
        v_thresh,
    )

    edge_margin_y = max(4, int(round(h * 0.02)))
    edge_margin_x = max(4, int(round(w * 0.02)))
    hbands = [band for band in hbands if edge_margin_y < band[2] < h - edge_margin_y]
    vbands = [band for band in vbands if edge_margin_x < band[2] < w - edge_margin_x]
    hbands = sorted(hbands, key=lambda item: item[2])
    vbands = sorted(vbands, key=lambda item: item[2])

    row_count = len(hbands) + 1
    col_count = len(vbands) + 1
    if row_count < min_rows or col_count < min_cols:
        return None
    if row_count > max_rows or col_count > max_cols:
        return None

    row_lines = [0, *[band[2] for band in hbands], h]
    col_lines = [0, *[band[2] for band in vbands], w]
    row_heights = np.diff(np.array(row_lines, dtype=np.float32))
    col_widths = np.diff(np.array(col_lines, dtype=np.float32))
    if np.min(row_heights) < h * 0.08 or np.min(col_widths) < w * 0.08:
        return None
    row_cv = float(np.std(row_heights) / (np.mean(row_heights) + 1e-6))
    col_cv = float(np.std(col_widths) / (np.mean(col_widths) + 1e-6))
    allowed_cv = 0.1 + 0.08 / max(1, min(row_count, col_count) - 1)
    if row_cv > allowed_cv or col_cv > allowed_cv:
        return None

    median_h_band = float(
        np.median([band_end - band_start + 1 for band_start, band_end, _, _ in hbands])
    )
    median_v_band = float(
        np.median([band_end - band_start + 1 for band_start, band_end, _, _ in vbands])
    )
    band_pad_y = max(1, int(round(median_h_band * 0.35)))
    band_pad_x = max(1, int(round(median_v_band * 0.35)))

    row_spans: list[tuple[int, int]] = []
    prev_end = 0
    for band_start, band_end, _, _ in hbands:
        safe_start = max(prev_end, band_start - band_pad_y)
        row_spans.append((prev_end, safe_start))
        prev_end = min(h, band_end + 1 + band_pad_y)
    row_spans.append((prev_end, h))

    col_spans: list[tuple[int, int]] = []
    prev_end = 0
    for band_start, band_end, _, _ in vbands:
        safe_start = max(prev_end, band_start - band_pad_x)
        col_spans.append((prev_end, safe_start))
        prev_end = min(w, band_end + 1 + band_pad_x)
    col_spans.append((prev_end, w))

    if any((y2 - y1) <= h * 0.06 for y1, y2 in row_spans):
        return None
    if any((x2 - x1) <= w * 0.06 for x1, x2 in col_spans):
        return None

    boxes: list[tuple[int, int, int, int]] = []
    for y1, y2 in row_spans:
        for x1, x2 in col_spans:
            boxes.append((int(x1), int(y1), int(x2 - x1), int(y2 - y1)))

    if len(boxes) != row_count * col_count:
        return None

    return (
        boxes,
        row_lines,
        col_lines,
        _build_grid_debug_image(
            source_img,
            boxes,
            row_lines,
            col_lines,
        ),
    )


def _detect_dashed_grid_boxes(
    source_img: np.ndarray,
    *,
    target_rows: int = 4,
    target_cols: int = 4,
) -> tuple[list[tuple[int, int, int, int]], list[int], list[int], np.ndarray] | None:
    """检测带黑色虚线分隔的规则表情包大图。"""
    if source_img.ndim != 3:
        return None

    h, w = source_img.shape[:2]
    if h < 240 or w < 600 or target_rows < 2 or target_cols < 2:
        return None

    gray = cv2.cvtColor(source_img[:, :, :3], cv2.COLOR_BGR2GRAY)
    if float((gray >= 245).mean()) < 0.45:
        return None

    dark = ((gray < 200) * 255).astype(np.uint8)

    close_y = max(9, h // 64)
    close_x = max(9, w // 96)
    vert = cv2.morphologyEx(
        dark,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (3, close_y)),
    )
    vert = cv2.morphologyEx(
        vert,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (3, max(31, h // 2))),
    )
    hori = cv2.morphologyEx(
        dark,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (close_x, 3)),
    )
    hori = cv2.morphologyEx(
        hori,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (max(31, w // 2), 3)),
    )

    vproj = vert.mean(axis=0).astype(np.float32)
    hproj = hori.mean(axis=1).astype(np.float32)
    v_thresh = max(140.0, float(vproj.max()) * 0.65)
    h_thresh = max(140.0, float(hproj.max()) * 0.65)

    vbands = _collect_projection_bands(
        vproj,
        v_thresh,
        edge_margin=max(24, int(w * 0.05)),
    )
    hbands = _collect_projection_bands(
        hproj,
        h_thresh,
        edge_margin=max(24, int(h * 0.05)),
    )
    if len(vbands) != target_cols - 1 or len(hbands) != target_rows - 1:
        return None

    vbands = sorted(vbands, key=lambda item: item[2])
    hbands = sorted(hbands, key=lambda item: item[2])
    vcenters = [band[2] for band in vbands]
    hcenters = [band[2] for band in hbands]
    vintervals = np.diff(np.array(vcenters, dtype=np.float32))
    hintervals = np.diff(np.array(hcenters, dtype=np.float32))
    if len(vintervals) != target_cols - 2 or len(hintervals) != target_rows - 2:
        return None
    if (
        float(np.std(vintervals) / (np.mean(vintervals) + 1e-6)) > 0.12
        or float(np.std(hintervals) / (np.mean(hintervals) + 1e-6)) > 0.12
    ):
        return None
    if np.min(vintervals) < w * 0.12 or np.min(hintervals) < h * 0.12:
        return None

    band_pad_y = max(1, min(4, h // 320))
    band_pad_x = max(1, min(4, w // 480))

    row_spans: list[tuple[int, int]] = []
    prev_end = 0
    for band_start, band_end, _, _ in hbands:
        safe_start = max(prev_end, band_start - band_pad_y)
        row_spans.append((prev_end, safe_start))
        prev_end = min(h, band_end + 1 + band_pad_y)
    row_spans.append((prev_end, h))

    col_spans: list[tuple[int, int]] = []
    prev_end = 0
    for band_start, band_end, _, _ in vbands:
        safe_start = max(prev_end, band_start - band_pad_x)
        col_spans.append((prev_end, safe_start))
        prev_end = min(w, band_end + 1 + band_pad_x)
    col_spans.append((prev_end, w))

    if len(row_spans) != target_rows or len(col_spans) != target_cols:
        return None
    if any(y2 - y1 <= 20 for y1, y2 in row_spans):
        return None
    if any(x2 - x1 <= 20 for x1, x2 in col_spans):
        return None

    row_lines = [0, *hcenters, h]
    col_lines = [0, *vcenters, w]
    boxes: list[tuple[int, int, int, int]] = []
    for y1, y2 in row_spans:
        for x1, x2 in col_spans:
            if x2 - x1 > 20 and y2 - y1 > 20:
                boxes.append((int(x1), int(y1), int(x2 - x1), int(y2 - y1)))
    if len(boxes) != target_rows * target_cols:
        return None

    return (
        boxes,
        row_lines,
        col_lines,
        _build_grid_debug_image(
            source_img,
            boxes,
            row_lines,
            col_lines,
        ),
    )


async def resolve_split_source_to_path(
    source: str,
    *,
    image_input_mode: str = "force_base64",
    api_client=None,
    download_qq_image_fn=None,
    logger_obj=logger,
) -> str | None:
    """
    将切图指令收到的图片源统一解析为本地文件路径，内部复用 tl_utils 逻辑。

    Args:
        source: 图片源（URL/文件/base64/data URL）
        image_input_mode: 图片输入模式，统一转为 base64
        api_client: 用于 normalize 的 API 客户端
        download_qq_image_fn: 处理 qpic 等直链的下载函数
        logger_obj: 日志对象
    """
    return await resolve_image_source_to_path(
        source,
        image_input_mode=image_input_mode,
        api_client=api_client,
        download_qq_image_fn=download_qq_image_fn,
        is_valid_checker=is_valid_base64_image_str,
        logger_obj=logger_obj,
    )


def split_image(
    image_path: str,
    rows: int = 6,
    cols: int = 4,
    output_dir: str | None = None,
    bboxes: list[dict[str, Any]] | None = None,
    manual_rows: int | None = None,
    manual_cols: int | None = None,
    use_sticker_cutter: bool = False,
    ai_rows: int | None = None,
    ai_cols: int | None = None,
) -> list[str]:
    """
    智能切分图片。

    Args:
        image_path: 源图片路径
        rows: 保留参数以兼容旧接口（网格切分默认值）
        cols: 保留参数以兼容旧接口（网格切分默认值）
        output_dir: 输出目录，如果不指定则使用插件数据目录下的 split_output
        bboxes: 外部提供的裁剪框（x,y,width,height），优先使用
        manual_rows: 手动指定的纵向切割数（行数）
        manual_cols: 手动指定的横向切割数（列数）
        use_sticker_cutter: 兼容旧接口的开关名，当前默认也会优先尝试黑描边自适应切分

    Returns:
        List[str]: 切分后的图片文件路径列表，按顺序排列
    """
    try:
        # 如果未指定输出目录，则使用插件的标准数据目录
        if not output_dir:
            data_dir = get_plugin_data_dir()
            output_dir_path = data_dir / "split_output"
        else:
            output_dir_path = Path(output_dir)

        # 获取源文件名（不含扩展名和路径）作为子目录，避免文件混淆
        base_name = Path(image_path).stem
        # 最终存储目录: .../split_output/base_name/
        final_output_dir = output_dir_path / base_name
        final_output_dir.mkdir(parents=True, exist_ok=True)
        output_files = []

        img = cv2.imread(image_path, cv2.IMREAD_UNCHANGED)
        if img is None:
            logger.error(f"无法读取图像: {image_path}")
            return []

        def trim_viewer_chrome(source_img: np.ndarray) -> np.ndarray:
            """裁掉截图中明显的看图器边框/留白，避免退化成机械等分。"""
            if source_img.ndim != 3 or source_img.shape[0] < 200:
                return source_img

            gray = cv2.cvtColor(source_img[:, :, :3], cv2.COLOR_BGR2GRAY)
            h, w = gray.shape
            white_ratio = (gray >= 245).mean(axis=1)
            content_rows = np.where(white_ratio > 0.18)[0]
            if len(content_rows) == 0:
                return source_img

            y0 = int(content_rows.min())
            y1 = int(content_rows.max()) + 1

            # 仅在上下确实存在较厚非内容条带时才裁，避免误伤正常图。
            if y0 < max(24, int(h * 0.02)) and (h - y1) < max(24, int(h * 0.02)):
                return source_img

            if y0 <= 0 and y1 >= h:
                return source_img

            if y0 >= int(h * 0.18) or (h - y1) >= int(h * 0.18):
                return source_img

            trimmed = source_img[y0:y1].copy()
            x0 = 0
            x1 = trimmed.shape[1]

            trimmed_gray = cv2.cvtColor(trimmed[:, :, :3], cv2.COLOR_BGR2GRAY)
            col_white_ratio = (trimmed_gray >= 245).mean(axis=0)
            non_blank_cols = np.where(col_white_ratio < 0.999)[0]
            if len(non_blank_cols) > 0:
                x0 = int(non_blank_cols.min())
                x1 = int(non_blank_cols.max()) + 1
                left_blank = x0
                right_blank = trimmed_gray.shape[1] - x1
                if (
                    left_blank >= max(20, int(trimmed_gray.shape[1] * 0.015))
                    and right_blank >= max(20, int(trimmed_gray.shape[1] * 0.015))
                    and left_blank <= int(trimmed_gray.shape[1] * 0.08)
                    and right_blank <= int(trimmed_gray.shape[1] * 0.08)
                ):
                    trimmed = trimmed[:, x0:x1].copy()
                else:
                    x0 = 0
                    x1 = trimmed_gray.shape[1]
            logger.debug(
                "检测到截图边框，自动裁边: "
                f"x0={x0}, x1={x1}, y0={y0}, y1={y1}, 原始尺寸={w}x{h}, "
                f"新尺寸={trimmed.shape[1]}x{trimmed.shape[0]}"
            )
            return trimmed

        img = trim_viewer_chrome(img)

        sticker_crops: list[np.ndarray] | None = None
        sticker_debug: np.ndarray | None = None
        grid_debug: np.ndarray | None = None
        grid_row_lines: list[int] = []
        grid_col_lines: list[int] = []

        def estimate_global_background_color(source_img: np.ndarray) -> np.ndarray:
            """从整张源图估计统一背景色，供所有切片共用。"""
            if source_img.ndim == 2:
                gray_val = float(np.median(source_img))
                return np.array([gray_val, gray_val, gray_val], dtype=np.float32)

            bgr = source_img[:, :, :3].astype(np.float32)
            gray = cv2.cvtColor(source_img[:, :, :3], cv2.COLOR_BGR2GRAY)
            h, w = gray.shape

            border = np.zeros((h, w), dtype=bool)
            border[0, :] = True
            border[-1, :] = True
            border[:, 0] = True
            border[:, -1] = True

            border_pixels = bgr[border]
            border_gray = gray[border]
            near_white = border_pixels[border_gray >= 245]
            if len(near_white) > 0:
                return np.median(near_white, axis=0)

            if len(border_pixels) > 0:
                return np.median(border_pixels, axis=0)
            return np.array([255.0, 255.0, 255.0], dtype=np.float32)

        global_bg_color = estimate_global_background_color(img)

        def flatten_crop_background(
            crop: np.ndarray,
            bg_color: np.ndarray,
        ) -> np.ndarray:
            """将带 alpha 的切片按统一背景色回填为不透明结果。"""
            if crop.ndim != 3 or crop.shape[2] != 4:
                return crop

            alpha = crop[:, :, 3].astype(np.float32) / 255.0
            rgb = crop[:, :, :3].astype(np.float32)

            alpha_3 = alpha[:, :, None]
            composed = rgb * alpha_3 + bg_color[None, None, :] * (1.0 - alpha_3)
            return np.clip(composed, 0, 255).astype(np.uint8)

        def generate_manual_boxes(
            target_rows: int, target_cols: int
        ) -> list[tuple[int, int, int, int]]:
            """基于等分网格生成裁剪框"""
            if target_rows <= 0 or target_cols <= 0:
                return []

            h, w = img.shape[:2]
            row_edges = np.linspace(0, h, target_rows + 1, dtype=int)
            col_edges = np.linspace(0, w, target_cols + 1, dtype=int)

            manual_boxes: list[tuple[int, int, int, int]] = []
            for i in range(target_rows):
                for j in range(target_cols):
                    y1, y2 = row_edges[i], row_edges[i + 1]
                    x1, x2 = col_edges[j], col_edges[j + 1]
                    box_w, box_h = x2 - x1, y2 - y1
                    if box_w > 0 and box_h > 0:
                        manual_boxes.append((x1, y1, box_w, box_h))
            return manual_boxes

        def looks_like_dense_sticker_sheet(source_img: np.ndarray) -> bool:
            """判断是否像规则表情包大图，适合优先尝试 4x4 网格。"""
            h, w = source_img.shape[:2]
            if h < 600 or w < 900:
                return False

            gray = cv2.cvtColor(source_img[:, :, :3], cv2.COLOR_BGR2GRAY)
            dark = gray < 220
            row_dark = dark.mean(axis=1)
            col_dark = dark.mean(axis=0)
            strong_rows = int(np.count_nonzero(row_dark > 0.12))
            strong_cols = int(np.count_nonzero(col_dark > 0.12))
            return strong_rows >= 2 and strong_cols >= 3

        def looks_like_explicit_grid_sheet(
            source_img: np.ndarray,
            detected_boxes: list[tuple[int, int, int, int]],
            row_lines: list[int],
            col_lines: list[int],
        ) -> bool:
            """判断图像是否是带明显分隔线的规则网格表情包。"""
            if len(detected_boxes) < 9 or len(row_lines) < 4 or len(col_lines) < 4:
                return False

            widths = np.diff(np.array(col_lines, dtype=np.float32))
            heights = np.diff(np.array(row_lines, dtype=np.float32))
            areas = np.array([w * h for _, _, w, h in detected_boxes], dtype=np.float32)
            if len(widths) == 0 or len(heights) == 0 or len(areas) == 0:
                return False

            width_cv = float(np.std(widths) / (np.mean(widths) + 1e-6))
            height_cv = float(np.std(heights) / (np.mean(heights) + 1e-6))
            area_cv = float(np.std(areas) / (np.mean(areas) + 1e-6))
            if width_cv > 0.25 or height_cv > 0.2 or area_cv > 0.25:
                return False

            gray = cv2.cvtColor(source_img[:, :, :3], cv2.COLOR_BGR2GRAY)
            dark = gray < 220
            line_scores: list[float] = []
            for y in row_lines[1:-1]:
                y0 = max(0, y - 1)
                y1 = min(gray.shape[0], y + 2)
                line_scores.append(float(dark[y0:y1, :].mean()))
            for x in col_lines[1:-1]:
                x0 = max(0, x - 1)
                x1 = min(gray.shape[1], x + 2)
                line_scores.append(float(dark[:, x0:x1].mean()))

            return bool(line_scores) and max(line_scores) < 0.02

        # 若传入外部裁剪框则优先使用，避免重复跑智能切分
        boxes: list[tuple[int, int, int, int]] = []
        if bboxes:
            h, w = img.shape[:2]
            for box in bboxes:
                try:
                    x = int(box.get("x", 0)) if isinstance(box, dict) else int(box[0])
                    y = int(box.get("y", 0)) if isinstance(box, dict) else int(box[1])
                    bw = (
                        int(box.get("width", 0))
                        if isinstance(box, dict)
                        else int(box[2])
                    )
                    bh = (
                        int(box.get("height", 0))
                        if isinstance(box, dict)
                        else int(box[3])
                    )
                except Exception as e:
                    logger.debug(f"外部裁剪框解析失败，跳过: {e}")
                    continue

                x = max(0, x)
                y = max(0, y)
                bw = min(bw, w - x)
                bh = min(bh, h - y)
                if bw > 0 and bh > 0:
                    boxes.append((x, y, bw, bh))

            if boxes:
                logger.debug(f"使用外部提供的裁剪框，共 {len(boxes)} 个")

        precomputed_grid_boxes: list[tuple[int, int, int, int]] = []
        if not boxes and not (manual_rows and manual_cols):
            grid_splitter = SmartMemeSplitter()
            precomputed_grid_boxes = grid_splitter.detect_grid(img, debug=True)
            if looks_like_explicit_grid_sheet(
                img,
                precomputed_grid_boxes,
                grid_splitter.last_row_lines,
                grid_splitter.last_col_lines,
            ):
                boxes = precomputed_grid_boxes
                logger.debug(
                    f"检测到显式网格表情包，优先使用网格切分，共 {len(boxes)} 个裁剪框"
                )

        if not boxes:
            solid_grid = _detect_solid_grid_boxes(img)
            if solid_grid is not None:
                boxes, grid_row_lines, grid_col_lines, grid_debug = solid_grid
                logger.debug(
                    f"检测到黑色实线规则表情包，优先使用显式网格切分，共 {len(boxes)} 个裁剪框"
                )

        # 手动切割优先级：外部裁剪框 > 手动指定 > AI > 智能切分
        if not boxes and manual_rows and manual_cols:
            boxes = generate_manual_boxes(manual_rows, manual_cols)
            if boxes:
                logger.debug(f"使用手动网格裁剪: {manual_cols} x {manual_rows}")

        if not boxes and looks_like_dense_sticker_sheet(img):
            dashed_grid = _detect_dashed_grid_boxes(img, target_rows=4, target_cols=4)
            if dashed_grid is not None:
                boxes, grid_row_lines, grid_col_lines, grid_debug = dashed_grid
                logger.debug("检测到黑色虚线规则表情包，优先使用虚线 4x4 网格切分")
            else:
                ai_files = ai_split_with_rows_cols(
                    image_path,
                    4,
                    4,
                    final_output_dir,
                    base_name,
                    img,
                )
                if ai_files:
                    logger.debug("检测到规则表情包大图，优先使用智能 4x4 网格切分")
                    return ai_files

        # AI 行列切割（可选），在没有手动网格时尝试
        if not boxes and ai_rows and ai_cols and ai_rows > 0 and ai_cols > 0:
            ai_files = ai_split_with_rows_cols(
                image_path, ai_rows, ai_cols, final_output_dir, base_name, img
            )
            if ai_files:
                return ai_files

        def run_sticker_cutter(debug: bool = True):
            """执行自适应贴纸分割"""
            nonlocal sticker_crops, sticker_debug
            try:
                from .sticker_cutter import StickerCutter

                cutter = StickerCutter()
                sticker_crops, sticker_debug = cutter.process_image(img, debug=debug)
                if sticker_crops:
                    logger.debug(
                        f"使用自适应贴纸分割，共 {len(sticker_crops)} 个裁剪结果"
                    )
            except Exception as e:
                logger.debug(f"自适应贴纸分割失败: {e}")
                sticker_crops = None
                sticker_debug = None

        # 默认优先使用黑描边自适应切分，网格切分仅作为兜底
        if not boxes:
            run_sticker_cutter(debug=True)

        # 如果没有外部裁剪框或手动网格，则使用 SmartMemeSplitter 进行智能切分
        if not boxes and not sticker_crops:
            if precomputed_grid_boxes:
                boxes = precomputed_grid_boxes
            else:
                splitter = SmartMemeSplitter()
                boxes = splitter.detect_grid(img, debug=True)

        # 网格切分仍失败时，再次尝试黑描边自适应切分
        if not boxes and not sticker_crops:
            logger.debug("网格切分未检测到有效区域，再次尝试自适应贴纸分割")
            run_sticker_cutter(debug=True)

        if not boxes and not sticker_crops:
            logger.warning("智能切分未检测到网格")
            return []

        # 直接保存自适应切分结果，并保留调试图
        if sticker_crops:
            try:
                for idx, crop in enumerate(sticker_crops, 1):
                    file_name = f"{base_name}_{idx:03d}.png"
                    file_path = final_output_dir / file_name
                    cv2.imwrite(
                        str(file_path),
                        flatten_crop_background(crop, global_bg_color),
                    )
                    output_files.append(str(file_path))
                if sticker_debug is not None:
                    debug_file = final_output_dir / f"{base_name}_debug.png"
                    cv2.imwrite(str(debug_file), sticker_debug)
            except Exception as e:
                logger.debug(f"保存自适应贴纸分割结果失败: {e}")
                output_files.clear()
        else:
            # 生成掩码预览（便于调试网格线）
            try:
                mask = np.zeros((img.shape[0], img.shape[1]), dtype=np.uint8)
                for x, y, w_box, h_box in boxes:
                    cv2.rectangle(mask, (x, y), (x + w_box, y + h_box), 255, 2)
                mask_file = final_output_dir / f"{base_name}_mask.png"
                cv2.imwrite(str(mask_file), mask)
                if grid_debug is None:
                    grid_debug = _build_grid_debug_image(
                        img,
                        boxes,
                        grid_row_lines,
                        grid_col_lines,
                    )
                if grid_debug is not None:
                    debug_file = final_output_dir / f"{base_name}_debug.png"
                    cv2.imwrite(str(debug_file), grid_debug)
            except Exception as e:
                logger.debug(f"生成掩码预览失败: {e}")

            # 保存智能切分结果
            tight_grid_boxes = bool(grid_row_lines and grid_col_lines)
            for idx, (x, y, w, h) in enumerate(boxes, 1):
                pad = 0 if tight_grid_boxes else 2
                x1 = max(0, x - pad)
                y1 = max(0, y - pad)
                x2 = min(img.shape[1], x + w + pad)
                y2 = min(img.shape[0], y + h + pad)

                crop = img[y1:y2, x1:x2]
                file_name = f"{base_name}_{idx:03d}.png"
                file_path = final_output_dir / file_name
                cv2.imwrite(str(file_path), crop)
                output_files.append(str(file_path))

        return output_files

    except Exception as e:
        logger.error(f"Error splitting image: {e}")
        return []


def create_zip(files: list[str], output_filename: str | None = None) -> str | None:
    """
    将文件列表打包成zip

    Args:
        files: 文件路径列表
        output_filename: 输出zip文件名（包含路径）。如果不指定，则使用第一个文件的目录 + 目录名.zip

    Returns:
        str: zip文件路径，失败返回None
    """
    if not files:
        return None

    try:
        if not output_filename:
            first_file = Path(files[0])
            dir_path = first_file.parent
            dir_name = dir_path.name
            # 输出到目录的同级，即 .../split_output/base_name.zip
            output_filename_path = dir_path.parent / f"{dir_name}.zip"
            output_filename = str(output_filename_path)

        with zipfile.ZipFile(output_filename, "w", zipfile.ZIP_DEFLATED) as zipf:
            for file in files:
                file_path = Path(file)
                zipf.write(file_path, file_path.name)

        return output_filename
    except Exception as e:
        logger.error(f"Error creating zip: {e}")
        return None
