"""把 QQ 客户端自带的 face_config.json 全量同步成插件可用的表情目录。

背景：插件里原先手写的 EMOJI_TO_FACE 只有 55 条、COMMON_FACES 白名单只有 58 张，
而 QQ 本体有 282 条系统表情（179 张可见 + 103 张隐藏超级表情）和 165 条官方
emoji 映射。手抄必然漏，所以这里直接吃 QQ 的配置：

  输入  <QQ安装目录>/nt_qq/global/nt_data/Emoji/emoji-resource/face_config.json
  输出  face_catalog.json      插件启动时加载的权威目录（sysface + emoji 映射）
        map.txt                 补齐库里缺的可见黄脸（与图片同名的跳过，图片优先）

face_config.json 结构：
  sysface[]  {QSid: "13", QDes: "/呲牙", QHide: "1"?, AniStickerPackName?: "汪汪"}
             QSid 就是 OneBot face id，QHide=1 表示超级表情（qlottie 动画，
             走普通 face 通道只会渲染成静态首帧，所以不进可用白名单）
  emoji[]    {QSid: "😁", QDes: "/呲牙", QCid: "128513", EMCode: "400823"}
             QSid 是 unicode emoji 本体，QDes 是 QQ 输入法给它的联想名——
             这个联想名不一定等于黄脸名（😊 叫"嘿嘿"，黄脸里叫"憨笑"），
             所以下面用 ALIAS 做一次语义对齐。

用法（务必剥离代理）：
  cd <仓库根>
  env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \\
      no_proxy='*' NO_PROXY='*' \\
      ./astrbot-win/venv/Scripts/python.exe \\
      <AstrBot 目录>/data/plugins/astrbot_plugin_sticker_pack/tools/sync_qq_faces.py

  --dry-run   只打印报告，不落盘
  --config P  手动指定 face_config.json
"""

from __future__ import annotations

import argparse
import glob
import io
import json
import os
import sys

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG_PATH = os.path.join(PLUGIN_DIR, "face_catalog.json")
# PLUGIN_DIR = <AstrBot>/data/plugins/<plugin>，上两级就是 data/
MAP_PATH = os.path.join(
    os.path.dirname(os.path.dirname(PLUGIN_DIR)), "stickers", "map.txt"
)

# QQ 输入法的 emoji 联想名 -> 本地库里真能发的黄脸名。
# 对齐原则：先求同义，再求近义，实在没有就留空（留空的 emoji 不替换，
# 原样发出去——QQ 本来就支持裸 emoji，硬凑一个不搭的脸反而更假）。
# 左边是 face_config 里 QDes 去掉 "/" 的名字，右边必须是 map.txt 里的黄脸名。
ALIAS: dict[str, str] = {
    # ---- 情绪脸：emoji 联想名和黄脸正式名普遍对不上，逐个对齐 ----
    "嘿嘿": "憨笑",
    "羞涩": "害羞",
    "亲亲": "飞吻",
    "汗": "汗",
    "紧张": "发抖",
    "吐舌": "吐",
    "呲牙": "呲牙",
    "淘气": "坏笑",
    "可爱": "卖萌",
    "媚眼": "色",
    "花痴": "花痴",
    "失落": "难过",
    "高兴": "呲牙",
    "哼哼": "坏笑",
    "不屑": "白眼",
    "瞪眼": "惊讶",
    "飞吻": "飞吻",
    "大哭": "大哭",
    "害怕": "惊恐",
    "激动": "笑哭",
    "肌肉": "奋斗",
    "拳头": "拳头",
    "厉害": "赞",
    "鼓掌": "鼓掌",
    "胜利": "胜利",
    "鄙视": "鄙视",
    "合十": "祈祷",
    "好的": "OK",
    "眼睛": "让我康康",
    # ---- 吃的：熟食统一走「吃」，主食走「饭」 ----
    "米饭": "饭",
    "饭团": "饭",
    "意面": "吃",
    "拉面": "吃",
    "面包": "吃",
    "汉堡": "吃",
    "薯条": "吃",
    "寿司": "吃",
    "煎蛋": "吃",
    "蛋糕": "蛋糕",
    "啤酒": "啤酒",
    "干杯": "干杯",
    "咖啡": "咖啡",
    "西瓜": "西瓜",
    "药丸": "药",
    "打针": "药",
    # ---- 物品：挑语义最近的黄脸 ----
    "玫瑰": "玫瑰",
    "庆祝": "庆祝",
    "礼物": "礼物",
    "蝴蝶结": "礼物",
    "气球": "庆祝",
    "炸弹": "发怒",
    "皇冠": "得意",
    "口红": "得意",
    "戒指": "示爱",
    "星星": "好闪",
    "闪光": "好闪",
    "奖杯": "赞",
    "钱": "钞票",
    "睡觉": "睡",
    "闪电": "闪电",
    "便便": "便便",
    "水": "汗",
    "热": "热化了",
    "飞机": "飞机",
    "火箭": "牛啊",
    "话筒": "K歌",
    "吉他": "K歌",
    "圣诞树": "庆祝",
    "吹气": "嘘",
    "包": "红包",
    # ---- 动物：只映射有专属黄脸的 ----
    "猪": "猪头",
    "骷髅": "惊恐",
    "牛": "牛啊",
    "幽灵": "幽灵",
    "鱼": "摸锦鲤",
    "狗": "汪汪",
    "老虎": "赞",
    # ---- 自然 / 符号 ----
    "晴天": "太阳",
    "月亮": "月亮",
    "正确": "对号",
    "错误": "错号",
    "问号": "问号脸",
    "叹号": "惊讶",
    "手枪": "生气",
    "爱心": "爱心",
}

# 不进白名单的脸，分三类：
#   1) 暴力/血腥向，AI 发出去容易炸群
#   2) 冷门老脸：一年见不到一次，用了反而露馅（上一轮专门过滤的那批）
#   3) 需要动画才有味道的动作脸：普通 face 通道只出静态帧，看着像卡住
# 它们仍在 map.txt 里，AI 显式写名字时走 FACE_SUBSTITUTE 降级成近似常用脸。
DENY_FACE: tuple[str, ...] = (
    # 暴力
    "菜刀", "刀", "骷髅", "喷血", "爆筋", "折磨", "衰", "手枪", "炸弹", "咒骂",
    # 冷门
    "斜眼笑", "无眼笑", "魔鬼笑", "瓢虫", "羊驼", "河蟹", "蛋", "菊花",
    "托脸", "顶呱呱", "变形", "举牌牌", "仔细分析", "我最美", "拽炸天",
    # 主动发出去很尬的两个
    "勾引", "老色痞",
    # 动作脸（静态很怪）
    "拍头", "扯一扯", "舔一舔", "蹭一蹭", "掐一掐", "扇脸", "喷脸", "糊脸",
    "头撞击", "甩头", "扔狗", "啃头", "颤抖", "开枪", "暴击", "打脸", "回头",
    # 球类/游戏类：聊天里 AI 主动发一张篮球足球很莫名其妙
    "篮球", "足球", "乒乓", "跳绳", "跳跳", "街舞", "骰子", "包剪锤",
    # 龙舟系列：端午限定，且这几个名字是老版本遗留
    "中龙舟", "大龙舟", "新年中龙", "新年大龙",
)

# 节日/生肖脸：只在节日有意义，平时发出来很怪，单独标记（不进 COMMON_FACES）
SEASONAL_FACE: tuple[str, ...] = (
    "龙年快乐", "划龙舟", "虎虎生威", "大展宏兔", "福萝卜", "豹富", "牛气冲天",
    "左拜年", "右拜年", "红包多多", "求红包", "谢红包", "红包包", "新年烟花",
    "双喜", "鞭炮", "灯笼", "生日快乐", "喝彩", "烟花",
)


def find_face_config(hint: str = "") -> str:
    """找 QQ 的 face_config.json：先看传入的，再扫常见的几个安装位置。"""
    if hint and os.path.isfile(hint):
        return hint
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    fallback = os.path.join("qq", "nt_qq", "global", "nt_data", "Emoji", "emoji-resource", "face_config.json")
    patterns = [
        os.path.join(repo, "napcat-win", fallback),
        os.path.join(repo, "tools", "windows", "napcat-win", fallback),
        os.path.join(os.path.expanduser("~"), "dafeiyu", "napcat-win", fallback),
        r"C:\Users\*\dafeiyu\stack\napcat\qq\nt_qq\global\nt_data\Emoji\emoji-resource\face_config.json",
        r"C:\Users\*\dafeiyu\napcat-win\qq\nt_qq\global\nt_data\Emoji\emoji-resource\face_config.json",
        r"C:\Users\*\Documents\Tencent Files\*\nt_qq\global\nt_data\Emoji\emoji-resource\face_config.json",
        r"C:\Program Files\Tencent\QQNT\resources\app\nt_data\Emoji\emoji-resource\face_config.json",
        r"C:\Program Files (x86)\Tencent\QQNT\**\face_config.json",
    ]
    for pat in patterns:
        hits = glob.glob(pat, recursive=True)
        if hits:
            return hits[0]
    raise SystemExit("没找到 face_config.json，用 --config 指定路径")


def load_map(path: str) -> tuple[dict[str, str], set[str]]:
    """读 map.txt：返回 (黄脸名->id, 图片名集合)。"""
    faces: dict[str, str] = {}
    images: set[str] = set()
    if not os.path.isfile(path):
        return faces, images
    with io.open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(":")
            if len(parts) >= 4 and parts[2] == "face":
                faces[parts[0]] = parts[3]
            elif len(parts) >= 3:
                images.add(parts[0])
    return faces, images


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="", help="face_config.json 路径")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg_path = find_face_config(args.config)
    print(f"读取 {cfg_path}")
    with io.open(cfg_path, encoding="utf-8") as fh:
        cfg = json.load(fh)

    sysface = cfg.get("sysface") or []
    emoji = cfg.get("emoji") or []
    if not sysface or not emoji:
        raise SystemExit("face_config.json 结构不对：缺 sysface / emoji")

    cur_faces, cur_images = load_map(MAP_PATH)
    print(f"map.txt 现有：黄脸 {len(cur_faces)} 张，图片 {len(cur_images)} 张")

    # ---------- 1. sysface 归一 ----------
    # 判定「超级表情」只看 AniStickerPackName：
    #   · 经典黄脸（0~350）在新版 QQ 全部带 AniStickerType:1——那只是把静态脸
    #     做成了会动的版本，走 face 通道发出去完全正常，不能当超级表情剔掉
    #     （否则流泪/比心/庆祝/生气 这些最常用的脸全被误杀）。
    #   · 真正的超级表情一定挂在独立表情包下（364 超级赞 → AniStickerPackName=汪汪、
    #     狗狗笑哭、企鹅爱心…），这种只发得出静态首帧，单独归到 hidden。
    visible: list[dict] = []
    hidden: list[dict] = []
    for item in sysface:
        sid = str(item.get("QSid", "")).strip()
        name = str(item.get("QDes", "")).strip().lstrip("/")
        if not sid.isdigit() or not name:
            continue
        pack = str(item.get("AniStickerPackName") or "").strip()
        rec = {"id": int(sid), "name": name, "hidden": bool(pack), "pack": pack}
        (hidden if pack else visible).append(rec)
    visible.sort(key=lambda r: r["id"])
    hidden.sort(key=lambda r: r["id"])

    # ---------- 2. emoji -> 黄脸名 ----------
    # 目标名必须真实存在于 map.txt 的黄脸里，否则这张 emoji 就不替换
    emoji_map: dict[str, str] = {}
    unmapped: list[str] = []
    for item in emoji:
        ch = str(item.get("QSid", ""))
        des = str(item.get("QDes", "")).strip().lstrip("/")
        if not ch:
            continue
        target = ALIAS.get(des, "")
        if target and target not in cur_faces:
            # 别名指向的黄脸不在库里（比如被图片顶掉了名字），再退回同名匹配
            target = des if des in cur_faces else ""
        if not target:
            unmapped.append(f"{ch} {des}")
            continue
        emoji_map[ch] = target
        # 带变体选择符的写法也要认（❤ vs ❤️）
        if len(ch) == 1 and ord(ch) > 0x2000:
            emoji_map.setdefault(ch + "\ufe0f", target)

    # ---------- 3. 补 map.txt 里缺的可见黄脸 ----------
    add_lines: list[str] = []
    skipped_conflict: list[str] = []
    for rec in visible:
        name = rec["name"]
        if name in cur_faces:
            if cur_faces[name] != str(rec["id"]):
                print(f"  ! id 不一致 {name}: 库内 {cur_faces[name]} / 官方 {rec['id']}")
            continue
        if name in cur_images:
            skipped_conflict.append(name)
            continue
        add_lines.append(f"{name}:{name}:face:{rec['id']}")

    # ---------- 4. 白名单（进 COMMON_FACES 的可见脸） ----------
    # 口径是「map.txt 里所有黄脸」而不是「官方目录里的脸」：
    # 目录只有 282 条，不少老脸（震惊/啤酒/红包/完成/明白/加一）这个版本压根没收录，
    # 但它们的 id 在经典 face 体系里依然有效、也一直在用。map.txt 才是
    # 「客户端确认能发」的那份清单，目录只负责补全。
    will_have = sorted(set(cur_faces) | {line.split(":")[0] for line in add_lines})
    allow = [
        n for n in will_have if n not in DENY_FACE and n not in SEASONAL_FACE
    ]

    # ---------- 5. 落盘 ----------
    catalog = {
        "_source": cfg_path,
        "_note": "由 tools/sync_qq_faces.py 从 QQ face_config.json 生成，勿手改",
        "sysface_visible": visible,
        "sysface_hidden": hidden,
        "emoji": emoji_map,
        "allow": allow,
        "deny": list(DENY_FACE),
        "seasonal": list(SEASONAL_FACE),
    }

    print()
    print(f"sysface  可见 {len(visible)} 张 / 隐藏(超级表情) {len(hidden)} 张")
    print(f"emoji    官方 {len(emoji)} 条 -> 能映射 {len(emoji_map)} 条，未映射 {len(unmapped)} 条")
    print(f"白名单   {len(allow)} 张（禁 {len(DENY_FACE)} + 节日 {len(SEASONAL_FACE)} 已排除）")
    print(f"map.txt  需补 {len(add_lines)} 条，与图片同名跳过 {len(skipped_conflict)} 条"
          + (f"：{skipped_conflict}" if skipped_conflict else ""))
    for line in add_lines:
        print(f"  + {line}")
    if unmapped:
        print(f"未映射 emoji（原样保留，不发黄脸）：{' '.join(unmapped)}")

    if args.dry_run:
        print("\n--dry-run，未写文件")
        return 0

    with io.open(CATALOG_PATH, "w", encoding="utf-8") as fh:
        json.dump(catalog, fh, ensure_ascii=False, indent=1)
    print(f"\n已写 {CATALOG_PATH}")

    if add_lines:
        if not any(line.startswith("# 黄脸") for line in open(MAP_PATH, encoding="utf-8")):
            add_lines.insert(0, "")
            add_lines.insert(0, "# 黄脸补全（来自 QQ face_config.json，tools/sync_qq_faces.py）")
        with io.open(MAP_PATH, "a", encoding="utf-8") as fh:
            fh.write("\n".join(add_lines) + "\n")
        print(f"已追加 {len(add_lines)} 行到 {MAP_PATH}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
