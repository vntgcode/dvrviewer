"""A small, read-only browser for recordings written by Amcrest cameras."""

from __future__ import annotations

import os
import re
from datetime import datetime, time
from pathlib import Path
from threading import Lock
from typing import Any

from flask import Flask, abort, jsonify, render_template, send_file


MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", "/recordings")).resolve()
SCAN_INTERVAL = max(1, int(os.environ.get("SCAN_INTERVAL", "15")))
IMAGE_LIMIT = max(1, int(os.environ.get("IMAGE_LIMIT", "500")))

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Amcrest JPG names omit the hour because it is encoded by the parent directory:
# jpg/15/42.53[M]... means 15:42:53.
IMAGE_RE = re.compile(r"^(\d{2})\.(\d{2})")
COMPACT_IMAGE_RE = re.compile(r"^(\d{2})(\d{2})(?:\[|$)")
VIDEO_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{2})-(\d{2})\.(\d{2})\.(\d{2})")


def _timestamp(day: str, filename: str, pattern: re.Pattern[str], end: bool = False) -> datetime | None:
    match = pattern.match(filename)
    if not match:
        return None
    values = match.groups()
    try:
        parts = values[3:6] if end else values[:3]
        return datetime.combine(datetime.strptime(day, "%Y-%m-%d").date(), time(*map(int, parts)))
    except ValueError:
        return None


def _image_timestamp(day: str, hour: str, filename: str) -> datetime | None:
    match = IMAGE_RE.match(filename) or COMPACT_IMAGE_RE.match(filename)
    if not match:
        return None
    try:
        minute, second = map(int, match.groups())
        return datetime.combine(datetime.strptime(day, "%Y-%m-%d").date(), time(int(hour), minute, second))
    except ValueError:
        return None


class MediaIndex:
    """Periodically rebuilt, in-memory view of a possibly changing NAS share."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._last_scan = 0.0
        self._lock = Lock()
        self._days: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {}

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def _scan_media(self, day: str, base: Path, image_pattern: str, video_pattern: str, image_hour: Any) -> dict[str, list[dict[str, Any]]]:
        images, videos = [], []
        for image in base.glob(image_pattern):
            # Doorbell events include a smaller [1] companion image beside the
            # full-resolution [0] image for the same timestamp.
            if image.name.endswith("[1].jpg"):
                continue
            stamp = _image_timestamp(day, image_hour(image), image.name)
            if stamp:
                images.append({"path": self._relative(image), "time": stamp.isoformat(), "name": image.name})
        for video in base.glob(video_pattern):
            start, end = _timestamp(day, video.name, VIDEO_RE), _timestamp(day, video.name, VIDEO_RE, True)
            if start and end and end >= start:
                videos.append({"path": self._relative(video), "start": start.isoformat(), "end": end.isoformat(), "name": video.name})
        return {"images": sorted(images, key=lambda x: x["time"]), "videos": sorted(videos, key=lambda x: x["start"])}

    def refresh_if_needed(self, force: bool = False) -> None:
        import time as clock

        if not force and clock.monotonic() - self._last_scan < SCAN_INTERVAL:
            return
        with self._lock:
            if not force and clock.monotonic() - self._last_scan < SCAN_INTERVAL:
                return
            days: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {}
            if self.root.is_dir():
                # Recordings are grouped by the human-friendly camera name, then date,
                # followed by the recorder's numeric channel directory.
                for camera_root in self.root.iterdir():
                    if not camera_root.is_dir():
                        continue
                    for day_dir in camera_root.iterdir():
                        if not day_dir.is_dir() or not DATE_RE.match(day_dir.name):
                            continue
                        cameras = days.setdefault(day_dir.name, {})
                        for channel_dir in day_dir.iterdir():
                            if not channel_dir.is_dir():
                                continue
                            camera_name = camera_root.name if channel_dir.name == "001" else f"{camera_root.name} / {channel_dir.name}"
                            media = self._scan_media(day_dir.name, channel_dir, "jpg/*/*.jpg", "dav/*/*.mp4", lambda image: image.parent.name)
                            if media["images"] or media["videos"]:
                                cameras[camera_name] = media
                        if not cameras:
                            days.pop(day_dir.name, None)
                    # Doorbell layout: <camera>/YYYY/MM/DD/HH/{jpg,dav}/file.
                    for year_dir in camera_root.iterdir():
                        if not year_dir.is_dir() or not re.match(r"^\d{4}$", year_dir.name):
                            continue
                        for month_dir in year_dir.iterdir():
                            if not month_dir.is_dir() or not re.match(r"^\d{2}$", month_dir.name):
                                continue
                            for day_dir in month_dir.iterdir():
                                if not day_dir.is_dir() or not re.match(r"^\d{2}$", day_dir.name):
                                    continue
                                day = f"{year_dir.name}-{month_dir.name}-{day_dir.name}"
                                media = self._scan_media(day, day_dir, "*/jpg/*.jpg", "*/dav/*.mp4", lambda image: image.parent.parent.name)
                                if media["images"] or media["videos"]:
                                    days.setdefault(day, {})[camera_root.name] = media
            self._days = days
            self._last_scan = clock.monotonic()

    def days(self) -> list[str]:
        self.refresh_if_needed()
        return sorted(self._days, reverse=True)

    def all_cameras(self) -> list[str]:
        self.refresh_if_needed()
        return sorted({camera for cameras in self._days.values() for camera in cameras})

    def days_for_camera(self, camera: str) -> list[str]:
        self.refresh_if_needed()
        return sorted((day for day, cameras in self._days.items() if camera in cameras), reverse=True)

    def cameras(self, day: str) -> list[str]:
        self.refresh_if_needed()
        return sorted(self._days.get(day, {}))

    def gallery(self, day: str, camera: str) -> dict[str, Any]:
        self.refresh_if_needed()
        data = self._days.get(day, {}).get(camera)
        if not data:
            return {"images": [], "videos": []}
        # Most recent events are normally most useful; cap the response for mobile browsers.
        images = list(reversed(data["images"][-IMAGE_LIMIT:]))
        return {"images": images, "videos": data["videos"]}

    def media_path(self, relative: str) -> Path | None:
        candidate = (self.root / relative).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            return None
        return candidate if candidate.is_file() else None


app = Flask(__name__)
index = MediaIndex(MEDIA_ROOT)


@app.get("/")
def home():
    return render_template("index.html")


@app.get("/api/days")
def get_days():
    return jsonify(index.days())


@app.get("/api/cameras")
def get_all_cameras():
    return jsonify(index.all_cameras())


@app.get("/api/cameras/<path:camera>/days")
def get_days_for_camera(camera: str):
    return jsonify(index.days_for_camera(camera))


@app.get("/api/days/<day>/cameras")
def get_cameras(day: str):
    return jsonify(index.cameras(day))


@app.get("/api/days/<day>/cameras/<camera>")
def get_gallery(day: str, camera: str):
    return jsonify(index.gallery(day, camera))


@app.get("/media/<path:relative>")
def media(relative: str):
    path = index.media_path(relative)
    if path is None:
        abort(404)
    # conditional=True supplies HTTP range responses required for reliable seeking on iOS.
    return send_file(path, conditional=True, download_name=path.name)

