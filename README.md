<p align="right">
  <img src="static/favicon.svg" alt="Home QR Inventory Logo" width="80">
</p>


# Home QR Inventory

**Home QR Inventory** is a self-hosted, network-local inventory management application designed for structured storage environments. It models physical space as a hierarchical graph of:

- **Nodes**: `Cabinet`, `Wardrobe`, `Shelf`, `Drawer`
- **Containers**: `Box`, `Organizator`, `InPlace` (always attached to shelves/drawers)

Items are stored exclusively inside containers. Each container is assigned a stable identifier (ID) and an associated QR code label, enabling direct navigation from a physical scan to the corresponding detailed view in the web interface.

The application is implemented as a single **FastAPI** service backed by **SQLite**, using **Jinja2** for server-side rendering and **Pillow** / **qrcode** for QR label generation. It supports extensible item metadata via a type/field system (EAV-style), allowing custom schemas per item category without modifying the core database schema.

All core subsystems—**database schema initialization**, **QR code generation**, **hierarchical cascade deletion**, and **mkcert-based local TLS integration**—are implemented in-process within a single codebase and tuned for deployment on a single host within a trusted home or internal network.

---

## Key Features

<table>
  <tr>
    <td>
      <img src="misc/homeqr.gif" alt="Desktop demo of Home QR Inventory">
    </td>
    <td align="right" valign="top">
      <img src="misc/homeqrmobile.gif" alt="Mobile demo of Home QR Inventory" width="180">
    </td>
  </tr>
</table>

- **Typed storage hierarchy**
  - Nodes: `Cabinet`, `Wardrobe`, `Shelf`, `Drawer`
  - Containers: `Box`, `Organizator`, `InPlace` attached to shelves/drawers
  - Application-level validation of parent–child relationships

- **Containers, items & QR codes**
  - Items exist only inside containers; containers and items can be moved between compatible locations
  - Each container gets an 8-character ID
  - QR labels (ID + name) as files (`qrcodes/<ID>.png`) or on-demand (`/container/{id}/qr.png`)

- **Extensible item metadata**
  - Custom item types with ordered fields (`text`, `number`, `select`, `date`, `checkbox`)
  - EAV-style schema for per-item field values

- **Search & views**
  - Global search across nodes, containers, item names, and notes
  - Node view: `/node/{id}` with hierarchy context and container stats
  - Container view: `/container/{id}` with item list and operations

- **JSON API**
  - Read-only endpoints for containers, items, item types, and fields
  - Intended for tooling and automation (no HTML scraping required)

- **TLS & mkcert integration**
  - Optional HTTPS via `TLS_CERT_FILE` / `TLS_KEY_FILE`
  - mkcert root CA discovery and export to `./certs`, served via `/certs` and `/install-certificate`

## Architecture

- Single FastAPI app (`app.py`) with SQLite (`data.sqlite3`) as the only persistence layer
- Jinja2 templates under `templates/` for HTML responses
- Static mounts for `/static`, `/qrcodes`, and `/certs`
- QR generation via `qrcode` + Pillow; mkcert integration for local CA handling

---

## 1. Installation

### 1.1 Prerequisites

- Python 3.10+
- Optional: [`mkcert`](https://github.com/FiloSottile/mkcert) if you plan to use local HTTPS with a trusted root CA

### 1.2a Clone and install

```bash
git clone https://github.com/droghi2/home_qr_inventory.git
cd home_qr_inventory

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

---

### 1.2b Docker Compose Deployment

Example `docker-compose.yml` for running Home QR Inventory with HTTPS:

```yaml
services:
  HomeQRinv:
    restart: unless-stopped
    image: python:3.12-slim
    working_dir: /home/app
    volumes:
      # Map your application directory from the host into the container
      - /app:/home/app
    command: >
      sh -c "
        pip install -r requirements.txt &&
        uvicorn app:app --host 0.0.0.0 --port 8443 --ssl-keyfile key.pem --ssl-certfile cert.pem
      "
    ports:
      - "8443:8443"
networks: {}
```

> ⚠️ **Volume layout requirement**
>
> The host directory you mount into the container (in the example:`/app`) **must contain the repository files at its root**    
> Docker maps this directory to `/home/app` inside the container,
> and the startup command expects to run from that path. If you mount a parent folder or a different structure,
> the commands will fail.

## Browser Camera Access (HTTPS Requirement)

Modern browsers only allow camera access (`getUserMedia`) from **secure origins**:

- `https://` URLs
- `http://localhost` (special-case exception)

If you plan to scan QR codes directly from the browser (e.g. using a phone or another device on your LAN), you **must** run the app over HTTPS (e.g. via mkcert and `TLS_CERT_FILE` / `TLS_KEY_FILE`).  
Plain `http://<your-lan-ip>` will not be allowed to use the camera on most browsers.

---

## 2. Running the Application

### 2.1 Development (HTTP)

From the project root:

```bash
uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

Then open:

```text
http://127.0.0.1:8000/
```

or use your machine’s LAN IP to access it from other devices.


### 2.2 Optional: HTTPS with mkcert

Install and initialize `mkcert` (see mkcert documentation):

```bash
mkcert -install
```

Generate a certificate/key pair for your host (example):

```bash
mkcert localhost 127.0.0.1 ::1
```

Place/rename the resulting files as:

```text
cert.pem
key.pem
```

in the project root, or adjust `TLS_CERT_FILE` / `TLS_KEY_FILE` in `app.py`.

Start `uvicorn` with TLS:

```bash
uvicorn app:app --host 0.0.0.0 --port 8443 --ssl-certfile cert.pem --ssl-keyfile key.pem
```

Navigate to:

```text
https://<your-lan-ip>:8443/
```

To install the mkcert root CA on clients, visit:

```text
https://<your-lan-ip>:8443/install-certificate
```

and download the CA file from `/certs`.

---

## 3. Configuration

### 3.1 QR base URL

The application uses `QR_BASE_URL` to construct the base URL that QR codes point to:

```python
QR_BASE_URL = os.getenv("QR_BASE_URL", "http://<your-lan-ip>:80000").rstrip("/")
```

Set `QR_BASE_URL` in the environment to match your actual host/port, for example:

```bash
export QR_BASE_URL="https://<your-lan-ip>:8443"
```

Then restart the application and regenerate QR codes where needed.

---

## License

This project is licensed under the **MIT License**.  
See the ![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg).
© 2025 droghi2. All rights reserved.
