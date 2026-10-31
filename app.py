"""A small, read-only browser for recordings written by Amcrest cameras."""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, time
from pathlib import Path
from threading import Lock
from typing import Any

from flask import Flask, abort, jsonify, render_template, send_file


MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", "/recordings")).resolve()
SCAN_INTERVAL = max(1, int(os.environ.get("SCAN_INTERVAL", "15")))
IMAGE_LIMIT = max(1, int(os.environ.get("IMAGE_LIMIT", "500")))
INDEX_DATABASE = Path(os.environ.get("INDEX_DATABASE", "/data/dvrviewer.sqlite3"))

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Amcrest JPG names omit the hour because it is encoded by the parent directory:
# jpg/15/42.53[M]... means 15:42:53.
IMAGE_RE = re.compile(r"^(\d{2})\.(\d{2})")
COMPACT_IMAGE_RE = re.compile(r"^(\d{2})(\d{2})(?:\[|$)")
SECOND_IMAGE_RE = re.compile(r"^(\d{2})(?:\[|$)")
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


def _nested_image_timestamp(day: str, path: Path) -> datetime | None:
    """Parse jpg/<hour>/<minute>/<second>[event].jpg recordings."""
    match = SECOND_IMAGE_RE.match(path.name)
    if not match:
        return None
    try:
        return datetime.combine(
            datetime.strptime(day, "%Y-%m-%d").date(),
            time(int(path.parent.parent.name), int(path.parent.name), int(match.group(1))),
        )
    except ValueError:
        return None


class MediaIndex:
    """Periodically rebuilt, in-memory view of a possibly changing NAS share."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._last_scan = 0.0
        self._lock = Lock()
        self._days: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {}

    def _connect(self) -> sqlite3.Connection:
        INDEX_DATABASE.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(INDEX_DATABASE, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS media (
                path TEXT PRIMARY KEY, camera TEXT NOT NULL, day TEXT NOT NULL,
                kind TEXT NOT NULL, name TEXT NOT NULL, image_time TEXT,
                video_start TEXT, video_end TEXT, size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL, last_seen INTEGER NOT NULL
            )
        """)
        connection.execute("CREATE INDEX IF NOT EXISTS media_camera_day ON media(camera, day)")
        return connection

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def _index_file(self, connection: sqlite3.Connection, scan_id: int, path: Path, camera: str, day: str, kind: str, parser: Any) -> None:
        try:
            stat = path.stat()
        except OSError:
            return
        relative = self._relative(path)
        existing = connection.execute("SELECT size, mtime_ns FROM media WHERE path = ?", (relative,)).fetchone()
        if existing == (stat.st_size, stat.st_mtime_ns):
            connection.execute("UPDATE media SET last_seen = ? WHERE path = ?", (scan_id, relative))
            return
        parsed = parser()
        if parsed is None:
            return
        image_time, video_start, video_end = parsed
        connection.execute("""
            INSERT INTO media (path, camera, day, kind, name, image_time, video_start, video_end, size, mtime_ns, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET camera=excluded.camera, day=excluded.day, kind=excluded.kind,
              name=excluded.name, image_time=excluded.image_time, video_start=excluded.video_start,
              video_end=excluded.video_end, size=excluded.size, mtime_ns=excluded.mtime_ns, last_seen=excluded.last_seen
        """, (relative, camera, day, kind, path.name, image_time, video_start, video_end, stat.st_size, stat.st_mtime_ns, scan_id))

    def _scan_media(self, connection: sqlite3.Connection, scan_id: int, camera: str, day: str, base: Path, image_pattern: str, video_pattern: str, image_stamp: Any) -> None:
        for image in base.glob(image_pattern):
            # Doorbell events include a smaller [1] companion image beside the
            # full-resolution [0] image for the same timestamp.
            if image.name.endswith("[1].jpg"):
                continue
            self._index_file(connection, scan_id, image, camera, day, "image", lambda: self._image_record(image_stamp(image)))
        for video in base.glob(video_pattern):
            self._index_file(connection, scan_id, video, camera, day, "video", lambda: self._parse_video(day, video.name))

    @staticmethod
    def _image_record(stamp: datetime | None) -> tuple[str, None, None] | None:
        return (stamp.isoformat(), None, None) if stamp else None

    @staticmethod
    def _parse_video(day: str, filename: str) -> tuple[None, str, str] | None:
        start, end = _timestamp(day, filename, VIDEO_RE), _timestamp(day, filename, VIDEO_RE, True)
        return (None, start.isoformat(), end.isoformat()) if start and end and end >= start else None

    def _load_index(self, connection: sqlite3.Connection) -> dict[str, dict[str, dict[str, list[dict[str, Any]]]]]:
        days: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {}
        for camera, day, kind, path, name, image_time, video_start, video_end in connection.execute("SELECT camera, day, kind, path, name, image_time, video_start, video_end FROM media"):
            gallery = days.setdefault(day, {}).setdefault(camera, {"images": [], "videos": []})
            if kind == "image":
                gallery["images"].append({"path": path, "time": image_time, "name": name})
            else:
                gallery["videos"].append({"path": path, "start": video_start, "end": video_end, "name": name})
        for cameras in days.values():
            for gallery in cameras.values():
                gallery["images"].sort(key=lambda image: image["time"])
                gallery["videos"].sort(key=lambda video: video["start"])
        return days

    def refresh_if_needed(self, force: bool = False) -> None:
        import time as clock

        if not force and clock.monotonic() - self._last_scan < SCAN_INTERVAL:
            return
        with self._lock:
            if not force and clock.monotonic() - self._last_scan < SCAN_INTERVAL:
                return
            scan_id = clock.time_ns()
            connection = self._connect()
            if self.root.is_dir():
                # Recordings are grouped by the human-friendly camera name, then date,
                # followed by the recorder's numeric channel directory.
                for camera_root in self.root.iterdir():
                    if not camera_root.is_dir():
                        continue
                    for day_dir in camera_root.iterdir():
                        if not day_dir.is_dir() or not DATE_RE.match(day_dir.name):
                            continue
                        for channel_dir in day_dir.iterdir():
                            if not channel_dir.is_dir():
                                continue
                            camera_name = camera_root.name if channel_dir.name == "001" else f"{camera_root.name} / {channel_dir.name}"
                            self._scan_media(connection, scan_id, camera_name, day_dir.name, channel_dir, "jpg/*/*.jpg", "dav/*/*.mp4", lambda image: _image_timestamp(day_dir.name, image.parent.name, image.name))
                            self._scan_media(connection, scan_id, camera_name, day_dir.name, channel_dir, "jpg/*/*/*.jpg", "dav/*/*.mp4", lambda image: _nested_image_timestamp(day_dir.name, image))
                    # Some exports add a source-camera directory below the user-facing
                    # camera folder: <camera>/<source-camera>/YYYY-MM-DD/001/...
                    for source_camera_dir in camera_root.iterdir():
                        if not source_camera_dir.is_dir():
                            continue
                        for day_dir in source_camera_dir.iterdir():
                            if not day_dir.is_dir() or not DATE_RE.match(day_dir.name):
                                continue
                            for channel_dir in day_dir.iterdir():
                                if not channel_dir.is_dir():
                                    continue
                                camera_name = camera_root.name if channel_dir.name == "001" else f"{camera_root.name} / {channel_dir.name}"
                                self._scan_media(connection, scan_id, camera_name, day_dir.name, channel_dir, "jpg/*/*.jpg", "dav/*/*.mp4", lambda image: _image_timestamp(day_dir.name, image.parent.name, image.name))
                                self._scan_media(connection, scan_id, camera_name, day_dir.name, channel_dir, "jpg/*/*/*.jpg", "dav/*/*.mp4", lambda image: _nested_image_timestamp(day_dir.name, image))
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
                                self._scan_media(connection, scan_id, camera_root.name, day, day_dir, "*/jpg/*.jpg", "*/dav/*.mp4", lambda image: _image_timestamp(day, image.parent.parent.name, image.name))
            connection.execute("DELETE FROM media WHERE last_seen != ?", (scan_id,))
            connection.commit()
            self._days = self._load_index(connection)
            connection.close()
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

