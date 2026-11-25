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
# CiRA runs this script with fresh globals every frame, so keep state on
# the builtins module (shared by the whole interpreter), one entry per camera.
import builtins
G = builtins.__dict__.setdefault("_spk_overlay", {}).setdefault(
    CAM, {"spk_state": {}, "spk_fps": 0.0, "spk_t": time.time()})


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

        slots = ROI[CAM]
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
        right = "%s  |  AI LIVE  |  %s" % (CAM, time.strftime("%H:%M:%S"))
        (tw, _), _ = cv2.getTextSize(right, cv2.FONT_HERSHEY_SIMPLEX, 0.6 * k, th)
        cv2.putText(img, right, (W - tw - int(14 * k), int(30 * k)), cv2.FONT_HERSHEY_SIMPLEX, 0.6 * k,
                    (200, 190, 170), max(1, th - 1), cv2.LINE_AA)
        # NOTE: don't write back into payload here - adding keys to payload from
        # this node crashed CiRA CORE 1.9.4 (segfault). Node-RED does the counting.
except Exception as e:
    print("overlay error:", e)
