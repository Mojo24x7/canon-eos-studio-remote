# Canon EOS Studio Remote

Browser-based studio control for Canon EOS cameras, optimized for:

- **Raspberry Pi** (e.g. Pi 4 / Pi 5)
- **Rockchip SBCs** (e.g. Rock 5B)

It can run:

- as a **bare-metal** Flask app on a single board,
- as a **Docker container**, or
- as a **K3s microservice** in a cluster,

and can work in both:

- **Local mode** – camera physically attached to the same board
- **Distributed mode** – camera attached to a small helper board via **USB/IP**, with the main server running on a more powerful node (even inside K3s).

---

## Features

- **Live View / “Mirror” mode**  
  Live preview in the browser on any device on your LAN (HDMI monitor, tablet, phone, laptop).

- **Capture & mini gallery**
  - One-click capture from the web UI
  - Images organised into dated folders
  - 3×3 mini gallery on the main page
  - Full gallery page for browsing sessions

- **Quick settings & presets**
  - Change supported camera settings via the UI (as exposed by `gphoto2`)
  - Save and load presets (e.g. “studio key light”, “window light”, “night”).

- **Intervalometer**
  - Start/stop time-lapse from the UI
  - Configurable interval and shot count.

- **Import-from-camera with cancel**
  - Bulk import all images from the camera (via `gphoto2`)
  - Runs as a background worker
  - Shows progress and allows cancelling mid-way.

- **GPS / EXIF tagging**
  - Set GPS coordinates in the UI (or fetch from another sensor/service)
  - Write coordinates and other EXIF metadata into images.

- **Histogram & info panel**
  - Generate histogram / summary for recent captures
  - Quick exposure check without opening a full editor.

- **Session / folder switching**
  - Quickly change the active photo folder / session
  - Useful for events, multiple shoots, or separating “family” vs “work” sessions.

- **Optional face-recognition integration**
  - On capture, the server can notify an external face-recognition service
  - Sends file path + metadata over HTTP for face tagging / notifications
  - Designed to work nicely with an AuraFace/RKNN-style pipeline running on an RK3588 board (not part of this repository).

---

## Deployment Topologies

### 1. Single-node (local USB)

- Camera’s USB cable is plugged directly into:
  - a **Raspberry Pi**, or
  - a **Rockchip board** (e.g. Rock 5B).
- Flask app (or Docker/K3s pod) runs on that same board.

This is the simplest and is ideal for a single studio/location.

### 2. Distributed USB/IP (camera node + main server)

For more flexible setups:

- **Camera node**
  - Small board physically connected to the Canon via USB.
  - Runs a small `usbip` exporter.
  - Makes the Canon USB device available over the network.

- **Main server node**
  - More powerful board (e.g. Pi 5 / Rockchip SBC with NVMe).
  - Attaches the remote USB device via `usbip`.
  - Runs the Flask app bare-metal, in Docker, or as a **K3s service**.

This is useful if your “brains + storage” live in one place, and the camera is physically located somewhere else.

---

## Architecture Overview

Main components:

- **Flask backend (`server.py`)**
  - HTTP API and HTML pages (`index.html`, `gallery.html`, `field.html`).
  - Orchestrates capture, intervalometer, import, EXIF, GPS, presets, histogram, etc.

- **Camera control via `gphoto2`**
  - Capture, listing, import, live view, and settings executed via `gphoto2` CLI commands.

- **Image and EXIF helpers**
  - EXIF/GPS tagging using `exiftool` (or similar).
  - Thumbnail and histogram generation (e.g. with `Pillow`, `ffmpeg`).

- **Optional integration with a face pipeline**
  - Configurable URL to call after each capture.
  - Sends image path, timestamp, and optionally GPS data.
  - External service performs detection/recognition and returns results or triggers notifications.

---

## Repository Structure

A typical layout looks like:

```text
canon-eos-studio-remote/
  server.py          # Flask backend and application logic
  README.md
  requirements.txt   # Python dependencies (if provided)

  photos/            # Captured photos (organised per date/session)
  tmp/               # Temporary files (live view frames, working data)

  www/
    index.html       # Main UI (live view, quick controls, mini gallery)
    gallery.html     # Gallery view
    field.html       # Alternate / simplified layout
```

You can adapt the exact structure to your needs; the code expects a `www/` directory with HTML templates and some writable directories for photos and temporary files.

---

## Requirements

### Hardware

**Main server:**

- Raspberry Pi (e.g. Pi 4 or Pi 5), or  
- Rockchip SBC (e.g. Rock 5B)

**Optional camera node (for USB/IP mode):**

- Any Pi or SBC that supports `usbip` and `gphoto2`.

**Camera:**

- Canon EOS camera.  
- The project has been tested with models such as the Canon 5D Mark II; other `gphoto2`-supported EOS cameras may work.

**Network:**

- All boards on the same network (wired or Wi-Fi).

### System packages

On Raspberry Pi OS / other Debian-like systems:

```bash
sudo apt update
sudo apt install -y   gphoto2   libgphoto2-6 libgphoto2-dev   exiftool   ffmpeg   python3-venv python3-pip   usbip
```

On Rockchip boards, install the equivalent packages from your distribution’s repositories.

### Python packages

Install in a virtual environment:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

A typical `requirements.txt` might include:

```text
Flask
Werkzeug
Pillow
numpy
```

(Plus any other libraries referenced in `server.py`.)

---

## Running (Bare Metal)

### 1. Clone the repository

```bash
git clone https://github.com/Mojo24x7/canon-eos-studio-remote.git
cd canon-eos-studio-remote
```

(Or use the SSH URL if you’ve set up SSH keys.)

### 2. Set up the virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Test camera connectivity

```bash
gphoto2 --auto-detect
gphoto2 --summary
```

You should see your Canon EOS camera listed.

### 4. Run the Flask server

```bash
source venv/bin/activate
python3 server.py
```

Then open in your browser:

```text
http://<board-ip>:5000
```

You should see:

- live view / mirror area  
- capture button and quick controls  
- mini gallery  
- GPS controls  
- import start/stop buttons  

---

## Running in Docker

### Example Dockerfile (simplified)

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

CMD ["python", "server.py"]
```

### Build the image

```bash
docker build -t canon-eos-studio-remote:latest .
```

### Run the container (local USB case)

You need to pass through USB and a folder for photos:

```bash
docker run -d   --name canon-eos-studio-remote   --restart unless-stopped   --net host   --device /dev/bus/usb:/dev/bus/usb   -v /path/to/photos:/photos   canon-eos-studio-remote:latest
```

Adjust `/path/to/photos` and the internal path so they match what `server.py` expects for the photos directory.

---

## Running in K3s

The app can be deployed as a pod in a K3s cluster, typically pinned to the node that has the camera attached.

### Example Deployment (skeleton)

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: canon-eos-studio-remote
spec:
  replicas: 1
  selector:
    matchLabels:
      app: canon-eos-studio-remote
  template:
    metadata:
      labels:
        app: canon-eos-studio-remote
    spec:
      hostNetwork: true
      containers:
        - name: app
          image: canon-eos-studio-remote:latest
          securityContext:
            privileged: true        # for USB pass-through
          volumeMounts:
            - name: photos
              mountPath: /photos
      volumes:
        - name: photos
          hostPath:
            path: /path/on/node/for/photos
            type: DirectoryOrCreate
      nodeSelector:
        camera-node: "true"         # label the node with the camera
```

You can then expose it with a Service and your preferred ingress setup (NGINX, Apache reverse proxy, etc.).

---

## USB/IP (Distributed Mode) – High Level

This repository does not ship a full USB/IP script, but the idea is:

**On the camera node:**

- Load the `usbip_host` module.
- Export the Canon USB device with `usbip`.

**On the main server node** (where this app runs):

- Attach the remote USB device using `usbip`.
- `gphoto2` will then see the camera as if it were local.

The application code does not need to change; it just talks to `gphoto2` as usual.

---

## Optional Face-Recognition Integration

The server can be configured to call an external HTTP endpoint after each capture, for example:

- A local face-recognition service running on another board.
- A service that uses AuraFace-based embeddings and a local face database.

Typical flow:

1. Canon EOS Studio Remote captures an image.
2. It sends a POST request with JSON such as:
   - image path or URL
   - timestamp
   - optional GPS/EXIF data
3. The face service:
   - detects faces
   - matches them to known identities
   - updates a database or sends notifications.

The exact payload, URL, and behaviour are configurable in the application code.

---

## License

This project is licensed under the **Apache License 2.0** – see the [`LICENSE`](LICENSE) file for details.

---

## Credits & Dependencies

This project builds on:

- **gphoto2 / libgphoto2** – Canon EOS camera control  
- **exiftool** – EXIF and GPS tagging  
- **ffmpeg** – video and frame processing  
- **Flask** and related Python libraries – web backend  

Face-recognition services are **not** part of this repository; if you integrate an external face pipeline, follow the licenses and documentation of those separate projects.
