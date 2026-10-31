# DVR Viewer

A read-only, no-login web viewer for Amcrest recordings stored as:

```
<camera-name>/YYYY-MM-DD/<channel>/jpg/<hour>/<MM>.<SS>*.jpg
<camera-name>/YYYY-MM-DD/<channel>/dav/<hour>/<HH>.<MM>.<SS>-<HH>.<MM>.<SS>*.mp4

# Doorbell layout is also supported
<camera-name>/YYYY/MM/DD/<hour>/jpg/<MM><SS>*.jpg
<camera-name>/YYYY/MM/DD/<hour>/dav/<HH>.<MM>.<SS>-<HH>.<MM>.<SS>*.mp4
```

The gallery shows recent still images for a date and camera. Selecting one opens every MP4 whose filename time range contains the still's timestamp. The app rescans the mounted directory periodically, so camera-created or deleted files are reflected without restarting.

## Run it

By default, Compose mounts the local `./recordings` directory. To use a NAS mount elsewhere, set `RECORDINGS_PATH` to the directory that directly contains the camera-name folders. For example:

Pull and start (or recreate) the service:

```sh
docker compose pull
docker compose up -d
```

Open `http://<docker-host>:8000` from Safari or Firefox. There is intentionally no authentication; place it only on a trusted network or add access control in a reverse proxy.

## Configuration

`SCAN_INTERVAL` defaults to 15 seconds and controls how often the filesystem is scanned. `IMAGE_LIMIT` defaults to 500, keeping mobile galleries responsive. Both can be changed in Compose.

The application never changes mounted recordings. The Compose mount is explicitly read-only.

## Notes

MP4 playback depends on the video/audio codecs produced by the camera. H.264/AAC MP4 is broadly compatible with iOS Safari and Firefox. If a camera writes a different codec, the browser may display the file but not decode it; transcoding would need to be added separately.

This project was created using an LLM and is designed to be just enough to meet the basic goals. 
