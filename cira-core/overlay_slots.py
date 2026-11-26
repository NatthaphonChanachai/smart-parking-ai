# =====================================================================
#  Smart Parking AI - Slot Overlay for CiRA CORE (PythonScript node)
#  Draws the parking-slot grid (same ROI as Node-RED "Init ROI") on top
#  of the video: green = free, red = occupied, grey = no detection,
#  blue = free accessible (PWD) slot - never recommended to general drivers,
#  plus slot IDs, the recommended slot, and a header with FREE x/y.
#
#  Wiring (each scene):  DeepDetect ──► MqttPub        (unchanged)
#                                   └─► PythonScript   (this code)
#  Uses the variables CiRA gives the PythonScript node:
#     payload : dict  (contains payload["DeepDetect"]["objects"])
#     img     : OpenCV image (BGR) - we draw on it in place
#  Only change CAM below for each scene.
# =====================================================================
import time
import cv2

CAM = "2A-C04"          # <<< 2A-C01 / 2A-C02 / 2A-C03 / 2A-C04
CONF = 0.5              # same threshold as Node-RED Slot Logic
HOLD = 3                # frames a new status must persist before shown

ROI = {
    "2A-C01": [("01", 0.00, 0.33, None, None, "pwd"), ("02", 0.33, 0.67, None, None, "normal"),
               ("03", 0.67, 1.00, None, None, "normal")],
    "2A-C02": [("04", 0.00, 0.65, None, None, "pwd"), ("05", 0.65, 1.00, None, None, "normal")],
    "2A-C03": [("06", 0.00, 0.33, None, None, "normal"), ("07", 0.33, 0.66, None, None, "normal"),
               ("08", 0.66, 1.00, None, None, "normal")],
    # webcam (landscape): top row 09-14, bottom row 15-20 - same values as Node-RED Init ROI
    "2A-C04": [("%02d" % (9 + i), x1, x2, 0.00, 0.40, "normal") for i, (x1, x2) in enumerate(
                   [(.07, .19), (.19, .30), (.30, .40), (.40, .51), (.51, .61), (.61, .75)])] +
              [("%02d" % (15 + i), x1, x2, 0.60, 1.00, "normal") for i, (x1, x2) in enumerate(
                   [(.06, .20), (.20, .30), (.30, .42), (.42, .52), (.52, .63), (.63, .78)])],
}
FLOOR = {"2A-C01": "FLOOR 1", "2A-C02": "FLOOR 2", "2A-C03": "FLOOR 3", "2A-C04": "FLOOR 4"}
OCC_NAMES = ("occupied", "car")
FREE_NAMES = ("empty", "avalible", "available", "free")

GREEN, RED, GREY = (90, 211, 34), (94, 77, 255), (150, 150, 150)   # BGR
BLUE = (235, 140, 40)   # free accessible (PWD) slot - shown, never recommended
ORANGE, WHITE, DARK = (59, 173, 255), (255, 255, 255), (24, 18, 10)

# ---- state kept between frames ---------------------------------------
# CiRA CORE starts this script with a FRESH Python state on every frame
# (even builtins are reset), so anything that must survive between frames
# (smoothing, the auto-ROI buffer) is kept in a small JSON file per camera.
import os, json, tempfile
STATE_FILE = os.path.join(tempfile.gettempdir(), "spk_overlay_%s.json" % CAM)
try:
    with open(STATE_FILE, encoding="utf-8") as _fh:
        G = json.load(_fh)
except Exception:
    G = {}
G.setdefault("spk_state", {})


def _save_state():
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(G, fh, separators=(",", ":"))
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print("save state:", e)


def slot_status(slots, objs, W, H):
    """best confidence of each class whose box centre falls inside a slot"""
    res = {}
    for sid, x1, x2, y1, y2, _ in slots:
        occ = free = 0.0
        for o in objs:
            cx, cy = o.get("x", 0) / W, o.get("y", 0) / H
            if not (x1 <= cx < x2):
                continue
            if y1 is not None and not (y1 <= cy < y2):
                continue
            nm = str(o.get("name", "")).strip().lower()
            c = float(o.get("confidence", 0))
            if nm in OCC_NAMES:
                occ = max(occ, c)
            elif nm in FREE_NAMES:
                free = max(free, c)
        if occ >= CONF and occ >= free:
            res[sid] = ("occupied", occ)
        elif free >= CONF:
            res[sid] = ("free", free)
        else:
            res[sid] = ("unknown", 0.0)
    return res


def smooth(sid, st):
    s = G["spk_state"].setdefault(sid, [st, st, 0])
    if st == s[0]:
        s[2] = 0
    elif st == s[1]:
        s[2] += 1
        if s[2] >= HOLD:
            s[0], s[2] = st, 0
    else:
        s[1], s[2] = st, 1
    return s[0]


def label(im, text, org, scale, fg, bg, th):
    (tw, tht), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_DUPLEX, scale, th)
    x, y = org
    cv2.rectangle(im, (x, y - tht - base - 4), (x + tw + 10, y + 2), bg, -1)
    cv2.putText(im, text, (x + 5, y - base + 1), cv2.FONT_HERSHEY_DUPLEX, scale, fg, th, cv2.LINE_AA)


# ---- AUTO ROI: learn the slot layout from where the AI sees slots ---------
# The model boxes every slot (occupied or empty), so the box centres of the
# last ~45 frames tell us where the real slots are. Every 3 s we rebuild the
# layout; it is only used when it finds exactly as many slots as the floor has
# (otherwise the last good layout stays). The result is shared with Node-RED
# through parking-roi.json so the dashboard always matches what CiRA draws.
AUTO_ROI = True
ROI_FILE = r"C:\Users\mint\.node-red\parking-roi.json"
WINDOW, EVERY = 45, 3.0


def _median(a):
    a = sorted(a)
    return a[len(a) // 2] if a else 0.0


def _file_roi():
    try:
        with open(ROI_FILE, encoding="utf-8") as fh:
            s = json.load(fh).get(CAM)
        if s and len(s) == len(ROI[CAM]):
            return [(d["id"], d["x1"], d["x2"], d.get("y1"), d.get("y2"), d.get("type", "normal")) for d in s]
    except Exception:
        pass
    return None


def _save_roi(slots):
    try:
        try:
            with open(ROI_FILE, encoding="utf-8") as fh:
                o = json.load(fh)
        except Exception:
            o = {}
        o[CAM] = [dict(id=s[0], x1=s[1], x2=s[2], type=s[5], **({} if s[3] is None else {"y1": s[3], "y2": s[4]}))
                  for s in slots]
        tmp = ROI_FILE + "." + CAM + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(o, fh, indent=1)
        os.replace(tmp, ROI_FILE)
    except Exception as e:
        print("save roi:", e)


def _learn(frames, base):
    """frames: list of [(cx, cy, w, h)] normalised -> new slot list or None"""
    pts = [p for fr in frames for p in fr]
    if len(frames) < 10 or not pts:
        return None
    mw, mh = _median([p[2] for p in pts]) or 0.1, _median([p[3] for p in pts]) or 0.1
    cl = []                                     # [sx, sy, sw, sh, n, set(frames)]
    for fi, fr in enumerate(frames):
        for x, y, w, h in fr:
            best, bd = None, 0.5
            for c in cl:
                d = (((x - c[0] / c[4]) / mw) ** 2 + ((y - c[1] / c[4]) / mh) ** 2) ** 0.5
                if d < bd:
                    best, bd = c, d
            if best is None:
                best = [0.0, 0.0, 0.0, 0.0, 0, set()]
                cl.append(best)
            best[0] += x; best[1] += y; best[2] += w; best[3] += h; best[4] += 1; best[5].add(fi)
    keep = [(c[0] / c[4], c[1] / c[4], c[2] / c[4], c[3] / c[4]) for c in cl if len(c[5]) >= len(frames) * 0.4]
    if len(keep) != len(base):
        return None
    base_rows = len(set(b[3] for b in base))   # 1 for full-height cameras (y = None)
    if base_rows == 1:
        rows = [sorted(keep, key=lambda c: c[0])]   # one row of slots: just left -> right
    else:
        keep.sort(key=lambda c: c[1])
        rows = []
        for c in keep:
            if rows and abs(c[1] - sum(r[1] for r in rows[-1]) / len(rows[-1])) < mh * 0.5:
                rows[-1].append(c)
            else:
                rows.append([c])
        if len(rows) != base_rows:
            return None
    for r in rows:
        r.sort(key=lambda c: c[0])
    single = base_rows == 1 and base[0][3] is None
    ry = [[min(c[1] - c[3] * 0.6 for c in r), max(c[1] + c[3] * 0.6 for c in r)] for r in rows]
    for i in range(len(ry) - 1):
        if ry[i][1] > ry[i + 1][0]:
            m = (ry[i][1] + ry[i + 1][0]) / 2
            ry[i][1] = ry[i + 1][0] = m
    cl3 = lambda v: round(min(1.0, max(0.0, v)), 3)
    out = []
    for ri, r in enumerate(rows):
        for i, c in enumerate(r):
            x1 = (0.0 if single else c[0] - c[2] * 0.6) if i == 0 else (r[i - 1][0] + c[0]) / 2
            x2 = (1.0 if single else c[0] + c[2] * 0.6) if i == len(r) - 1 else (c[0] + r[i + 1][0]) / 2
            b = base[len(out)]                  # keep slot id + type in reading order
            out.append((b[0], cl3(x1), cl3(x2), None if single else cl3(ry[ri][0]),
                        None if single else cl3(ry[ri][1]), b[5]))
            # sanity: a slot may drift with the camera, but not jump to another slot's place
            if abs(c[0] - (b[1] + b[2]) / 2) > 0.25 or (b[3] is not None and abs(c[1] - (b[3] + b[4]) / 2) > 0.25):
                return None
    return out


def roi_for(objs, W0, H0, now):
    if "roi" not in G:
        saved = _file_roi()
        G["roi"] = saved or [list(s) for s in ROI[CAM]]
        G["roi_src"] = "SAVED" if saved else "DEFAULT"
        G["buf"], G["cal_t"], G["buf_t"] = [], now, 0
    if AUTO_ROI:
        names = OCC_NAMES + FREE_NAMES
        if now - G.get("buf_t", 0) >= 0.12:     # sample ~8 frames/s is plenty
            G["buf_t"] = now
            G["buf"].append([[round(o["x"] / W0, 4), round(o["y"] / H0, 4), round(o.get("w", 0) / W0, 4),
                              round(o.get("h", 0) / H0, 4)] for o in objs
                             if str(o.get("name", "")).strip().lower() in names and float(o.get("confidence", 0)) >= 0.3])
            del G["buf"][:-WINDOW]
        if now - G["cal_t"] >= EVERY:
            G["cal_t"] = now
            new = _learn(G["buf"], ROI[CAM])
            if new:
                moved = max(abs(a - b) for n, o in zip(new, G["roi"]) for a, b in
                            zip((n[1], n[2], n[3] or 0, n[4] or 0), (o[1], o[2], o[3] or 0, o[4] or 0)))
                G["roi_src"] = "AUTO"
                if moved > 0.015:               # only update/save when the layout really moved
                    G["roi"] = [list(s) for s in new]
                    _save_roi(new)
    return [tuple(s) for s in G["roi"]]


try:
    if img is not None and CAM in ROI:
        H, W = img.shape[:2]
        k = W / 1280.0
        th = max(1, int(round(2 * k)))
        bar_h = int(46 * k)
        dd = payload.get("DeepDetect", {}) if isinstance(payload, dict) else {}
        objs = dd.get("objects", []) or []
        W0, H0 = float(dd.get("im_w") or W), float(dd.get("im_h") or H)

        now = time.time()

        slots = roi_for(objs, W0, H0, now)
        raw = slot_status(slots, objs, W0, H0)
        shown = {sid: smooth(sid, raw[sid][0]) for sid, *_ in slots}

        # recommended slot = first free NORMAL slot only (accessible/PWD slots are never recommended)
        rec = next((s[0] for s in slots if shown[s[0]] == "free" and s[5] == "normal"), None)
        typ_of = {s[0]: s[5] for s in slots}

        def colour(sid, st):
            if st == "free":
                return BLUE if typ_of[sid] == "pwd" else GREEN
            return RED if st == "occupied" else GREY
        pulse = 0.5 + 0.5 * abs(((now * 1.5) % 2) - 1)        # 0.5..1 blink

        # translucent fills
        layer = img.copy()
        for sid, x1, x2, y1, y2, _ in slots:
            p1 = (int(x1 * W) + 3, max(int((y1 or 0.0) * H), bar_h) + 3)
            p2 = (int(x2 * W) - 3, int((y2 if y2 is not None else 1.0) * H) - 3)
            cv2.rectangle(layer, p1, p2, colour(sid, shown[sid]), -1)
        a = 0.22 if slots[0][3] is not None else 0.14     # lighter tint for full-height slots
        cv2.addWeighted(layer, a, img, 1 - a, 0, img)

        # borders + ids
        for sid, x1, x2, y1, y2, typ in slots:
            p1 = (int(x1 * W) + 3, max(int((y1 or 0.0) * H), bar_h) + 3)
            p2 = (int(x2 * W) - 3, int((y2 if y2 is not None else 1.0) * H) - 3)
            st = shown[sid]
            col = colour(sid, st)
            if sid == rec:
                cv2.rectangle(img, p1, p2, ORANGE, th * 3)
                g = int(255 * pulse)
                label(img, "GO >", (p2[0] - int(80 * k), p1[1] + int(28 * k)), 0.6 * k, DARK, (59, g // 2 + 60, g), th)
            else:
                cv2.rectangle(img, p1, p2, col, th)
            tag = sid + (" PWD ONLY" if typ == "pwd" else "")
            label(img, tag, (p1[0] + 4, p1[1] + int(28 * k)), 0.65 * k, WHITE, col, th)
            if st != "unknown":
                cv2.putText(img, "%d%%" % round(raw[sid][1] * 100), (p1[0] + 8, p2[1] - int(8 * k)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5 * k, WHITE, max(1, th - 1), cv2.LINE_AA)

        # header bar
        # count normal slots only; accessible slots are reported separately
        free_n = sum(1 for sid, v in shown.items() if v == "free" and typ_of[sid] == "normal")
        total = sum(1 for t in typ_of.values() if t == "normal")
        pwd_total = len(slots) - total
        pwd_free = sum(1 for sid, v in shown.items() if v == "free" and typ_of[sid] == "pwd")
        cv2.rectangle(img, (0, 0), (W, bar_h), DARK, -1)
        cv2.rectangle(img, (0, bar_h - th), (int(W * free_n / total), bar_h), GREEN, -1)
        txt = "%s   FREE %d/%d" % (FLOOR.get(CAM, CAM), free_n, total)
        if pwd_total:
            txt += "   PWD %d/%d" % (pwd_free, pwd_total)
        cv2.putText(img, txt, (int(14 * k), int(32 * k)), cv2.FONT_HERSHEY_DUPLEX, 0.9 * k,
                    GREEN if free_n else RED, th, cv2.LINE_AA)
        if free_n == 0:
            label(img, "FULL", (int(W * 0.45), int(36 * k)), 0.9 * k, WHITE, (40, 40, int(200 + 55 * pulse)), th)
        right = "%s  |  ROI %s  |  %s" % (CAM, G.get("roi_src", "-"), time.strftime("%H:%M:%S"))
        (tw, _), _ = cv2.getTextSize(right, cv2.FONT_HERSHEY_SIMPLEX, 0.6 * k, th)
        cv2.putText(img, right, (W - tw - int(14 * k), int(30 * k)), cv2.FONT_HERSHEY_SIMPLEX, 0.6 * k,
                    (200, 190, 170), max(1, th - 1), cv2.LINE_AA)
        # NOTE: don't write back into payload here - adding keys to payload from
        # this node crashed CiRA CORE 1.9.4 (segfault). Node-RED does the counting.
except Exception as e:
    print("overlay error:", e)
finally:
    _save_state()
