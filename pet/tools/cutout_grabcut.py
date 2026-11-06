import os
import numpy as np
import cv2

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 素材仓库默认与代码仓库同级；放在别处时用 DFY_ASSETS_DIR 指定
SRC = os.environ.get("DFY_ASSETS_DIR") or os.path.join(
    os.path.dirname(_REPO), "dafeiyu-assets", "avatars"
)
THEME = os.path.join(os.environ["APPDATA"], "clawd-on-desk", "themes", "dafeiyu")
ASSETS = os.path.join(THEME, "assets")

MAP = {
    "idle.png": "05_站姿.png",
    "thinking.png": "09_双手交叠.png",
    "working.png": "08_抬手.png",
    "juggling.png": "03_抱鲸鱼.png",
    "attention.png": "01_招手微笑.png",
    "notification.png": "04_眨眼.png",
    "error.png": "12_抱鲸鱼Q版.png",
    "sleeping.png": "16_床上抱鲸.png",
    "waking.png": "17_被窝时刻.png",
    "click-left.png": "13_眨眼Q版.png",
    "click-right.png": "06_捧碗站姿.png",
    "double.png": "14_夜间陪伴.png",
    "idle-a.png": "10_安静微笑.png",
    "idle-b.png": "04_眨眼.png",
    "eat.png": "02_吃饭.png",
}


def read(path):
    data = np.fromfile(path, dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_UNCHANGED)


def write(path, img):
    ext = os.path.splitext(path)[1]
    ok, buf = cv2.imencode(ext, img)
    buf.tofile(path)


def cutout(src_path, dst_path):
    bgr = read(src_path)
    h, w = bgr.shape[:2]
    m = 6
    mask = np.zeros((h, w), np.uint8)
    mask[m:h - m, m:w - m] = cv2.GC_PR_FGD
    bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
    cv2.grabCut(bgr, mask, (m, m, w - 2 * m, h - 2 * m), bgd, fgd, 6, cv2.GC_INIT_WITH_MASK)
    solid = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
    solid = cv2.morphologyEx(solid, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    solid = cv2.morphologyEx(solid, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    # 只保留与中心相连的最大块，避免把背景中的杂色当主体
    n, labels, stats, _ = cv2.connectedComponentsWithStats(solid, 8)
    cx, cy = w // 2, h // 2
    keep = labels[cy, cx]
    if keep == 0 and n > 1:
        keep = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    solid = np.where(labels == keep, 255, 0).astype(np.uint8)
    alpha = cv2.GaussianBlur(solid, (3, 3), 0)
    out = cv2.cvtColor(bgr, cv2.COLOR_BGRA2BGRA) if bgr.shape[2] == 4 else cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA)
    out[:, :, 3] = alpha
    write(dst_path, out)
    ys, xs = np.where(solid > 0)
    return (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)


if __name__ == "__main__":
    os.makedirs(ASSETS, exist_ok=True)
    for dst, src in MAP.items():
        print(dst, cutout(os.path.join(SRC, src), os.path.join(ASSETS, dst)))
