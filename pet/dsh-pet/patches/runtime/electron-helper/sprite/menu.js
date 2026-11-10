/**
 * 右键菜单：打开/关闭、叶子动作派发，以及菜单里用到的自绘节点渲染。
 *
 * PetSprite 由 sprite.js 定义，本文件只往原型上挂方法，必须在其后加载。
 * 菜单结构与文案来自 shared-core（两端同一份）；这里只决定点下去之后做什么。
 * @module dsh-pet/runtime/electron-helper/sprite-menu
 */
'use strict';

// 右键菜单：精简成四类——余额开关 / 声线（含音量）/ 互动（对话 + OCR 识屏）/ 打开 WebUI。
// 打开 WebUI 固定走救赎中枢（hub），不再用宿主形态推导的「打开网站/打开软件」。
const HUB_BASE = 'http://127.0.0.1:23408';

/** 声线快捷项：只保留常用的两条，其余声线仍在网页“声线管理”里。 */
const VOICE_SHORTCUTS = ['姬子', '小忆'];
/** 音量档位（百分比）：点一下写 voiceVolume。 */
const VOLUME_STEPS = [20, 40, 60, 80, 100];

/** 睡觉拒绝台词的气泡时长（ms）：比默认 10s 略短，它只是「被打扰了」的一句回绝。 */
const SLEEP_REFUSE_BUBBLE_MS = 8000;

Object.assign(PetSprite.prototype, {
  onContextMenu(e) {
    const d = this.dragState;
    if (d.active || d.dragging || this.justDragged || this.menuOpen) return;
    e.preventDefault();
    this.stopThrow(); // 菜单弹出前停住飞行中的宠物
    this.stopMove(); // 菜单悬停期间宠物不漫游
    // 精简菜单：余额开关 / 声线 / 互动 / 打开 WebUI。动作树、碎碎念、配图、待办、睡觉等
    // 日常入口全部收掉——需要时仍可用快捷键和网页控制台。
    this.ensureVoiceCatalog();
    const tools = [
      // 一个开关项：开着叫「关闭余额」，关着叫「显示余额」，点一下立即写中枢并给出气泡答复。
      { label: this.pet.balanceEnabled === true ? '关闭余额' : '显示余额', action: 'toggle-balance' },
      this.buildVoiceBranch(),
      this.buildBubbleBranch(),
      {
        label: '互动',
        children: [
          { label: '对话', action: 'chat', hint: S.CHAT_HOTKEY_HINT },
          // 睡觉/起床切换项：睡后随机链整个停住（不掷骰、不游走），点宠物或点「起床」才醒。
          // 每次右键重建菜单，this.sleeping 取当下状态，标签随之切换。
          this.sleeping ? { label: '起床', action: 'wake' } : { label: '睡觉', action: 'sleep' },
        ],
      },
      // OCR 定期识屏：开关控制宿主定期识屏点评（弹气泡+语音），另有手动识屏/点评
      this.buildOcrBranch(),
      // 桌宠自己的开关：DSH 宿主不提供 quit 能力，右键菜单必须留一个就地关闭入口。
      // 关闭桌面窗 = 保留托盘；彻底退出 = 连托盘一起关，宿主不会再拉起进程。
      {
        label: '桌宠开关',
        children: [
          { label: '关闭桌面窗（保留托盘）', action: 'set-display', display: 'web' },
          { label: '彻底退出（含托盘，可再开）', action: 'quit-pet' },
          { label: '重新载入', action: 'reload' },
        ],
      },
      // 固定打开救赎中枢 WebUI（23408），不受宿主形态影响
      { label: '打开 WebUI（救赎中枢）', action: 'open-hub' },
    ];
    const tree = tools;
    if (!tree.length) return;
    this.menuOpen = true;
    this.setInteractive(true); // 菜单是窗口内 DOM：悬停期间整窗保持可交互，关闭后恢复命中区穿透
    this.syncInputBusy();
    window.__dshPetDebug.menuOpen = true;
    const m = S.mountContextMenu({
      tree,
      x: e.clientX,
      y: e.clientY,
      // 只允许在「窗口 ∩ 工作区」内显示：宠物贴边时外扩余量伸出屏幕，菜单走进那里会被 OS 裁掉
      clamp: this.visibleClampRect(),
      onAction: (leaf) => this.onMenuAction(leaf),
      // 菜单被点外/Esc 关闭（非菜单项路径）：同样复位可交互标记，恢复命中区判定
      onClose: () => {
        this.menuOpen = false;
        window.__dshPetDebug.menuOpen = false;
        this.syncInputBusy();
        if (!this.inputBusy()) this.setInteractive(false);
      },
    });
    this.menuClose = m.close;
    this.syncInputRegions(); // mountContextMenu 完成布局后才能读取菜单矩形
  },

  /** keepFacing = 调用方已显式指定朝向：跳过下面「文字类动画点播前强制朝左」的镜像修正，显式值优先。 */
  onMenuAction(leaf, keepFacing, fromHost) {
    this.closeMenu();
    if (!leaf || typeof leaf !== 'object') return;
    // 睡觉：打扰类动作在点击这一刻就拒绝——弹一句台词，什么都不执行。持锁时让路（锁=控制权交给第三方）。
    // 对话弹窗、待办面板、点播动画都是渲染端行为，宿主闸门拦不到；两端用同一份放行判定。
    if (this.sleeping && !this.frozen && !fromHost && !S.isSleepAllowedAction(leaf.action)) {
      const pool = Array.isArray(this.pet.sleepRefuseTexts) ? this.pet.sleepRefuseTexts : [];
      const text = pool[Math.floor(Math.random() * pool.length)];
      if (text !== undefined) this.showWhisper(text, undefined, undefined, SLEEP_REFUSE_BUBBLE_MS, false);
      return;
    }
    if (leaf.action === 'third-party') {
      // 第三方菜单项：只把 (owner, label) 交给宿主，具体做什么由注册方自己查 /menu/events。
      // 带上宠物 id——同一条菜单项在不同宠物上被点，注册方要能分辨是哪只。
      if (leaf.owner) {
        S.postAction(BASE + '/menu/click', { owner: leaf.owner, label: leaf.label, pet: this.pet.id }).catch(() => {
          /* 上报失败不影响本地：注册方下次轮询自然拿不到这次触发 */
        });
      }
      return;
    }
    if (leaf.action === 'open-site') {
      if (window.petBridge) window.petBridge.openDshSite(ORIGIN); // 系统默认浏览器打开（等效 Ctrl+点击链接）
      return;
    }
    if (leaf.action === 'open-hub') {
      this.openHub();
      return;
    }
    if (leaf.action === 'disable-voice') {
      this.setVoiceEnabled(false);
      return;
    }
    if (leaf.action === 'set-volume') {
      this.setVoiceVolume(leaf.volume);
      return;
    }
    if (leaf.action === 'ocr') {
      void this.lookScreenFromMenu();
      return;
    }
    if (leaf.action === 'narrate') {
      void this.narrateScreenFromMenu();
      return;
    }
    if (leaf.action === 'show-balance') {
      this.showBalanceFromMenu(); // 立即拉余额并展示（无需等 1s 触发轮询，展示路径与周期触发一致）
      return;
    }
    if (leaf.action === 'set-voice') {
      this.switchVoice(leaf.voice);
      return;
    }
    if (leaf.action === 'play-voice') {
      this.playVoicePreview(leaf.voice);
      return;
    }
    if (leaf.action === 'toggle-balance') {
      this.toggleBalanceEnabled();
      return;
    }
    if (leaf.action === 'toggle-bubble-backdrop') {
      this.setBubbleOption('bubbleBackdropEnabled', !bubbleBackdropOn());
      return;
    }
    if (leaf.action === 'toggle-bubble-rise') {
      this.setBubbleOption('bubbleRiseSfxEnabled', !bubbleRiseSfxOn());
      return;
    }

    if (leaf.action === 'toggle-ocr-watch') {
      this.setOcrWatch(!(config && config.visionWatchEnabled === true));
      return;
    }
    if (leaf.action === 'sleep' || leaf.action === 'wake') {
      // 只发请求，画面等 sleep 叶子回来才动：三段名与时机都由宿主/渲染端状态机决定（见 setSleep）
      this.postSleepAction(leaf.action);
      return;
    }
    if (leaf.action === 'whisper') {
      // leaf.image 只在「配图」菜单的子项上出现：指定这一句用哪张图，其余入口不带（仍随机抽图）。
      this.showWhisperFromMenu(leaf.image); // 立即让 host 强制新生成一句并展示（绕过节流；展示路径与周期触发一致）
      return;
    }
    if (leaf.action === 'chat') {
      this.showChatFromMenu(); // 打开对话弹窗（记忆经 host /chat 读写，浏览器/桌面同一实例共享）
      return;
    }
    if (leaf.action === 'todo') {
      this.toggleTodo(); // 打开待办面板（右键菜单入口）
      return;
    }
    if (leaf.action === 'home') {
      this.goHome(); // 停漫游/移动，清会话位置，回配置角落
      return;
    }
    if (leaf.action === 'set-display') {
      this.setPetDisplay(leaf.display); // 改这只宠物的显示位置：写 display 配置，由宿主重载生效
      return;
    }
    if (leaf.action === 'reload') {
      this.reloadDesktop(); // 请宿主重启桌面 Helper：全部桌面宠物窗口按最新配置重建
      return;
    }
    if (leaf.action === 'quit-pet') {
      this.quitPetCompletely(); // 关桌面窗 + 关托盘：宿主重启 helper 时会跳过拉起，等于彻底退出
      return;
    }
    if (!leaf.anim) return;
    // 用户从菜单主动点动作：旧台词与旧语音让位，别动作已经换了嘴里还在念上一句。
    // 宿主回推的 anim 叶子带 fromHost=true，不走这里（那是状态同步，不能打断同时下发的语音）。
    if (!fromHost) this.interruptSpeech();
    // 回执归属：这一段若来自第三方点播，编号跟着换源走（自播动画传 null）
    const playbackId = typeof leaf.playbackId === 'string' && leaf.playbackId ? leaf.playbackId : null;
    // 文字类（noMirror）朝右是镜像的：点播前强制朝左，避免文字镜像。keepFacing 时跳过（显式值优先）。
    if (!keepFacing && S.isNoMirrorAnimation(this.animations.categories, leaf.anim) && this.facing === 'right') {
      this.facing = 'left';
    }
    // 点播移动动画：走真实移动（与随机游走同一套），只有「选哪个动画」由菜单决定；挪不动则退化纯播放。
    if (this.animations.moves.actions.some((a) => a.name === leaf.anim)) {
      const moved = this.tryMove(leaf.anim, playbackId);
      if (moved === false) this.playOnce(leaf.anim, playbackId);
      // moved === true = 已有移动/抛掷在进行中（没换源）：本段不会真的播起来 → 如实回 failed/busy
      else if (moved === true && playbackId) this.reportPlayback(playbackId, 'failed', 'busy');
      return;
    }
    this.playOnce(leaf.anim, playbackId);
  },

  closeMenu() {
    if (this.menuClose) {
      this.menuClose();
      this.menuClose = null;
    }
    this.menuOpen = false;
    window.__dshPetDebug.menuOpen = false;
    this.syncInputBusy(); // 菜单关：若没有别的占用（拖拽/弹窗）则交还常规判定
    if (!this.inputBusy()) this.setInteractive(false);
  },

  // 「查看余额」菜单：POST 动作让宿主刷新余额。结果这里什么都不用做——写叶子会让版本号变，
  // 挂着的长轮询那一拍立刻返回并展示（同一展示路径）。数据只有一个出口（S），菜单只负责让它刷新。
  // 不做 balanceEnabled 门控：手动请求不受开关限制，关了余额的宠物被点一次也该看到数据。
  showBalanceFromMenu() {
    S.postAction(BALANCE_URL)
      .then((ok) => {
        if (!ok) console.warn('[dsh-pet] 菜单查看余额：刷新动作未成功');
      })
      .catch((e) => {
        console.error('[dsh-pet] 菜单查看余额异常', e);
      });
  },

  /** 声线目录：中枢 /bot/voices（与网页“声线管理”共用 cosyvoice profiles）。拉一次缓存 30s。 */
  ensureVoiceCatalog(force) {
    const now = Date.now();
    if (!force && this.voiceCatalog && now - (this.voiceCatalogAt || 0) < 30000) return;
    if (this.voiceCatalogPending) return;
    this.voiceCatalogPending = true;
    fetch(HUB_BASE + '/bot/voices', { cache: 'no-store' })
      .then((res) => res.json())
      .then((json) => {
        if (!json || json.ok !== true || !json.data) throw new Error('声线列表读取失败');
        this.voiceCatalog = {
          active: String(json.data.active || ''),
          voices: (Array.isArray(json.data.voices) ? json.data.voices : [])
            .map((v) => String((v && v.name) || ''))
            .filter(Boolean),
        };
        this.voiceCatalogAt = Date.now();
      })
      .catch((e) => console.warn('[dsh-pet] 声线列表读取失败', e))
      .then(() => {
        this.voiceCatalogPending = false;
      });
  },

  /** 菜单里的“声线”分支：常用两条 + 关闭语音 + 音量；其余声线去网页“声线管理”。 */
  buildVoiceBranch() {
    const vc = this.voiceCatalog || { active: '', voices: [] };
    const cfg = S.getVoiceConfig ? S.getVoiceConfig() : { enabled: false, volume: 1 };
    const on = cfg.enabled === true;
    const active = vc.active || '';
    const vol = Math.round((typeof cfg.volume === 'number' ? cfg.volume : 1) * 100);
    // 声线列表：优先列中枢 /bot/voices 返回的全部声线（当前 5 条），
    // 拉不到目录时才退回两条内置快捷项。
    const names = (Array.isArray(vc.voices) ? vc.voices : [])
      .map((v) => (typeof v === 'string' ? v : (v && v.name) || ''))
      .filter(Boolean);
    const shortcuts = names.length ? names : VOICE_SHORTCUTS;
    const children = shortcuts.map((name) => ({
      label: name + (name === active ? '（使用中）' : ''),
      action: 'set-voice',
      voice: name,
    }));
    children.push({ label: '关闭语音' + (on ? '' : '（已关）'), action: 'disable-voice' });
    children.push({
      label: '音量：' + vol + '%',
      children: VOLUME_STEPS.map((p) => ({
        label: p + '%' + (p === vol ? '（当前）' : ''),
        action: 'set-volume',
        volume: p / 100,
      })),
    });
    return { label: '声线' + (on ? '：' + (active || '未设置') : '（已关）'), children };
  },

  /** 菜单里的「气泡」分支：背景泡泡显示、上浮音效两个开关（点击戳破只有视觉特效，无音效）。 */
  buildBubbleBranch() {
    const on = bubbleBackdropOn();
    return {
      label: '气泡' + (on ? '' : '（已隐藏）'),
      children: [
        { label: '背景泡泡：' + (on ? '开' : '关'), action: 'toggle-bubble-backdrop' },
        { label: '上浮音效：' + (bubbleRiseSfxOn() ? '开' : '关'), action: 'toggle-bubble-rise' },
      ],
    };
  },

  /** 气泡开关：写中枢 /pet/config，本端立即生效（不等 30s 轮询）。 */
  async setBubbleOption(key, next) {
    try {
      const res = await fetch(HUB_BASE + '/pet/config', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ [key]: next === true }),
      });
      const json = await res.json();
      if (!json || json.ok !== true) throw new Error((json && json.error) || '保存失败');
      if (config) config[key] = next === true;
      syncBubbleBackdrop();
    } catch (e) {
      console.warn('[dsh-pet] 气泡开关保存失败', e);
      this.showWhisper('气泡开关失败：' + (e && e.message ? e.message : e), undefined, undefined, 6000, false);
    }
  },

  /** 菜单里的「OCR 识屏」分支：定期识屏开关 + 立即识屏/点评两个手动动作。 */
  buildOcrBranch() {
    const on = config ? config.visionWatchEnabled === true : false;
    const sec = config ? config.visionWatchSec : 900;
    return {
      label: 'OCR 识屏' + (on ? '：每 ' + Math.round(sec / 60) + ' 分钟' : '（已关）'),
      children: [
        { label: '定期识屏反馈：' + (on ? '开' : '关'), action: 'toggle-ocr-watch' },
        { label: '立即识屏（只读文字）', action: 'ocr' },
        { label: '立即点评', action: 'narrate' },
      ],
    };
  },

  /** OCR 定期识屏开关：写中枢 visionWatchEnabled，本端立即同步（宿主按周期自行执行）。 */
  async setOcrWatch(next) {
    try {
      const res = await fetch(HUB_BASE + '/pet/config', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ visionWatchEnabled: next === true }),
      });
      const json = await res.json();
      if (!json || json.ok !== true) throw new Error((json && json.error) || '保存失败');
      if (config) config.visionWatchEnabled = next === true;
      this.showWhisper(next === true
        ? '定期识屏开啦，每 ' + Math.round((config ? config.visionWatchSec : 900) / 60) + ' 分钟看一眼屏幕'
        : '定期识屏关掉啦', undefined, undefined, 6000, false);
    } catch (e) {
      console.warn('[dsh-pet] 识屏开关保存失败', e);
      this.showWhisper('识屏开关失败：' + (e && e.message ? e.message : e), undefined, undefined, 6000, false);
    }
  },

  /** 切换声线：写中枢 profiles/.active，顺手把语音开关打开（选了声线就是要听）。 */
  async switchVoice(name) {
    if (!name) return;
    try {
      const res = await fetch(HUB_BASE + '/bot/voices', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ action: 'switch', name }),
      });
      const json = await res.json();
      if (!json || json.ok !== true) throw new Error((json && json.error) || '切换失败');
      const pres = await fetch(HUB_BASE + '/pet/config', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ voiceEnabled: true }),
      });
      const pjson = await pres.json();
      if (!pjson || pjson.ok !== true) throw new Error((pjson && pjson.error) || '打开语音失败');
      this.voiceCatalog = Object.assign({}, this.voiceCatalog, { active: name });
      this.ensureVoiceCatalog(true);
      const cfg = S.getVoiceConfig ? S.getVoiceConfig() : { volume: 1 };
      if (S.setVoiceConfig) S.setVoiceConfig(name, true, cfg.volume);
      this.showWhisper('声线换成「' + name + '」啦，下一句就用它～', undefined, undefined, 6000, false);
    } catch (e) {
      console.warn('[dsh-pet] 切换声线失败', e);
      this.showWhisper('声线切换失败：' + (e && e.message ? e.message : e), undefined, undefined, 6000, false);
    }
  },

  /** 关闭语音：写中枢 voiceEnabled=false；气泡/字幕照旧，只是不出声。 */
  async setVoiceEnabled(on) {
    try {
      const res = await fetch(HUB_BASE + '/pet/config', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ voiceEnabled: on === true }),
      });
      const json = await res.json();
      if (!json || json.ok !== true) throw new Error((json && json.error) || '保存失败');
      const cfg = S.getVoiceConfig ? S.getVoiceConfig() : {};
      if (S.setVoiceConfig) S.setVoiceConfig(cfg.name || '', on === true, cfg.volume);
      this.showWhisper(on === true ? '语音打开啦～' : '语音关掉啦，只说话不出声', undefined, undefined, 6000, false);
    } catch (e) {
      console.warn('[dsh-pet] 语音开关保存失败', e);
      this.showWhisper('语音开关失败：' + (e && e.message ? e.message : e), undefined, undefined, 6000, false);
    }
  },

  /** 音量档位：写中枢 voiceVolume（0–1），本端立即生效，不等 30s 轮询。 */
  async setVoiceVolume(volume) {
    const v = Math.min(1, Math.max(0, Number(volume) || 0));
    try {
      const res = await fetch(HUB_BASE + '/pet/config', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ voiceVolume: v }),
      });
      const json = await res.json();
      if (!json || json.ok !== true) throw new Error((json && json.error) || '保存失败');
      const cfg = S.getVoiceConfig ? S.getVoiceConfig() : { enabled: false };
      if (S.setVoiceConfig) S.setVoiceConfig(cfg.name || '', cfg.enabled === true, v);
      this.showWhisper('音量调到 ' + Math.round(v * 100) + '%', undefined, undefined, 5000, false);
    } catch (e) {
      console.warn('[dsh-pet] 音量保存失败', e);
      this.showWhisper('音量保存失败：' + (e && e.message ? e.message : e), undefined, undefined, 6000, false);
    }
  },

  /** 打开救赎中枢 WebUI：优先新开系统浏览器，不退化成宿主窗口聚焦。 */
  openHub() {
    if (window.petBridge && typeof window.petBridge.openExternal === 'function') {
      window.petBridge.openExternal(HUB_BASE);
      return;
    }
    if (window.petBridge && typeof window.petBridge.openDshSite === 'function') {
      window.petBridge.openDshSite(HUB_BASE);
      return;
    }
    window.open(HUB_BASE, '_blank');
  },

  /** 识屏 OCR：读当前窗口文字，直接说给你听（宿主 /vision/look）。 */
  async lookScreenFromMenu() {
    try {
      const res = await fetch(BASE + '/vision/look', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ region: 'window', maxLines: 60, maxChars: 600 }),
      });
      const json = await res.json();
      if (!json || json.ok !== true) throw new Error((json && (json.message || json.error)) || '识屏失败');
      const text = String(json.text || '')
        .replace(/\s+/g, ' ')
        .trim();
      if (!text) throw new Error('当前窗口没识别到文字');
      const title = json.window && json.window.title ? '「' + json.window.title + '」' : '';
      const head = text.length > 100 ? text.slice(0, 100) + '…' : text;
      this.showWhisper('我识到' + title + '：' + head, undefined, undefined, 12000, false);
    } catch (e) {
      this.showWhisper('识屏失败：' + (e && e.message ? e.message : e), undefined, undefined, 8000, false);
    }
  },

  /** 看屏点评：OCR + 本地模型点评，成功时宿主自己写 say 叶子（气泡由长轮询展示）。 */
  async narrateScreenFromMenu() {
    try {
      const res = await fetch(BASE + '/vision/narrate?pet=' + encodeURIComponent(this.pet.id), { method: 'POST' });
      const json = await res.json();
      if (!json || json.ok !== true) {
        const code = json && (json.error || json.reason);
        throw new Error(code === 'disabled' ? '看屏点评未开启（WebUI 参数里打开「看屏点评」）' : (json && json.message) || '看屏点评失败');
      }
    } catch (e) {
      this.showWhisper('看屏点评失败：' + (e && e.message ? e.message : e), undefined, undefined, 8000, false);
    }
  },

  /** 试听：直接播放中枢返回的参考音（audio/wav）。 */
  playVoicePreview(name) {
    const target = name || (this.voiceCatalog && this.voiceCatalog.active);
    if (!target) return;
    const cfg = S.getVoiceConfig ? S.getVoiceConfig() : { volume: 1 };
    const audio = new Audio(HUB_BASE + '/bot/voices/audio?name=' + encodeURIComponent(target));
    audio.volume = typeof cfg.volume === 'number' ? Math.min(1, Math.max(0, cfg.volume)) : 1;
    audio.play().catch((e) => console.warn('[dsh-pet] 试听失败', e));
  },

  /** 余额显示开关：写中枢白名单字段 balanceEnabled（渲染端 30s 轮询后同步）。 */
  toggleBalanceEnabled() {
    const next = this.pet.balanceEnabled !== true;
    fetch(HUB_BASE + '/pet/config', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ balanceEnabled: next }),
    })
      .then((res) => res.json())
      .then((json) => {
        if (!json || json.ok !== true) throw new Error((json && json.error) || '保存失败');
        this.pet.balanceEnabled = next;
        if (next) this.showBalanceFromMenu();
        // 关掉时立刻收掉正在显示的余额文字，不等下一轮配置轮询（否则会残留几十秒）
        if (!next) {
          this.bubbleOn = false;
          this.balanceView = null;
          this.balanceState = null;
          if (this.balanceRetryTimer != null) window.clearTimeout(this.balanceRetryTimer);
          this.balanceRetryTimer = null;
          this.renderBubble();
        }
        this.showWhisper('余额显示已' + (next ? '打开' : '关闭') + '～', undefined, undefined, 6000, false);
      })
      .catch((e) => console.warn('[dsh-pet] 余额开关保存失败', e));
  },

  // 「碎碎念」菜单：POST 动作让宿主立即新生成一句（写进 S）。动作不回文本，文本随版本号变化由
  // 挂着的长轮询送达。手动触发不受 whisperEnabled 限制（该字段只关自动周期）。
  // image 给了就只用这张（「配图」菜单点的是具体某张）：不在池内宿主回 unknown-image，本端只记日志。
  showWhisperFromMenu(image) {
    // surface 必填：本窗口就是桌面端
    // 桌面窗口是 file://，语音接口得走 bridge 绝对地址；念不念由 say.voiceTo 决定，
    // 本端身份在启动时已经 S.setSurface('desktop') 登记过。
    S.setVoiceBase(BASE);
    // 图名是配置里的中文键，必须编码（可能含空格与全角字符）
    const pic = image ? '&image=' + encodeURIComponent(image) : '';
    S.postAction(WHISPER_URL + '?pet=' + encodeURIComponent(this.pet.id) + '&surface=desktop' + pic)
      .then((ok) => {
        if (!ok)
          console.warn('[dsh-pet] 菜单碎碎念：生成动作未成功 pet=' + this.pet.id + (image ? ' image=' + image : ''));
      })
      .catch((e) => {
        console.warn('[dsh-pet] 菜单碎碎念异常', e);
      });
  },

  // 「对话」菜单：最简输入框（shared 组件）——回车发送后弹窗消失，回复用碎碎念同款显示（说话动画 +
  // 气泡 10s），只多一步用户输入。记忆经宿主 /chat 读写，同一实例的浏览器与桌面共享。
  // 弹窗跟随身体命中区 this.hit（桌宠在视频中间，视频框右上角 ≠ 宠物右上角），超出视口自动夹回；
  // 弹窗是窗口内 DOM，期间整窗保持可交互，关闭后恢复命中区穿透。
  /** 悬停时粘贴一张图：弹窗没开就带着图打开，开着就塞进去；纯文字交给系统自己处理。 */
  onPasteImage(e) {
    if (!this.hoverBody) return;
    // 焦点在输入框里是正常粘贴，抢了会把文字粘丢——那种情况弹窗自己会接
    const active = document.activeElement;
    if (active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA' || active.isContentEditable)) return;
    const file = S.firstImageFromClipboard(e.clipboardData);
    if (!file) return;
    e.preventDefault();
    if (this.chatMount) {
      void this.chatMount.setImageFile(file);
      return;
    }
    this.showChatFromMenu(file);
  },

  /** 截一帧（主进程取宠物所在屏）；形状与浏览器侧 EncodedImage 一致，拿不到就抛给调用方决定。 */
  async captureEncoded() {
    if (!window.petBridge || typeof window.petBridge.captureScreen !== 'function') {
      throw new Error('当前环境不支持截屏');
    }
    const shot = await window.petBridge.captureScreen();
    if (!shot) throw new Error('没有取到屏幕画面');
    return {
      base64: shot.data,
      mediaType: shot.mediaType,
      bytes: shot.bytes,
      width: shot.width,
      height: shot.height,
      resized: true,
      previewUrl: 'data:' + shot.mediaType + ';base64,' + shot.data,
    };
  },

  /** 截图快捷键：弹窗开着就把图塞进当前弹窗，没开就带着图打开；截不到也开弹窗把原因写出来。 */
  async screenshotFromShortcut() {
    let encoded;
    try {
      encoded = await this.captureEncoded();
    } catch (error) {
      const reason = '截屏失败：' + (error && error.message ? error.message : error);
      if (this.chatMount) {
        this.chatMount.flash(reason);
        return;
      }
      this.showChatFromMenu();
      if (this.chatMount) this.chatMount.flash(reason);
      return;
    }
    if (this.chatMount) {
      this.chatMount.setEncoded(encoded);
      return;
    }
    this.showChatFromMenu(undefined, encoded);
  },

  showChatFromMenu(image, encoded) {
    if (this.chatMount) {
      this.chatMount.close();
      this.chatMount = null;
      return; // 已开着：先关旧的
    }
    // 语音接口同样走 bridge 地址；念不念由 say.voiceTo 决定（本端身份启动时已 setSurface('desktop')）
    S.setVoiceBase(BASE);
    const m = S.mountChatDialog({
      petId: this.pet.id,
      baseUrl: BASE + '/chat',
      surface: 'desktop',
      x: Math.max(4, this.hit.getBoundingClientRect().right + 6),
      y: Math.max(4, this.hit.getBoundingClientRect().top + 6),
      // 拖拽落到宠物身上时直接带着图开弹窗
      ...(image ? { initialImage: image } : {}),
      // 截图快捷键：截到的帧是 base64，没有 File
      ...(encoded ? { initialEncoded: encoded } : {}),
      // 截屏：主进程取宠物所在屏的一帧（JPEG base64），形状与浏览器侧一致
      capture: () => this.captureEncoded(),
      // 弹窗同菜单：只允许在「窗口 ∩ 工作区」内显示，贴边时不被屏幕裁掉
      clamp: this.visibleClampRect(),
      onSent: () => {
        // 发送成功：回复已写进 S → 版本号变 → 挂着的长轮询立刻返回并弹气泡
        console.info('[dsh-pet] 对话已发送 pet=' + this.pet.id);
      },
      onClose: () => {
        this.chatMount = null;
        this.chatOpen = false;
        window.__dshPetDebug.chatOpen = false;
        this.syncInputBusy(); // 弹窗关：交还常规判定（菜单还开着的话仍保持忙）
        if (!this.menuOpen) this.setInteractive(false); // 无菜单时恢复命中区穿透
      },
    });
    this.chatMount = m;
    this.chatOpen = true; // 穿透守卫：弹窗期间整窗保持可交互
    window.__dshPetDebug.chatOpen = true;
    this.syncInputBusy();
    this.setInteractive(true);
  },

  // 快捷键「按住 M 说话」的两端：按下时若没有弹窗就先开一个，再让它开录；松开只收尾、不关窗，
  // 识别结果落进输入框等用户确认（不自动发送）。
  startHearChat() {
    if (!this.chatMount) this.showChatFromMenu();
    if (this.chatMount) this.chatMount.startHear();
  },

  stopHearChat() {
    if (this.chatMount) this.chatMount.stopHear();
  },

  // 待办面板开关：面板占气泡槽（优先级最高），期间整窗保持可交互，关闭后交还命中区穿透。
  // 与浏览器半侧共用 shared/todo/panel，只有接口基址不同（桌面是 file:// 页面，必须绝对地址）。
  toggleTodo() {
    if (this.todoPanel) {
      this.closeTodo();
      return;
    }
    const panel = S.createTodoPanel({
      baseUrl: BASE + '/todos',
      onClose: () => this.closeTodo(),
      // 点宠物本体不收起：拖它走是常见需求，收起就没法一边看清单一边挪位置。
      keepOpenOn: (target) => typeof target?.closest === 'function' && target.closest('.pet-hit') !== null,
    });
    // replaceChildren 而不是 appendChild：气泡里还留着上一次渲染的行（碎碎念/余额），
    // 而 renderBubble 在面板开着时会提前返回、走不到清空那一步，不清就会叠在面板上方。
    this.bubble.replaceChildren(panel.el);
    this.todoPanel = panel;
    this.todoOpen = true;
    window.__dshPetDebug.todoOpen = true;
    this.renderBubble();
    this.syncInputBusy();
    this.setInteractive(true);
  },

  // 收起待办面板：摘掉面板节点、撤掉气泡变体类，再把槽位交回正常判定。
  closeTodo() {
    const panel = this.todoPanel;
    if (!panel) return;
    panel.dispose();
    this.todoPanel = null;
    this.todoOpen = false;
    window.__dshPetDebug.todoOpen = false;
    this.renderBubble();
    if (!this.menuOpen && !this.chatOpen) this.setInteractive(false);
  },

  // 「回到初始位置」菜单：停掉漫游/移动，清掉会话位置，回到配置角落
  goHome() {
    this.stopThrow();
    this.stopMove();
    this.customPos = null;
    this.position();
  },

  // 「重载配置」菜单：请宿主重启桌面 Helper，与设置页「保存」同一条重启路径（宿主 POST /reload →
  // syncDesktop）。不在渲染端就地重拉：窗口数量/display/size 由宿主按 DSH_PET_PETS 建，且全局轮询是
  // 一次性启动的（loopsStarted），就地重建会漏启新循环或留下旧定时器。
  // 本请求由即将被重启的进程发出，响应可能永远不回（宿主杀进程时回调地址已失效），故不 await、
  // 不重试，失败只记日志——重载本身就是把本进程换掉。
  reloadDesktop() {
    fetch(BASE + '/reload', { method: 'POST' }).catch((e) => {
      console.warn('[dsh-pet] 重载配置请求失败（宿主不可达？）', e);
    });
  },

  // 「显示位置」菜单：改这一只宠物的 display，写它自己的配置，再由宿主重载生效。取值直接来自菜单项，
  // 渲染端不做推导（当前状态以配置为准）。不就地开关窗：窗口是进程启动时按 DSH_PET_PETS 一次性建的。
  // 与 reloadDesktop 同一处境：选「仅网页/都不」时本进程会在重载中被换掉，故不 await、不重试。
  setPetDisplay(display) {
    if (!display) return;
    fetch(BASE + '/pet-display', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ id: this.pet.id, display }),
    }).catch((e) => {
      console.warn('[dsh-pet] 显示位置请求失败（宿主不可达？）', e);
    });
  },

  /**
   * 彻底退出：全部宠物 display 改 web，同时 trayEnabled=false。
   * 宿主写盘后会重启桌面 Helper；启动判定是「无桌面宠物 && 不需要托盘」→ 直接跳过，
   * 于是本进程退出后不会被拉回来。想再开：DSH 插件设置里把显示位置改回桌面并打开托盘。
   */
  async quitPetCompletely() {
    try {
      const res = await fetch(BASE + '/config', { cache: 'no-store' });
      const merged = await res.json();
      const main = (merged && merged.main) || {};
      const pets = Array.isArray(main.pets) ? main.pets : [];
      if (!pets.length) throw new Error('配置里没有宠物');
      const put = await fetch(BASE + '/config', {
        method: 'PUT',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          pets: pets.map((pet) => ({ ...pet, display: 'web' })),
          trayEnabled: false,
        }),
      });
      const json = await put.json().catch(() => null);
      if (json && json.ok === false) throw new Error(json.error || '宿主拒绝保存');
    } catch (e) {
      // 宿主会在写完配置后停掉本进程，响应收不到是正常路径，不当错误处理
      console.warn('[dsh-pet] 彻底退出请求失败（宿主不可达？）', e);
    }
  },

  /**
   * 自定义节点树 → DOM（custom 形态的渲染器）。
   *
   * tag 与样式白名单在 shared 的解析层已裁过，这里只做映射：不用 innerHTML，文本走 createTextNode，
   * 第三方内容因而没有注入面。样式键是 camelCase，直接赋给 el.style 即可。
   */
  renderTree(node) {
    const el = document.createElement(node.tag);
    if (node.style) {
      for (const [key, value] of Object.entries(node.style)) el.style[key] = value;
    }
    if (node.tag === 'img') {
      el.src = node.src || '';
      el.alt = '';
      return el;
    }
    // 文本与子节点可同时存在：先文本、后子节点（与 textContent + appendChild 的顺序一致）
    if (node.text) el.appendChild(document.createTextNode(node.text));
    for (const child of node.children || []) el.appendChild(this.renderTree(child));
    return el;
  },
});
