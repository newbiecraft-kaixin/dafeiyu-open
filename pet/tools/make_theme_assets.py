import os
import shutil
from PIL import Image, ImageDraw

SRC = r"C:\path\to\dafeiyu-assets\avatars"
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
    "sleeping.png": "15_睡衣晚安.png",
    "waking.png": "18_睡前时光.png",
    "click-left.png": "13_眨眼Q版.png",
    "click-right.png": "06_捧碗站姿.png",
    "double.png": "14_夜间陪伴.png",
    "idle-a.png": "10_安静微笑.png",
    "idle-b.png": "04_眨眼.png",
}


def cutout(src_path, dst_path):
    im = Image.open(src_path).convert("RGBA")
    w, h = im.size
    for seed in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        ImageDraw.floodfill(im, seed, (0, 0, 0, 0), thresh=24)
    # 边缘贴近背景色的残留像素做一次半透明处理，减少浅蓝描边
    px = im.load()
    bg = (231, 241, 253)
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a == 0:
                continue
            if abs(r - bg[0]) <= 10 and abs(g - bg[1]) <= 10 and abs(b - bg[2]) <= 10:
                near = False
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < w and 0 <= ny < h and px[nx, ny][3] == 0:
                        near = True
                        break
                if near:
                    px[x, y] = (r, g, b, 0)
    im.save(dst_path)
    return im.getbbox()


if __name__ == "__main__":
    os.makedirs(ASSETS, exist_ok=True)
    bboxes = {}
    for dst, src in MAP.items():
        bboxes[dst] = cutout(os.path.join(SRC, src), os.path.join(ASSETS, dst))
    for k, v in bboxes.items():
        print(k, v)
    # 方便后续主题卡预览
    shutil.copyfile(os.path.join(ASSETS, "idle.png"), os.path.join(THEME, "preview.png"))
