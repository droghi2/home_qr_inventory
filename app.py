
import os
import sqlite3
from uuid import uuid4
from pathlib import Path
import re, unicodedata, json
import json
import qrcode
from fastapi import FastAPI, Request, Form, HTTPException, Body
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from PIL import Image, ImageDraw, ImageFont
from PIL import Image, ImageDraw, ImageFont
import time
from PIL import Image, ImageDraw, ImageFont
from fastapi.responses import Response
import io, textwrap, qrcode
from fastapi.responses import JSONResponse


APP_TITLE = "Home QR Inventory (Typed)"
BASE_DIR = os.path.dirname(__file__)
DB_PATH = os.path.join(BASE_DIR, "data.sqlite3")
QRCODES_DIR = os.path.join(BASE_DIR, "qrcodes")
Path(QRCODES_DIR).mkdir(exist_ok=True)

# Hard-coded base for QR (change via env if needed)
QR_BASE_URL = os.getenv("QR_BASE_URL", "http://192.168.1.245:80").rstrip("/")

def qr_for_container(cid: str) -> str:
    return f"{QR_BASE_URL}/container/{cid}"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db(); cur = conn.cursor()

    # Structure nodes: Cabinet, Wardrobe, Shelf, Drawer
    cur.execute("""
        CREATE TABLE IF NOT EXISTS nodes(
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,        -- Cabinet | Wardrobe | Shelf | Drawer
            name TEXT NOT NULL,
            parent_id TEXT,
            note TEXT DEFAULT '',
            FOREIGN KEY(parent_id) REFERENCES nodes(id) ON DELETE CASCADE
        );
    """)

    # Containers: Box | Organizator | InPlace (live under Shelf/Drawer)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS containers(
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,        -- Box | Organizator | InPlace
            name TEXT NOT NULL,
            parent_id TEXT NOT NULL,   -- nodes.id (Shelf/Drawer)
            note TEXT DEFAULT '',
            FOREIGN KEY(parent_id) REFERENCES nodes(id) ON DELETE CASCADE
        );
    """)

    # Items live only in containers
    cur.execute("""
        CREATE TABLE IF NOT EXISTS items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            container_id TEXT NOT NULL,
            name TEXT NOT NULL,
            qty INTEGER DEFAULT 1,
            note TEXT DEFAULT '',
            FOREIGN KEY(container_id) REFERENCES containers(id) ON DELETE CASCADE
        );
    """)



    # Item types & dynamic fields (EAV style)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS item_types(
            id TEXT PRIMARY KEY,
            name TEXT UNIQUE NOT NULL
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS item_fields(
            id TEXT PRIMARY KEY,
            type_id TEXT NOT NULL,
            name TEXT NOT NULL,      -- machine key
            label TEXT NOT NULL,     -- UI label
            kind TEXT NOT NULL CHECK(kind IN ('text','number','select','date','checkbox')),
            required INTEGER DEFAULT 0,
            options TEXT DEFAULT '[]',  -- JSON array for 'select'
            ord INTEGER DEFAULT 0,
            FOREIGN KEY(type_id) REFERENCES item_types(id) ON DELETE CASCADE
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS item_field_values(
            item_id INTEGER NOT NULL,
            field_id TEXT NOT NULL,
            value TEXT,
            PRIMARY KEY (item_id, field_id),
            FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE,
            FOREIGN KEY(field_id) REFERENCES item_fields(id) ON DELETE CASCADE
        );
    """)

    # Add items.type_id if missing (for item -> type link)
    cur.execute("PRAGMA table_info(items)")
    cols = [r[1] for r in cur.fetchall()]
    if "type_id" not in cols:
        cur.execute("ALTER TABLE items ADD COLUMN type_id TEXT")



    conn.commit(); conn.close()

init_db()

# FastAPI app & static
app = FastAPI(title=APP_TITLE)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
app.mount("/qrcodes", StaticFiles(directory=QRCODES_DIR), name="qrcodes")

# Templates
env = Environment(loader=FileSystemLoader(os.path.join(BASE_DIR, "templates")), autoescape=select_autoescape(['html','xml']))
def render(tpl, **kwargs): return HTMLResponse(env.get_template(tpl).render(**kwargs))

# Rules
ALLOWED_NODE_CHILDREN = {
    "ROOT": {"Cabinet", "Wardrobe"},
    "Cabinet": {"Shelf", "Drawer"},
    "Wardrobe": {"Shelf", "Drawer"},
    "Shelf": set(),   # no child nodes under shelves
    "Drawer": set(),  # no child nodes under drawers
}
ALLOWED_CONTAINER_BY_PARENT = {
    "Shelf": {"Box", "Organizator", "InPlace"},
    "Drawer": {"Box", "Organizator", "InPlace"},
}



def slugify_label(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return "field"
    # normalize diacritics → ascii
    norm = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    key = re.sub(r"[^a-zA-Z0-9]+", "_", norm).strip("_").lower()
    return key or "field"

def ensure_unique_field_key(conn, type_id: str, base_key: str, exclude_field_id: str | None = None) -> str:
    cur = conn.cursor()
    if exclude_field_id:
        cur.execute("SELECT name FROM item_fields WHERE type_id=? AND id!=?", (type_id, exclude_field_id))
    else:
        cur.execute("SELECT name FROM item_fields WHERE type_id=?", (type_id,))
    existing = {r["name"] for r in cur.fetchall()}
    k, n = base_key, 2
    while k in existing:
        k = f"{base_key}_{n}"
        n += 1
    return k



def list_item_types(conn):
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM item_types ORDER BY name")
    return cur.fetchall()

def fields_for_type(conn, type_id: str):
    cur = conn.cursor()
    cur.execute("""
        SELECT id, name, label, kind, required, options, ord
        FROM item_fields
        WHERE type_id=?
        ORDER BY ord, label
    """, (type_id,))
    rows = cur.fetchall()
    # parse options JSON
    out = []
    for r in rows:
        o = dict(r)
        try:
            o["options"] = json.loads(o["options"] or "[]")
        except Exception:
            o["options"] = []
        out.append(o)
    return out

def values_for_items(conn, item_ids):
    """Return {item_id: [{label, value, field_id}], ...}"""
    if not item_ids: return {}
    ph = ",".join("?" * len(item_ids))
    cur = conn.cursor()
    cur.execute(f"""
        SELECT v.item_id, v.value, f.label, f.id AS field_id
        FROM item_field_values v
        JOIN item_fields f ON f.id = v.field_id
        WHERE v.item_id IN ({ph})
        ORDER BY f.ord, f.label
    """, item_ids)
    out = {}
    for row in cur.fetchall():
        out.setdefault(row["item_id"], []).append(
            {"label": row["label"], "value": row["value"], "field_id": row["field_id"]}
        )
    return out







def delete_node_recursive(conn, node_id: str):
    """Delete a node and everything under it (child nodes, containers, items, QR pngs)."""
    cur = conn.cursor()

    # 1) Delete containers directly under this node (safety; usually shelves/drawers hold them)
    cur.execute("SELECT id FROM containers WHERE parent_id=?", (node_id,))
    for (cid,) in cur.fetchall():
        # delete items
        cur.execute("DELETE FROM items WHERE container_id=?", (cid,))
        # delete container
        cur.execute("DELETE FROM containers WHERE id=?", (cid,))
        # delete QR file if present
        try:
            png = os.path.join(QRCODES_DIR, f"{cid}.png")
            if os.path.exists(png):
                os.remove(png)
        except Exception:
            pass

    # 2) Recurse into child nodes (shelves/drawers)
    cur.execute("SELECT id FROM nodes WHERE parent_id=?", (node_id,))
    for (child_id,) in cur.fetchall():
        delete_node_recursive(conn, child_id)

    # 3) Finally delete this node
    cur.execute("DELETE FROM nodes WHERE id=?", (node_id,))


def build_qr_with_label_bytes(url: str, label: str) -> bytes:
    # Crisp QR
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=10, border=4)
    qr.add_data(url); qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    # Pick a font
    try:
        font = ImageFont.truetype("arial.ttf", max(24, qr_img.width // 8))
    except Exception:
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", max(24, qr_img.width // 8))
        except Exception:
            font = ImageFont.load_default()

    label = (label or "").strip()

    # Optional: wrap long names to max width ~1.3x QR width
    max_text_width = int(qr_img.width * 1.3)
    lines = []
    if label:
        words = label.split()
        line = ""
        dtmp = ImageDraw.Draw(Image.new("RGB", (10, 10), "white"))
        for w in words:
            test = (line + " " + w).strip()
            tw, th = dtmp.textbbox((0,0), test, font=font)[2:]
            if tw <= max_text_width or not line:
                line = test
            else:
                lines.append(line)
                line = w
        if line:
            lines.append(line)
    if not lines:  # empty name
        lines = [""]

    # Measure total text block
    dtmp = ImageDraw.Draw(Image.new("RGB", (10, 10), "white"))
    line_sizes = [dtmp.textbbox((0,0), ln, font=font) for ln in lines]
    line_ws = [b[2]-b[0] for b in line_sizes]
    line_hs = [b[3]-b[1] for b in line_sizes]
    text_w = max(line_ws) if line_ws else 0
    line_height = max(line_hs) if line_hs else 0
    text_h = line_height * len(lines) + max(0, (len(lines)-1) * 6)

    # Canvas
    pad, gap = 24, 12
    out_w = max(qr_img.width + 2*pad, text_w + 2*pad)
    out_h = pad + qr_img.height + gap + text_h + pad
    canvas = Image.new("RGB", (out_w, out_h), "white")
    d = ImageDraw.Draw(canvas)

    # Paste QR
    x_qr = (out_w - qr_img.width) // 2
    y_qr = pad
    canvas.paste(qr_img, (x_qr, y_qr))

    # -- padding & layout (increase bottom padding) --
    pad_top = 24
    pad_bottom = 48   # was 24; adds extra whitespace below the label
    gap = 12          # space between QR and text

    out_w = max(qr_img.width + 2*pad_top, text_w + 2*pad_top)
    out_h = pad_top + qr_img.height + gap + text_h + pad_bottom

    canvas = Image.new("RGB", (out_w, out_h), "white")
    d = ImageDraw.Draw(canvas)

    # Center QR
    x_qr = (out_w - qr_img.width) // 2
    y_qr = pad_top
    canvas.paste(qr_img, (x_qr, y_qr))

    # Draw text centered
    y_text = y_qr + qr_img.height + gap
    for ln, w in zip(lines, line_ws):
        x = (out_w - w) // 2
        d.text((x, y_text), ln, fill=(0,0,0), font=font)
        y_text += line_height + 6

    # Return bytes
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


def save_qr_with_label(cid: str, label: str):
    """Generate QR PNG with the container name centered below."""
    url = qr_for_container(cid)

    # Build QR (crisp, standard border)
    qr = qrcode.QRCode(
        version=None, error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10, border=4
    )
    qr.add_data(url); qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    # Font
    try:
        font = ImageFont.truetype("arial.ttf", 24)
    except Exception:
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", 24)
        except Exception:
            font = ImageFont.load_default()

    label = (label or "").strip()
    # Measure text
    dtmp = ImageDraw.Draw(Image.new("RGB", (10, 10), "white"))
    bbox = dtmp.textbbox((0, 0), label, font=font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]

    # Canvas below QR
    pad = 24      # outer padding
    gap = 12      # QR -> text gap
    out_w = max(qr_img.width + 2*pad, text_w + 2*pad)
    out_h = pad + qr_img.height + gap + text_h + pad

    canvas = Image.new("RGB", (out_w, out_h), "white")
    d = ImageDraw.Draw(canvas)

    # Center QR
    x_qr = (out_w - qr_img.width) // 2
    y_qr = pad
    canvas.paste(qr_img, (x_qr, y_qr))

    # Center text
    x_text = (out_w - text_w) // 2
    y_text = y_qr + qr_img.height + gap
    d.text((x_text, y_text), label, fill=(0, 0, 0), font=font)

    canvas.save(os.path.join(QRCODES_DIR, f"{cid}.png"))



# -------------- Home = Map --------------
@app.get("/", response_class=HTMLResponse)
def map_view(request: Request, q: str | None = None):
    """
    Home map:
      - top-level grid with counts
      - global search across containers + items
      - show matched items under each matching container
    """
    conn = get_db(); cur = conn.cursor()

    # Top-level nodes
    cur.execute("SELECT * FROM nodes WHERE parent_id IS NULL ORDER BY type, name")
    top = cur.fetchall()
    top_ids = [t["id"] for t in top]

    # Children per top (for quick lists)
    children = {}
    for n in top:
        cur.execute("SELECT * FROM nodes WHERE parent_id=? ORDER BY type, name", (n["id"],))
        subs = cur.fetchall()
        shelves = [s for s in subs if s["type"] == "Shelf"]
        drawers = [d for d in subs if d["type"] == "Drawer"]
        children[n["id"]] = {"shelves": shelves, "drawers": drawers}

    # Counts for the top-level tiles
    shelves_count, drawers_count, containers_count = {}, {}, {}
    if top_ids:
        placeholders = ",".join("?" * len(top_ids))
        cur.execute(f"""
            SELECT parent_id, type, COUNT(*) AS cnt
            FROM nodes
            WHERE parent_id IN ({placeholders}) AND type IN ('Shelf','Drawer')
            GROUP BY parent_id, type
        """, top_ids)
        for r in cur.fetchall():
            (shelves_count if r["type"]=="Shelf" else drawers_count)[r["parent_id"]] = r["cnt"]

        cur.execute(f"""
            SELECT t.id AS top_id, COUNT(*) AS cnt
            FROM containers c
            JOIN nodes p ON p.id = c.parent_id
            JOIN nodes t ON t.id = p.parent_id
            WHERE t.id IN ({placeholders})
            GROUP BY t.id
        """, top_ids)
        for r in cur.fetchall():
            containers_count[r["top_id"]] = r["cnt"]

    # Global search results (containers)
    results = []
    matched_items = {}  # cont_id -> [items...]
    if q:
        like = f"%{q}%"
        cur.execute("""
            SELECT DISTINCT c.*, p.name AS parent_name, p.type AS parent_type, t.name AS top_name, t.id AS top_id
            FROM containers c
            JOIN nodes p ON p.id=c.parent_id
            LEFT JOIN nodes t ON t.id=p.parent_id
            LEFT JOIN items it ON it.container_id=c.id
            WHERE c.name LIKE ? OR c.type LIKE ? OR p.name LIKE ? OR t.name LIKE ? OR it.name LIKE ? OR it.note LIKE ?
            ORDER BY t.name, p.name, c.name
        """, (like, like, like, like, like, like))
        results = cur.fetchall()

        # collect matched items for the containers shown above
        cont_ids = [r["id"] for r in results]
        if cont_ids:
            placeholders = ",".join("?" * len(cont_ids))
            cur.execute(f"""
                SELECT it.id AS item_id, it.name, it.qty, it.note, c.id AS cont_id
                FROM items it
                JOIN containers c ON c.id = it.container_id
                WHERE c.id IN ({placeholders}) AND (it.name LIKE ? OR it.note LIKE ?)
                ORDER BY it.id DESC
            """, cont_ids + [like, like])
            for row in cur.fetchall():
                matched_items.setdefault(row["cont_id"], []).append(row)

    conn.close()
    return render(
        "map.html",
        request=request,
        top=top,
        children=children,
        shelves_count=shelves_count,
        drawers_count=drawers_count,
        containers_count=containers_count,
        q=q or "",
        results=results,
        matched_items=matched_items,   # make sure this is here
        title=APP_TITLE
    )




# -------------- Nodes --------------
@app.post("/nodes")
def create_node(name: str = Form(...), type: str = Form(...), parent_id: str | None = Form(None), note: str = Form("")):
    conn = get_db(); cur = conn.cursor()

    parent_type = None
    if parent_id:
        cur.execute("SELECT type FROM nodes WHERE id=?", (parent_id,))
        p = cur.fetchone()
        if not p:
            conn.close(); raise HTTPException(status_code=400, detail="Parent node not found")
        parent_type = p["type"]

    allowed = ALLOWED_NODE_CHILDREN.get(parent_type or "ROOT", set())
    if type not in allowed:
        conn.close(); raise HTTPException(status_code=400, detail=f"{type} not allowed under {parent_type or 'ROOT'}")

    nid = uuid4().hex[:8].upper()
    cur.execute("INSERT INTO nodes(id, type, name, parent_id, note) VALUES (?, ?, ?, ?, ?)",
                (nid, type, name.strip(), parent_id, note.strip()))
    conn.commit(); conn.close()
    return RedirectResponse(url=f"/node/{nid}", status_code=303)

@app.get("/node/{node_id}", response_class=HTMLResponse)
def view_node(request: Request, node_id: str):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM nodes WHERE id=?", (node_id,))
    node = cur.fetchone()
    if not node:
        conn.close(); raise HTTPException(status_code=404, detail="Node not found")

    # NEW: parent (for Shelf/Drawer)
    parent = None
    if node["parent_id"]:
        cur.execute("SELECT id, name, type, note FROM nodes WHERE id=?", (node["parent_id"],))
        parent = cur.fetchone()

    # child nodes
    cur.execute("SELECT * FROM nodes WHERE parent_id=? ORDER BY type, name", (node_id,))
    subs = cur.fetchall()

    # containers under this node
    cur.execute("SELECT * FROM containers WHERE parent_id=? ORDER BY type, name", (node_id,))
    containers = cur.fetchall()

    # NEW: items count per container (to show "Items: N" or "No items")
    # Items count per container (robust LEFT JOIN by current node)
    items_count = {}
    cur.execute("""
        SELECT c.id AS cid, COUNT(i.id) AS cnt
        FROM containers c
        LEFT JOIN items i ON i.container_id = c.id
        WHERE c.parent_id = ?
        GROUP BY c.id
    """, (node_id,))
    for r in cur.fetchall():
        # sqlite3.Row: access by key; if tuple, use r[0], r[1]
        cid = r["cid"] if isinstance(r, dict) or hasattr(r, "keys") else r[0]
        cnt = r["cnt"] if isinstance(r, dict) or hasattr(r, "keys") else r[1]
        items_count[cid] = cnt

    # --- counts of containers under each child shelf/drawer
    counts = {}          # total per child
    bytype = {}          # per child -> { 'Box':n, 'Organizator':m, 'InPlace':k }
    single_names = {}    # per child -> container name if exactly 1

    child_ids = [s["id"] for s in subs]
    if child_ids:
        placeholders = ",".join("?" * len(child_ids))

        # per-type counts
        cur.execute(f"""
            SELECT parent_id, type, COUNT(*) AS cnt
            FROM containers
            WHERE parent_id IN ({placeholders})
            GROUP BY parent_id, type
        """, child_ids)
        for r in cur.fetchall():
            pid, typ, cnt = r["parent_id"], r["type"], r["cnt"]
            bytype.setdefault(pid, {})[typ] = cnt
            counts[pid] = counts.get(pid, 0) + cnt

        # collect names to show the single one if only one exists
        cur.execute(f"""
            SELECT parent_id, name
            FROM containers
            WHERE parent_id IN ({placeholders})
            ORDER BY name
        """, child_ids)
        names_map = {}
        for r in cur.fetchall():
            names_map.setdefault(r["parent_id"], []).append(r["name"])
        for pid, names in names_map.items():
            if len(names) == 1:
                single_names[pid] = names[0]

    conn.close()
    return render(
        "node.html",
        request=request,
        node=node,
        parent=parent,
        subs=subs,
        containers=containers,
        counts=counts,            # already used in your template
        bytype=bytype,            # NEW
        single_names=single_names,# NEW
        items_count=items_count,
        title=f"{APP_TITLE} · {node['name']}"
    )


@app.post("/node/{node_id}/delete")
def delete_node(node_id: str):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, parent_id FROM nodes WHERE id=?", (node_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            raise HTTPException(status_code=404, detail="Node not found")
        parent_id = row["parent_id"]

        delete_node_recursive(conn, node_id)
        conn.commit()
    finally:
        conn.close()

    # redirect home for top-level, or back to parent if Shelf/Drawer
    return RedirectResponse(url=f"/node/{parent_id}" if parent_id else "/", status_code=303)




# -------------- Containers --------------
@app.post("/containers")
def create_container(name: str = Form(...), type: str = Form(...), parent_id: str = Form(...), note: str = Form("")):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT type FROM nodes WHERE id=?", (parent_id,))
    p = cur.fetchone()
    if not p:
        conn.close(); raise HTTPException(status_code=400, detail="Parent node not found")
    parent_type = p["type"]

    allowed = ALLOWED_CONTAINER_BY_PARENT.get(parent_type, set())
    if type not in allowed:
        conn.close(); raise HTTPException(status_code=400, detail=f"{type} not allowed under {parent_type}")

    cid = uuid4().hex[:8].upper()
    cur.execute("INSERT INTO containers(id, type, name, parent_id, note) VALUES (?, ?, ?, ?, ?)",
                (cid, type, name.strip(), parent_id, note.strip()))
    conn.commit(); conn.close()

    # QR with label
    save_qr_with_label(cid, name.strip())


    return RedirectResponse(url=f"/container/{cid}", status_code=303)

@app.get("/container/{cont_id}", response_class=HTMLResponse)
def view_container(request: Request, cont_id: str):
    conn = get_db(); cur = conn.cursor()

    # this container
    cur.execute("SELECT * FROM containers WHERE id=?", (cont_id,))
    cont = cur.fetchone()
    if not cont:
        conn.close(); raise HTTPException(status_code=404, detail="Container not found")

    # parent node (Shelf/Drawer)
    parent = None
    top = None
    if cont["parent_id"]:
        cur.execute("SELECT id, name, type, note, parent_id FROM nodes WHERE id=?", (cont["parent_id"],))
        parent = cur.fetchone()

        # top-level (Cabinet/Wardrobe)
        if parent and parent["parent_id"]:
            cur.execute("SELECT id, name, type, note FROM nodes WHERE id=?", (parent["parent_id"],))
            top = cur.fetchone()

    cur.execute("SELECT * FROM items WHERE container_id=? ORDER BY name", (cont_id,))
    items = cur.fetchall()

    # types for UI
    item_types = list_item_types(conn)

    # dynamic values for all items in this container
    item_ids = [it["id"] for it in items]
    item_dyn = values_for_items(conn, item_ids)





    # --- move targets for THIS container (Shelves/Drawers that allow this type) ---
    allowed_parent_types = [ptype for ptype, allowed in ALLOWED_CONTAINER_BY_PARENT.items() if cont["type"] in allowed]
    move_nodes = []
    if allowed_parent_types:
        placeholders = ",".join("?" * len(allowed_parent_types))
        cur.execute(f"""
            SELECT
                n.id, n.name, n.type, n.note AS note,                 -- target shelf/drawer + its note
                p.name AS parent_name, p.type AS parent_type, p.note AS parent_note  -- its cabinet/wardrobe + note
            FROM nodes n
            LEFT JOIN nodes p ON p.id = n.parent_id
            WHERE n.type IN ({placeholders})
            ORDER BY COALESCE(p.name,''), n.name
        """, allowed_parent_types)
        move_nodes = cur.fetchall()

    # --- move targets for ITEMS (all other containers) ---
    cur.execute("""
        SELECT
            c.id, c.name, c.type, c.note AS note,                      -- dest container + note
            p.name AS parent_name, p.type AS parent_type, p.note AS parent_note,  -- shelf/drawer + note
            t.name AS top_name,  t.type AS top_type,  t.note AS top_note          -- cabinet/wardrobe + note (nullable)
        FROM containers c
        JOIN nodes p ON p.id = c.parent_id
        LEFT JOIN nodes t ON t.id = p.parent_id
        WHERE c.id != ?
        ORDER BY COALESCE(t.name,''), COALESCE(p.name,''), c.name
    """, (cont_id,))
    move_containers = cur.fetchall()



    conn.close()
    return render(
        "container.html",
        request=request,
        cont=cont,
        items=items,
        parent=parent,
        top=top,
        item_types=item_types,   # NEW
        item_dyn=item_dyn,       # NEW {item_id: [{label,value}]}
        move_nodes=move_nodes,
        move_containers=move_containers,
        title=f"{APP_TITLE} · {cont['name']}"
    )


@app.post("/container/{cont_id}/items")
async def add_item(cont_id: str,
                   request: Request,
                   name: str = Form(...),
                   qty: int = Form(1),
                   note: str = Form(""),
                   type_id: str | None = Form(None)):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id FROM containers WHERE id=?", (cont_id,))
    if not cur.fetchone():
        conn.close(); raise HTTPException(status_code=404, detail="Container not found")

    # create item
    cur.execute("INSERT INTO items(container_id, name, qty, note, type_id) VALUES (?, ?, ?, ?, ?)",
                (cont_id, name.strip(), qty, note.strip(), type_id))
    item_id = cur.lastrowid

    # dynamic fields (if any)
    form = await request.form()
    if type_id:
        fields = fields_for_type(conn, type_id)
        for f in fields:
            key = f"field_{f['id']}"
            if key in form:
                val = str(form[key]).strip()
                cur.execute("INSERT INTO item_field_values(item_id, field_id, value) VALUES (?, ?, ?)",
                            (item_id, f["id"], val))

    conn.commit(); conn.close()
    return RedirectResponse(url=f"/container/{cont_id}", status_code=303)


@app.post("/container/{cont_id}/items/{item_id}/delete")
def delete_item(cont_id: str, item_id: int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM items WHERE id=? AND container_id=?", (item_id, cont_id))
    conn.commit(); conn.close()
    return RedirectResponse(url=f"/container/{cont_id}", status_code=303)

@app.post("/container/{cont_id}/qr/refresh")
def refresh_qr_container(cont_id: str):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT name FROM containers WHERE id=?", (cont_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Container not found")

    save_qr_with_label(cont_id, row["name"])
    # Add a timestamp query param so the browser fetches the new file
    return RedirectResponse(url=f"/container/{cont_id}?ts={int(time.time())}", status_code=303)

@app.get("/container/{cont_id}/qr.png")
def container_qr_png(cont_id: str):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT name FROM containers WHERE id=?", (cont_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Container not found")

    png = build_qr_with_label_bytes(qr_for_container(cont_id), row["name"])
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "no-store, max-age=0"}  # no caching
    )

@app.post("/container/{cont_id}/delete")
def delete_container(cont_id: str):
    conn = get_db(); cur = conn.cursor()

    # Find container & parent
    cur.execute("SELECT id, parent_id FROM containers WHERE id=?", (cont_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Container not found")
    parent_id = row["parent_id"]

    # Delete items
    cur.execute("DELETE FROM items WHERE container_id=?", (cont_id,))
    # Delete the container
    cur.execute("DELETE FROM containers WHERE id=?", (cont_id,))

    # Remove QR png if exists
    try:
        png = os.path.join(QRCODES_DIR, f"{cont_id}.png")
        if os.path.exists(png):
            os.remove(png)
    except Exception:
        pass

    conn.commit()
    conn.close()

    # Go back to the parent Shelf/Drawer page
    return RedirectResponse(url=f"/node/{parent_id}", status_code=303)


from fastapi import FastAPI, Request, Form, HTTPException
# (already imported above)

@app.post("/container/{cont_id}/move")
def move_container(cont_id: str, dest_parent_id: str = Form(...)):
    """Move a container (Box/Organizator/InPlace) to another Shelf/Drawer."""
    conn = get_db(); cur = conn.cursor()

    # Check container
    cur.execute("SELECT id, type FROM containers WHERE id=?", (cont_id,))
    c = cur.fetchone()
    if not c:
        conn.close(); raise HTTPException(status_code=404, detail="Container not found")

    # Check destination node
    cur.execute("SELECT id, type FROM nodes WHERE id=?", (dest_parent_id,))
    dest = cur.fetchone()
    if not dest:
        conn.close(); raise HTTPException(status_code=400, detail="Destination node not found")

    # Enforce typing rules: container type must be allowed under destination node type
    allowed = ALLOWED_CONTAINER_BY_PARENT.get(dest["type"], set())
    if c["type"] not in allowed:
        conn.close()
        raise HTTPException(status_code=400, detail=f"{c['type']} not allowed under {dest['type']}")

    # Move
    cur.execute("UPDATE containers SET parent_id=? WHERE id=?", (dest_parent_id, cont_id))
    conn.commit(); conn.close()
    return RedirectResponse(url=f"/container/{cont_id}", status_code=303)


@app.post("/container/{cont_id}/items/move")
def move_item(cont_id: str, item_id: int = Form(...), dest_container_id: str = Form(...)):
    """Move an item to another container."""
    conn = get_db(); cur = conn.cursor()

    # Verify item exists
    cur.execute("SELECT id FROM items WHERE id=?", (item_id,))
    if not cur.fetchone():
        conn.close(); raise HTTPException(status_code=404, detail="Item not found")

    # Verify dest container exists
    cur.execute("SELECT id FROM containers WHERE id=?", (dest_container_id,))
    if not cur.fetchone():
        conn.close(); raise HTTPException(status_code=400, detail="Destination container not found")

    # Move item
    cur.execute("UPDATE items SET container_id=? WHERE id=?", (dest_container_id, item_id))
    conn.commit(); conn.close()
    return RedirectResponse(url=f"/container/{dest_container_id}", status_code=303)



@app.post("/container/{cont_id}/items/{item_id}/update")
async def update_item(cont_id: str, item_id: int, request: Request,
                      name: str = Form(...),
                      qty: int = Form(1),
                      note: str = Form(""),
                      type_id: str | None = Form(None)):
    conn = get_db(); cur = conn.cursor()
    # verify item
    cur.execute("SELECT id FROM items WHERE id=? AND container_id=?", (item_id, cont_id))
    if not cur.fetchone():
        conn.close(); raise HTTPException(status_code=404, detail="Item not found")

    cur.execute("UPDATE items SET name=?, qty=?, note=?, type_id=? WHERE id=?",
                (name.strip(), qty, note.strip(), type_id, item_id))

    # wipe previous dynamic values, re-insert from form
    cur.execute("DELETE FROM item_field_values WHERE item_id=?", (item_id,))
    form = await request.form()
    if type_id:
        fields = fields_for_type(conn, type_id)
        for f in fields:
            key = f"field_{f['id']}"
            if key in form:
                val = str(form[key]).strip()
                cur.execute("INSERT INTO item_field_values(item_id, field_id, value) VALUES (?, ?, ?)",
                            (item_id, f["id"], val))

    conn.commit(); conn.close()
    return RedirectResponse(url=f"/container/{cont_id}", status_code=303)







@app.get("/api/item-types")
def api_item_types():
    conn = get_db()
    types = list_item_types(conn)
    conn.close()
    return JSONResponse([dict(t) for t in types])

@app.get("/api/item-types/{type_id}/fields")
def api_item_type_fields(type_id: str):
    conn = get_db()
    fields = fields_for_type(conn, type_id)
    conn.close()
    return JSONResponse(fields)

@app.get("/api/items/{item_id}")
def api_item_detail(item_id: int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT i.id, i.name, i.qty, i.note, i.type_id, t.name AS type_name
        FROM items i
        LEFT JOIN item_types t ON t.id = i.type_id
        WHERE i.id=?
    """, (item_id,))
    it = cur.fetchone()
    if not it:
        conn.close()
        raise HTTPException(status_code=404, detail="Item not found")

    # dynamic values
    cur.execute("""
        SELECT f.id AS field_id, f.label, f.kind, v.value
        FROM item_field_values v
        JOIN item_fields f ON f.id=v.field_id
        WHERE v.item_id=?
        ORDER BY f.ord, f.label
    """, (item_id,))
    fields = [dict(r) for r in cur.fetchall()]
    conn.close()
    return JSONResponse({"item": dict(it), "fields": fields})

@app.get("/types", response_class=HTMLResponse)
def types_page(request: Request):
    conn = get_db(); cur = conn.cursor()
    types = list_item_types(conn)
    # For each type, list fields
    type_fields = {}
    for t in types:
        type_fields[t["id"]] = fields_for_type(conn, t["id"])
    conn.close()
    return render("types.html", request=request, types=types, type_fields=type_fields, title=f"{APP_TITLE} · Types")

@app.post("/types")
def create_type(name: str = Form(...)):
    conn = get_db(); cur = conn.cursor()
    tid = uuid4().hex[:8].upper()
    cur.execute("INSERT INTO item_types(id, name) VALUES (?, ?)", (tid, name.strip()))
    conn.commit(); conn.close()
    return RedirectResponse(url="/types", status_code=303)

@app.post("/types/{type_id}/delete")
def delete_type(type_id: str):
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM item_types WHERE id=?", (type_id,))
    conn.commit(); conn.close()
    return RedirectResponse(url="/types", status_code=303)

@app.post("/types/{type_id}/fields")
def create_field(type_id: str,
                 name: str = Form(""),
                 label: str = Form(...),
                 kind: str = Form(...),
                 required: int = Form(0),
                 options: str = Form("")):
    if kind not in ("text","number","select","date","checkbox"):
        raise HTTPException(status_code=400, detail="Invalid kind")

    conn = get_db(); cur = conn.cursor()
    fid = uuid4().hex[:8].upper()
    # key: optional → auto from label if blank; always slugify + ensure unique
    key_in = (name or "").strip()
    base_key = slugify_label(key_in or label)
    key = ensure_unique_field_key(conn, type_id, base_key)

    # options only for select
    opts_json = "[]"
    if kind == "select":
        arr = [o.strip() for o in (options or "").split(",") if o.strip()]
        opts_json = json.dumps(arr)

    # ord = biggest+1
    cur.execute("SELECT COALESCE(MAX(ord), 0) FROM item_fields WHERE type_id=?", (type_id,))
    ordv = (cur.fetchone()[0] or 0) + 1

    cur.execute("""
        INSERT INTO item_fields(id, type_id, name, label, kind, required, options, ord)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (fid, type_id, key, label.strip(), kind, 1 if required else 0, opts_json, ordv))
    conn.commit(); conn.close()
    return RedirectResponse(url=f"/types/{type_id}", status_code=303)



# View a single type (detail page)
@app.get("/types/{type_id}", response_class=HTMLResponse)
def type_detail(request: Request, type_id: str):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id, name FROM item_types WHERE id=?", (type_id,))
    t = cur.fetchone()
    if not t:
        conn.close()
        raise HTTPException(status_code=404, detail="Type not found")
    fields = fields_for_type(conn, type_id)
    conn.close()
    return render("type.html", request=request, t=t, fields=fields, title=f"{APP_TITLE} · {t['name']}")

# (Optional) rename a type (simple)
@app.post("/types/{type_id}/update")
def update_type(type_id: str, name: str = Form(...)):
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE item_types SET name=? WHERE id=?", (name.strip(), type_id))
    conn.commit(); conn.close()
    return RedirectResponse(url=f"/types/{type_id}", status_code=303)

# Update a field inline
@app.post("/fields/{field_id}/update")
def update_field(field_id: str,
                 type_id: str = Form(...),
                 label: str = Form(...),
                 name: str = Form(""),
                 kind: str = Form(...),
                 required: int = Form(0),
                 options: str = Form(""),
                 ord: int = Form(0)):
    if kind not in ("text","number","select","date","checkbox"):
        raise HTTPException(status_code=400, detail="Invalid kind")

    conn = get_db(); cur = conn.cursor()

    # compute key (optional): if blank → from label; always slugify & ensure unique (excluding self)
    key_in = (name or "").strip()
    base_key = slugify_label(key_in or label)
    key = ensure_unique_field_key(conn, type_id, base_key, exclude_field_id=field_id)

    # options only for select
    opts_json = "[]"
    if kind == "select":
        arr = [o.strip() for o in (options or "").split(",") if o.strip()]
        opts_json = json.dumps(arr)

    cur.execute("""
        UPDATE item_fields
           SET label=?, name=?, kind=?, required=?, options=?, ord=?
         WHERE id=? AND type_id=?
    """, (label.strip(), key, kind, 1 if required else 0, opts_json, ord, field_id, type_id))
    conn.commit(); conn.close()
    return RedirectResponse(url=f"/types/{type_id}", status_code=303)


# Delete a field
@app.post("/fields/{field_id}/delete")
def delete_field(field_id: str, type_id: str = Form(...)):
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM item_fields WHERE id=?", (field_id,))
    conn.commit(); conn.close()
    return RedirectResponse(url=f"/types/{type_id}", status_code=303)

@app.post("/types/{type_id}/fields/reorder")
def reorder_fields(type_id: str, payload: dict = Body(...)):
    order = payload.get("order")
    if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
        raise HTTPException(status_code=400, detail="Invalid order payload")

    conn = get_db(); cur = conn.cursor()
    # Only reorder fields that belong to this type (ignore stray ids)
    qmarks = ",".join("?" * len(order)) if order else ""
    valid = set()
    if order:
        cur.execute(f"SELECT id FROM item_fields WHERE type_id=? AND id IN ({qmarks})", [type_id, *order])
        valid = {r["id"] for r in cur.fetchall()}

    pos = 1
    for fid in order:
        if fid in valid:
            cur.execute("UPDATE item_fields SET ord=? WHERE id=? AND type_id=?", (pos, fid, type_id))
            pos += 1

    conn.commit(); conn.close()
    return JSONResponse({"ok": True})