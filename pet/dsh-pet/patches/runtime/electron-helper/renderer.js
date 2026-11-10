/**
 * 桌面 Helper 渲染端入口：配置加载、错误处理、启动装配与素材注入。
 * 依赖链（index.html 顺序加载）：shared-core.js → constants.js → sprite.js → events.js → 本文件。
 * 配置唯一来源是宿主 /config 的成品聚合（一步 fetch → S.flattenConfigPets 拍平），失败大声报错并每 5s
 * 重试，没有静默兜底。窗口 = 宠物包围盒 + 四周外扩余量，宠物移动由本页逐帧上报给主进程。
 * 不实现宠物行为与系统通知（见 sprite.js）。
 * @module dsh-pet/runtime/electron-helper/renderer
 */
'use strict';

// 配置（大声报错；失败 5s 重试）
function showError(message) {
  console.error('[dsh-pet] ' + message);
  window.__dshPetDebug.configOk = false;
  errorEl.textContent = 'dsh-pet 配置错误：' + message;
  errorEl.classList.add('visible');
}
function hideError() {
  errorEl.classList.remove('visible');
  errorEl.textContent = '';
}
function scheduleReboot() {
  if (bootTimer) return;
  bootTimer = setTimeout(() => {
    bootTimer = null;
    void boot();
  }, 5000);
}

/**
 * 连续启动失败次数（成功一次即清零）。首次失败多半是启动竞态：主进程不等 bridge 回调服务就绪
 * 就建窗，首帧 fetch 会打空；5s 重试就自愈，不该闪红条。持续失败才是真配置错误，照旧大声报。
 */
let bootFailures = 0;

async function loadConfig() {
  // 唯一配置入口：宿主 /config 的成品聚合（已合并并保证绝对正确），一步拉取 → 拍平，零校验零兜底
  const res = await fetch(BASE + '/config', { cache: 'no-store' });
  if (!res.ok) throw new Error(`config http ${res.status}`);
  const merged = await res.json();
  // 接口统一 200 + 实体：失败时状态码仍是 200，得看体里的 ok。
  if (merged && merged.ok === false) throw new Error('config ' + String(merged.reason ?? 'failed'));
  return {
    pets: S.flattenConfigPets(merged),
    // 拖拽抛掷物理参数（顶层全局，所有宠物共用；合并器已填内置默认）
    physics: (merged && merged.main && merged.main.physics) || S.DEFAULT_PHYSICS,
    // 抛掷锁定在当前屏幕（根字段；合并器已填内置默认 false）
    confineToScreen: (merged && merged.main && merged.main.confineToScreen) === true,
    // 字体（全局一份；合并器已填内置默认）：字体族在此注入全局变量，字号由每只宠物按自己的尺寸算
    font: (merged && merged.main && merged.main.font) || S.DEFAULT_FONT,
    // 语音：音色、总开关与音量（缺省 = 不播，与网页端同一口径）
    voiceName: (merged && merged.main && merged.main.voiceName) || '',
    voiceEnabled: (merged && merged.main && merged.main.voiceEnabled) === true,
    // 音量只在播放入口用，缺省由共享层按最大音量处理，这里不兜 1
    voiceVolume: merged && merged.main ? merged.main.voiceVolume : undefined,
    // 背景泡泡与音效（只读 main 条目；缺省视为开，与内置默认一致）
    bubbleBackdropEnabled: (merged && merged.main && merged.main.bubbleBackdropEnabled) !== false,
    bubbleRiseSfxEnabled: (merged && merged.main && merged.main.bubbleRiseSfxEnabled) !== false,
    // 背景泡泡数量（0~24，0 = 关闭）
    bubbleBackdropCount: (() => {
      const n = Number(merged && merged.main && merged.main.bubbleBackdropCount);
      return Number.isFinite(n) ? Math.min(24, Math.max(0, Math.round(n))) : 9;
    })(),
    // 背景泡泡调色板
    bubbleBackdropPalette: (() => {
      const v = String((merged && merged.main && merged.main.bubbleBackdropPalette) || '').toLowerCase();
      return ['candy', 'ocean', 'sunset', 'rainbow', 'mono'].includes(v) ? v : 'candy';
    })(),
    // 背景泡泡运动幅度
    bubbleBackdropMotion: (() => {
      const v = String((merged && merged.main && merged.main.bubbleBackdropMotion) || '').toLowerCase();
      return ['calm', 'normal', 'lively'].includes(v) ? v : 'normal';
    })(),
    // 主气泡背景板 / 壁纸（只读 main 条目；默认关闭 = 与旧版观感完全一致）
    // 预设/类型/文字明暗做白名单收敛；颜色与图片地址只截长度，非法 CSS 值由浏览器自行丢弃，不影响布局。
    bubbleBoardEnabled: (merged && merged.main && merged.main.bubbleBoardEnabled) === true,
    bubbleBoardPreset: (() => {
      const v = String((merged && merged.main && merged.main.bubbleBoardPreset) || '').toLowerCase();
      return ['glass', 'paper', 'night', 'custom'].includes(v) ? v : 'glass';
    })(),
    bubbleBoardMode: (() => {
      const v = String((merged && merged.main && merged.main.bubbleBoardMode) || '').toLowerCase();
      return ['solid', 'gradient', 'image'].includes(v) ? v : 'gradient';
    })(),
    bubbleBoardColor: String((merged && merged.main && merged.main.bubbleBoardColor) || '#ffffff').slice(0, 64),
    bubbleBoardGradientFrom: String((merged && merged.main && merged.main.bubbleBoardGradientFrom) || '#ffffff').slice(0, 64),
    bubbleBoardGradientTo: String((merged && merged.main && merged.main.bubbleBoardGradientTo) || '#dbe7ff').slice(0, 64),
    bubbleBoardImage: String((merged && merged.main && merged.main.bubbleBoardImage) || '').slice(0, 2048),
    bubbleBoardScrim: (() => {
      const n = Number(merged && merged.main && merged.main.bubbleBoardScrim);
      return Number.isFinite(n) ? Math.min(0.8, Math.max(0, n)) : 0.32;
    })(),
    bubbleBoardText: (() => {
      const v = String((merged && merged.main && merged.main.bubbleBoardText) || '').toLowerCase();
      return ['auto', 'light', 'dark'].includes(v) ? v : 'auto';
    })(),
    // 动画切换间隔与睡觉开关（两者都挂在 animations 下）
    animations: {
      switchSec: (() => {
        const n = Number(merged && merged.main && merged.main.animations && merged.main.animations.switchSec);
        return Number.isFinite(n) && n > 0 ? Math.min(120, Math.max(2, n)) : 12;
      })(),
      sleepEnabled: !(merged && merged.main && merged.main.animations && merged.main.animations.sleepEnabled === false),
    },
    // 动画播放速度（animations.speed，0.25~3）：轮询到新值后由 sprite.applyAnimSpeed 就地改 rate
    animationSpeed: (() => {
      const n = Number(merged && merged.main && merged.main.animations && merged.main.animations.speed);
      return Number.isFinite(n) && n > 0 ? Math.min(3, Math.max(0.25, n)) : 1;
    })(),
    // OCR 定期识屏（看屏点评）：开=宿主按 visionWatchSec 定期识屏并弹气泡反馈
    visionWatchEnabled: (merged && merged.main && merged.main.visionWatchEnabled) === true,
    visionWatchSec: Math.max(60, Number(merged && merged.main && merged.main.visionWatchSec) || 900),
  };
}

/**
 * 配置热更新：每 30s 重拉一次 /config，把语音开关/音量等“网页管控的参数”同步到本窗口。
 * 声线本身由宿主每次合成就地读 profiles/.active，切换后下一句话即生效；这里只同步会随配置变化的开关。
 */
let configPollTimer = null;
function startConfigPolling() {
  if (configPollTimer) return;
  configPollTimer = setInterval(async () => {
    try {
      const cfg = await loadConfig();
      config = cfg;
      S.setVoiceConfig(cfg.voiceName, cfg.voiceEnabled, cfg.voiceVolume);
      syncBubbleBackdrop();
      // 背景板/壁纸热更新：只换类与 CSS 变量，不重建气泡内容（避免打断正在看的那句话）
      for (const s of sprites) s.syncBubbleBoard(s.bubbleSlot);
      // 动画速度热更新：当前正在播的两路视频立刻改 rate，不用等下一次切动画
      for (const s of sprites) s.applyAnimSpeed();
      // 睡觉开关热更新：改成 false 时把正在睡的宠物就地叫醒，且不再走睡觉/起床动画
      for (const s of sprites) s.applySleepSetting();
    } catch (e) {
      console.warn('[dsh-pet] 配置轮询失败：' + (e && e.message ? e.message : String(e)));
    }
  }, 30000);
}

// 启动（配置校验通过才建 sprite；失败大声报错 + 5s 自动重试）
async function boot() {
  try {
    const cfg = await loadConfig();
    config = cfg;
    // 语音：本窗口固定是桌面端；音色、总开关与音量直接取成品配置，不走懒加载——
    // 懒加载要等第一次播放才去拉 /config，撞上 bridge 起步竞态就永远读不到开关（读失败只试一次）。
    S.setVoiceBase(BASE);
    S.setSurface('desktop');
    S.setVoiceConfig(cfg.voiceName, cfg.voiceEnabled, cfg.voiceVolume);
    // 背景泡泡按配置挂载（关掉时不注入）
    syncBubbleBackdrop();
    bootFailures = 0;
    hideError();
    // 字体族进 documentElement：对话窗/得分弹窗挂在 body 下、继承不到宠物根容器，只能走全局变量。
    // 必须等 /config 拉回来之后才设（字体族来自配置）。
    document.documentElement.style.setProperty('--dsh-pet-font-family', S.fontStackOf(cfg.font.family));
    const pets = cfg.pets.filter((p) => S.isDesktopVisible(p.display));
    if (pets.length === 0) {
      // 全部宠物只在网页显示是合法状态，不是配置错误：没有桌面宠物就表示本窗口没活儿干，
      // 安静关掉、不弹条、不重试。托盘是否还在看 main 条目的 trayEnabled，与这里无关。
      // 宿主流由 syncDesktop() 在配置变化时重建 Helper；手动流需自行重启。
      hideError();
      console.info('[dsh-pet] no desktop pets; closing this helper window (tray kept only if trayEnabled)');
      window.close();
      return;
    }
    // 本窗口只承载一只宠物：petIndex 由主进程按 DSH_PET_PETS 顺序注入
    const pet = pets[CONFIG.petIndex];
    if (!pet) {
      showError('petIndex=' + CONFIG.petIndex + ' 超出桌面宠物列表（共 ' + pets.length + ' 只），本窗口不创建宠物');
      scheduleReboot();
      return;
    }
    for (const s of sprites) s.dispose();
    sprites = [new PetSprite(pet)];
    window.__dshPetDebug.configOk = true;
    window.__dshPetDebug.spriteCount = sprites.length;
    for (const s of sprites) s.playIdle();
    // 声线目录预热：右键菜单的“声线”分支需要同步列表，先在后台拉一次（菜单打开时不阻塞）。
    for (const s of sprites) s.ensureVoiceCatalog();
    // 常驻余额：启动就拉一次并保持气泡（碎碎念会临时顶替，说完自动回来）；balanceSticky:false 可关
    if (pet.balanceEnabled && pet.balanceSticky !== false) {
      for (const s of sprites) s.showBalanceFromMenu();
    }
    startLoops();
    startConfigPolling();
  } catch (e) {
    const msg = '配置加载失败：' + (e && e.message ? String(e.message) : String(e));
    // 首次失败先静默（多半是启动竞态，见 bootFailures 的说明），下一次 5s 重试仍失败才报。
    bootFailures += 1;
    if (bootFailures > 1) showError(msg);
    else console.error('[dsh-pet] ' + msg + ' (first boot, retrying)');
    scheduleReboot();
  }
}

// 注入打字资源：气泡字体 + 点击/拖拽光标图标（与浏览器同一套素材，host 经 /dsh-pet-7340/ 提供）
function injectAssets() {
  const style = document.createElement('style');
  style.textContent =
    '@font-face{font-family:"ShangshouSoftCandy";src:url("' +
    BASE +
    '/font/' +
    encodeURIComponent('上首软糖体') +
    '.ttf") format("truetype");font-display:swap;font-weight:400}' +
    '.pet-hit{cursor:url("' +
    BASE +
    '/pic/cursor-grab.png") 16 16, grab}' +
    '.pet-hit.dragging{cursor:url("' +
    BASE +
    '/pic/cursor-grabbing.png") 16 16, grabbing}';
  document.head.appendChild(style);
  // 统一右键菜单样式（与浏览器注入同一份 MENU_CSS）
  const menuStyle = document.createElement('style');
  menuStyle.textContent = S.MENU_CSS;
  document.head.appendChild(menuStyle);
  // 悬浮提示样式（与浏览器注入同一份 TOOLTIP_CSS，取代命中层上的原生 title 提示）
  const tipStyle = document.createElement('style');
  tipStyle.textContent = S.TOOLTIP_CSS;
  document.head.appendChild(tipStyle);
}

// 背景泡泡音效：WebAudio 实时合成，不依赖音频文件。每颗泡泡每上浮一轮就有机会“啵”一声，
// 用最小间隔压住密度，避免多颗泡泡各响一次变成噪音。音量跟随语音音量，但不受语音总开关约束。
const BUBBLE_SFX_MIN_GAP_MS = 2500;
const BUBBLE_SFX_BASE_GAIN = 0.09;
let bubbleAudioCtx = null;
let lastBubbleSfxAt = 0;

// 背景泡泡命中范围：外扩 22px，并保证最小命中直径 32px（小泡泡按包围盒与外扩取 max）。
// 这是唯一来源：写进 CSS 变量后，index.html 的 ::before 命中圈与 sprite/input.js 的登记矩形都读它，
// 两处数值不会漂移（input.js 的兜底常量与这里保持一致）。
const BUBBLE_HIT_PAD = 22;
const BUBBLE_HIT_MIN_DIAMETER = 32;
document.documentElement.style.setProperty('--hit-pad', BUBBLE_HIT_PAD + 'px');
document.documentElement.style.setProperty('--hit-min', BUBBLE_HIT_MIN_DIAMETER + 'px');

// 两个开关都只读成品配置 main；缺省（含配置还没拉回来时）一律视为开。
function bubbleBackdropOn() {
  return !config || config.bubbleBackdropEnabled !== false;
}
function bubbleRiseSfxOn() {
  return !config || config.bubbleRiseSfxEnabled !== false;
}

function bubbleSfxGain() {
  const volume =
    config && typeof config.voiceVolume === 'number'
      ? Math.min(1, Math.max(0, config.voiceVolume))
      : 1;
  return BUBBLE_SFX_BASE_GAIN * volume;
}

/** 上浮到位的水声（轻声“啵”）；force=true 用于用户戳破，跳过最小间隔并换成下沉的爆裂音。 */
function playBubblePop(force) {
  if (!bubbleRiseSfxOn()) return;
  const now = Date.now();
  if (!force && now - lastBubbleSfxAt < BUBBLE_SFX_MIN_GAP_MS) return;
  lastBubbleSfxAt = now;
  try {
    const Ctor = window.AudioContext || window.webkitAudioContext;
    if (!Ctor) return;
    if (!bubbleAudioCtx) bubbleAudioCtx = new Ctor();
    const ctx = bubbleAudioCtx;
    if (ctx.state === 'suspended') {
      const p = ctx.resume();
      if (p && typeof p.catch === 'function') p.catch(() => {});
    }
    const t = ctx.currentTime;
    const burst = force === true;
    const base = burst ? 300 + Math.random() * 90 : 480 + Math.random() * 420;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'sine';
    osc.frequency.setValueAtTime(base, t);
    // 上浮：轻轻往上扬；戳破：短促往下掉，像水泡裂开
    osc.frequency.exponentialRampToValueAtTime(burst ? base * 0.35 : base * 1.9, t + (burst ? 0.07 : 0.08));
    gain.gain.setValueAtTime(0, t);
    gain.gain.linearRampToValueAtTime(bubbleSfxGain() * (burst ? 1.4 : 1), t + 0.012);
    gain.gain.exponentialRampToValueAtTime(0.0001, t + (burst ? 0.16 : 0.18));
    osc.connect(gain).connect(ctx.destination);
    osc.start(t);
    osc.stop(t + 0.2);
  } catch {
    /* 音频不可用：静默忽略 */
  }
}

/** 戳破爆炸特效时长：粒子散开的时间 */
const BUBBLE_BURST_FX_MS = 260;

/** 在泡泡中心炸开：一圈冲击环 + 若干向外喷散的小水珠（点击戳破时有爆裂音，见 burstBubble） */
function spawnBurstFx(parent, cx, cy, size) {
  const fx = document.createElement('div');
  fx.className = 'pet-bubble-burst';
  fx.style.left = cx + 'px';
  fx.style.top = cy + 'px';
  fx.style.setProperty('--r', Math.max(6, Math.round(size / 2)) + 'px');

  const ring = document.createElement('i');
  ring.className = 'pet-bubble-burst-ring';
  fx.appendChild(ring);

  const count = 7;
  for (let i = 0; i < count; i += 1) {
    const drop = document.createElement('i');
    drop.className = 'pet-bubble-burst-drop';
    const angle = (Math.PI * 2 * i) / count + Math.random() * 0.6;
    const dist = size * (0.55 + Math.random() * 0.6);
    const scale = 0.7 + Math.random() * 0.7;
    drop.style.setProperty('--tx', (Math.cos(angle) * dist).toFixed(1) + 'px');
    drop.style.setProperty('--ty', (Math.sin(angle) * dist).toFixed(1) + 'px');
    drop.style.width = (3 * scale).toFixed(1) + 'px';
    drop.style.height = (3 * scale).toFixed(1) + 'px';
    fx.appendChild(drop);
  }
  parent.appendChild(fx);
  window.setTimeout(() => fx.remove(), BUBBLE_BURST_FX_MS + 60);
}

// 泡泡调色板：色相组 + 饱和度/亮度基调。每颗泡泡随机取色，色值以半透明 hsla 写进 CSS 变量，
// 背景仍然透得出来；candy 故意用 10 个明显不同的色相，比早期的单一淡蓝丰富得多。
const BUBBLE_PALETTE_HUES = {
  candy: [335, 190, 265, 45, 150, 215, 15, 285, 95, 165],
  ocean: [190, 200, 210, 220, 230, 240, 175, 255],
  sunset: [12, 28, 42, 340, 325, 300, 280, 355],
  mono: [210, 220, 230, 200, 240, 215, 225, 205],
};
const BUBBLE_PALETTE_TONES = {
  candy: { s: 92, l: 80 },
  ocean: { s: 86, l: 72 },
  sunset: { s: 94, l: 70 },
  rainbow: { s: 90, l: 78 },
  mono: { s: 12, l: 88 },
};
// 上浮时长 / 漂移幅度 / 摇摆周期三档：calm 更稳，lively 更飘。
const BUBBLE_MOTION = {
  calm: { rise: [9, 14], drift: [8, 22], sway: [5.5, 8] },
  normal: { rise: [7, 13], drift: [14, 36], sway: [3.8, 6] },
  lively: { rise: [5.5, 10], drift: [26, 62], sway: [2.6, 4.2] },
};

function bubblePaletteName() {
  return (config && config.bubbleBackdropPalette) || 'candy';
}

function bubbleMotionProfile() {
  return BUBBLE_MOTION[(config && config.bubbleBackdropMotion) || 'normal'] || BUBBLE_MOTION.normal;
}

function bubbleCountTarget() {
  const n = Number(config && config.bubbleBackdropCount);
  return Number.isFinite(n) ? Math.min(24, Math.max(0, Math.round(n))) : 9;
}

/** 从当前调色板随机取一组同色系的半透明色写进 CSS 变量（描边/折射环/弧形反光/高光/内阴影/外发光）。 */
function applyBubbleColor(dot) {
  const palette = bubblePaletteName();
  const names = Object.keys(BUBBLE_PALETTE_HUES);
  const key = names.includes(palette) ? palette : 'candy';
  const hues = BUBBLE_PALETTE_HUES[key];
  const tone = BUBBLE_PALETTE_TONES[key];
  const baseHue = key === 'rainbow' ? Math.random() * 360 : hues[Math.floor(Math.random() * hues.length)];
  // 色相再抖 ±7°、饱和度 ±4、亮度 ±5：同一色号也不像复制粘贴；后续所有色值都在这个色系里取
  const hue = Math.round((((baseHue + (Math.random() * 14 - 7)) % 360) + 360) % 360);
  const sat = Math.min(100, Math.max(4, tone.s + (Math.random() * 8 - 4)));
  const light = Math.min(96, Math.max(58, tone.l + (Math.random() * 10 - 5)));
  const alpha = (min, span) => (min + Math.random() * span).toFixed(3);
  dot.style.setProperty('--bub-core', 'hsla(' + hue + ', ' + sat + '%, ' + Math.round(light + 2) + '%, ' + alpha(0.1, 0.07) + ')');
  dot.style.setProperty('--bub-edge', 'hsla(' + hue + ', ' + sat + '%, ' + Math.round(light) + '%, ' + alpha(0.04, 0.04) + ')');
  dot.style.setProperty('--bub-ring', 'hsla(' + hue + ', ' + Math.min(100, sat + 4) + '%, ' + Math.min(98, light + 20) + '%, ' + alpha(0.6, 0.18) + ')');
  dot.style.setProperty('--bub-border', 'hsla(' + hue + ', ' + Math.min(100, sat + 8) + '%, ' + Math.min(98, light + 14) + '%, ' + alpha(0.68, 0.2) + ')');
  dot.style.setProperty('--bub-arc', 'hsla(' + hue + ', ' + Math.min(100, sat + 2) + '%, ' + Math.min(99, light + 24) + '%, ' + alpha(0.36, 0.18) + ')');
  dot.style.setProperty('--bub-hi', 'hsla(' + hue + ', ' + Math.min(100, sat + 2) + '%, ' + Math.min(99, light + 18) + '%, ' + alpha(0.82, 0.18) + ')');
  dot.style.setProperty('--bub-inner', 'hsla(' + hue + ', ' + Math.min(100, sat + 6) + '%, ' + Math.min(98, light + 12) + '%, ' + alpha(0.2, 0.1) + ')');
  dot.style.setProperty('--bub-glow', 'hsla(' + hue + ', ' + sat + '%, ' + Math.round(light) + '%, ' + alpha(0.28, 0.12) + ')');
}

/** 随机化上浮时长、漂移幅度、摇摆周期与相位（负延迟让窗口打开时已经在不同阶段）。 */
function applyBubbleMotion(dot) {
  const profile = bubbleMotionProfile();
  const rand = (range) => range[0] + Math.random() * (range[1] - range[0]);
  const rise = rand(profile.rise);
  const sway = rand(profile.sway);
  const drift = rand(profile.drift) * (Math.random() < 0.5 ? -1 : 1);
  dot.style.setProperty('--rise-dur', rise.toFixed(1) + 's');
  dot.style.setProperty('--sway-dur', sway.toFixed(1) + 's');
  dot.style.setProperty('--drift', drift.toFixed(1) + 'px');
  dot.style.setProperty('--rise-delay', (-Math.random() * rise).toFixed(1) + 's');
  dot.style.setProperty('--sway-delay', (-Math.random() * sway).toFixed(1) + 's');
}

// 背景泡泡：桌宠身后缓缓上浮的水泡。纯 CSS 动画（见 index.html 的 petBubbleRise + petBubbleSway）。
function createBubble() {
  const dot = document.createElement('i');
  dot.className = 'pet-bubble-dot';
  const size = 18 + Math.round(Math.random() * 26);
  dot.style.width = size + 'px';
  dot.style.height = size + 'px';
  dot.style.left = (4 + Math.random() * 92).toFixed(1) + '%';
  applyBubbleColor(dot);
  applyBubbleMotion(dot);
  // 上浮那条只走一遍：到顶就销毁并在随机延迟后从底部补一颗，不再整批按同一周期回到起点；
  // 横向摇摆仍是循环，所以不监听它的结束。
  dot.addEventListener('animationend', (event) => {
    if (event.animationName !== 'petBubbleRise') return;
    if (dot.dataset.bursting === '1') return;
    playBubblePop();
    recycleBubble(dot);
  });
  dot.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    burstBubble(dot);
  });
  return dot;
}

/** 上浮到顶的泡泡：销毁，隔一小段随机时间再从底部补一颗（延迟随机，避免整批同步“重生”）。 */
function recycleBubble(dot) {
  const parent = dot.parentElement;
  dot.remove();
  if (!parent || !parent.isConnected) return;
  window.setTimeout(() => {
    if (!bubbleBackdropOn()) return;
    if (!parent.isConnected) return;
    if (parent.querySelectorAll('.pet-bubble-dot:not([data-bursting="1"])').length >= bubbleCountTarget()) return;
    const next = createBubble();
    next.style.top = '100%';
    next.style.setProperty('--rise-delay', '0s');
    next.style.setProperty('--sway-delay', '0s');
    parent.appendChild(next);
  }, 240 + Math.random() * 1800);
}

/** 戳破一颗泡泡：有爆裂音 + 视觉爆炸并淡出，随后在底部补一颗（数量仍以 bubbleBackdropCount 为准）。 */
function burstBubble(dot) {
  if (dot.dataset.bursting === '1') return;
  const parent = dot.parentElement;
  if (!parent) return;
  dot.dataset.bursting = '1';
  const box = parent.getBoundingClientRect();
  const rect = dot.getBoundingClientRect();
  const size = Math.round(rect.width);
  const cx = rect.left - box.left + size / 2;
  const cy = rect.top - box.top + size / 2;
  const opacity = Number(getComputedStyle(dot).opacity) || 0.85;
  // 停掉上浮动画并按当前位置钉住：爆炸点对准这一帧的泡泡
  dot.style.animation = 'none';
  dot.style.top = rect.top - box.top + 'px';
  dot.style.left = rect.left - box.left + 'px';
  dot.style.opacity = String(opacity);
  dot.style.transition = 'transform .18s ease-out, opacity .18s ease-out';
  dot.style.transform = 'scale(1)';
  spawnBurstFx(parent, cx, cy, size);
  playBubblePop(true);
  window.requestAnimationFrame(() => {
    dot.style.transform = 'scale(1.8)';
    dot.style.opacity = '0';
  });
  window.setTimeout(() => {
    dot.remove();
    if (!bubbleBackdropOn()) return;
    if (!parent.isConnected) return;
    if (parent.querySelectorAll('.pet-bubble-dot').length >= bubbleCountTarget()) return;
    const next = createBubble();
    next.style.top = '100%';
    next.style.setProperty('--rise-delay', '0s');
    next.style.setProperty('--sway-delay', '0s');
    parent.appendChild(next);
  }, 200);
}

// 背景泡泡在飘，命中矩形得跟着走：定时把最新的泡泡矩形重新上报给主进程。
// 150ms 一次够用（泡泡约 60~100px/s，误差几个像素，命中区另外外扩了 22px）。
const BUBBLE_REGION_SYNC_MS = 150;
let bubbleRegionTimer = null;
function startBubbleRegionSync() {
  if (bubbleRegionTimer) return;
  bubbleRegionTimer = window.setInterval(() => {
    if (!document.getElementById('pet-bubbles')) return;
    for (const s of sprites) s.syncInputRegions();
  }, BUBBLE_REGION_SYNC_MS);
}
function stopBubbleRegionSync() {
  if (!bubbleRegionTimer) return;
  window.clearInterval(bubbleRegionTimer);
  bubbleRegionTimer = null;
}

let bubbleStyleKey = '';

/** 一根水草：高度/宽度/色相/摇摆周期/相位/振幅全部逐根随机（纯 CSS 动画，见 index.html）。 */
function createSeaweedBlade() {
  const blade = document.createElement('i');
  blade.className = 'pet-seaweed-blade';
  blade.style.left = (4 + Math.random() * 92).toFixed(1) + '%';
  blade.style.width = (14 + Math.random() * 16).toFixed(1) + 'px';
  blade.style.height = (120 + Math.random() * 150).toFixed(0) + 'px';
  blade.style.setProperty('--sw-h', String(145 + Math.round(Math.random() * 40)));
  blade.style.setProperty('--sway-dur', (4 + Math.random() * 5).toFixed(1) + 's');
  blade.style.setProperty('--sway-delay', (-Math.random() * 9).toFixed(1) + 's');
  blade.style.setProperty('--sway-amp', (4 + Math.random() * 7).toFixed(1) + 'deg');
  blade.style.setProperty('--sway-skew', (Math.random() * 4).toFixed(1) + 'deg');
  return blade;
}

/** 背景海草层：挂在泡泡盒里（泡泡盒是唯一稳定绘制的背景层），作为首个子节点垫在泡泡下面。 */
function syncSeaweed(box) {
  if (box.querySelector('#pet-seaweed')) return; // 已挂载就不重建，避免 30s 轮询把摇摆相位重置
  const layer = document.createElement('div');
  layer.id = 'pet-seaweed';
  const count = 7 + Math.floor(Math.random() * 4); // 7~10 根
  for (let i = 0; i < count; i += 1) layer.appendChild(createSeaweedBlade());
  box.insertBefore(layer, box.firstChild);
}

/** 让背景泡泡与配置一致：数量增减到位（0 = 关闭），调色板/运动幅度变化时就地重绘。海草只跟总开关。 */
function syncBubbleBackdrop() {
  const root = document.getElementById('root');
  if (!root) return;
  const enabled = bubbleBackdropOn();
  let box = document.getElementById('pet-bubbles');
  if (!enabled) {
    if (box) box.remove();
    stopBubbleRegionSync();
    // 泡泡删掉后立刻重报一次命中区，把它们的矩形注销掉
    for (const s of sprites) s.syncInputRegions();
    return;
  }
  if (!box) {
    box = document.createElement('div');
    box.id = 'pet-bubbles';
    root.insertBefore(box, root.firstChild);
    bubbleStyleKey = '';
  }
  // 海草垫在泡泡盒最底层；数量为 0 时盒子也保留，海草仍可见
  syncSeaweed(box);
  const target = bubbleCountTarget();
  const styleKey = bubblePaletteName() + '|' + ((config && config.bubbleBackdropMotion) || 'normal');
  const dots = Array.from(box.querySelectorAll('.pet-bubble-dot:not([data-bursting="1"])'));
  if (styleKey !== bubbleStyleKey) {
    bubbleStyleKey = styleKey;
    for (const dot of dots) {
      applyBubbleColor(dot);
      applyBubbleMotion(dot);
    }
  }
  if (dots.length < target) {
    for (let i = dots.length; i < target; i += 1) box.appendChild(createBubble());
  } else if (dots.length > target) {
    for (let i = dots.length - 1; i >= target; i -= 1) dots[i].remove();
  }
  startBubbleRegionSync();
}

// 显示器几何变化（改分辨率/缩放、插拔屏、旋转）：主进程重算后推来，渲染端就地重挂视口与边界。
if (window.petBridge && window.petBridge.onDisplays) {
  window.petBridge.onDisplays((geo) => {
    // 变化前的视口原点必须在 applyDeskGeometry 之前取：relayout 靠它把旧视口相对坐标还原成
    // 世界坐标，否则合盖/开盖时视口原点一平移，宠物就会被误判越界。
    const prevView = { x: VIEW.x, y: VIEW.y, w: VIEW.w, h: VIEW.h };
    if (!applyDeskGeometry(geo)) return;
    for (const s of sprites) s.relayout(prevView);
  });
}

// 窗口内容区尺寸异常时按当前位置重新规整。
// 守卫：拖拽/飞行/漫游中绝不重设位置——这三种状态下位置由输入或物理驱动，而 position() 会把
// 宠物拉回 customPos。跨屏时 Windows 会因 WM_DPICHANGED 主动改窗口尺寸，不设防就会看到宠物跳回上一个落点。
window.addEventListener('resize', () => {
  for (const s of sprites) {
    if (s.dragState.active || s.throwRef !== null || s.moveRef !== null) continue;
    s.position();
  }
});

injectAssets();
// 泡泡要等配置拉回来才知道开不开：boot() 里 loadConfig 之后就位（这里先不同步）
void boot();
