/**
 * 气泡与悬浮提示的渲染：工作状态 / 碎碎念 / 余额三段内容共用一个气泡槽，按优先级择一显示。
 *
 * PetSprite 由 sprite.js 定义，本文件只往原型上挂方法，必须在其后加载。
 * 只读实例状态，除 DOM 与 syncInputBusy() 之外不写任何字段。
 * @module dsh-pet/runtime/electron-helper/sprite-bubble
 */
'use strict';

/** 背景板预设 / 底子类型白名单：配置在 renderer 已收敛过一次，这里再兜一层，防止运行时手改出非法值。 */
const BUBBLE_BOARD_PRESETS = ['glass', 'paper', 'night', 'custom'];
const BUBBLE_BOARD_MODES = ['solid', 'gradient', 'image'];

/**
 * 壁纸地址归一：http(s) / data: / file: 原样使用；Windows 盘符与 POSIX 绝对路径转成 file:// URL。
 * 桌面页本身是 file://，直接把 C:\x.jpg 塞进 url() 不会被当成图片地址；
 * 相对路径不做猜测（不同机器的当前目录不一致），返回空串让调用方回落预设底色。
 */
function bubbleBoardImageUrl(raw) {
  const v = typeof raw === 'string' ? raw.trim() : '';
  if (!v) return '';
  if (/^(https?:|data:|file:)/i.test(v)) return v;
  const p = v.replace(/\\/g, '/');
  if (/^[a-zA-Z]:\//.test(p)) return 'file:///' + encodeURI(p);
  if (p.startsWith('/')) return 'file://' + encodeURI(p);
  return '';
}

/** 颜色只放行 #hex / rgb() / hsl() / 颜色名：写错时回落到调用方给的默认值，不把任意字符串塞进样式。 */
function bubbleBoardColor(value, fallback) {
  const v = typeof value === 'string' ? value.trim() : '';
  if (/^#([0-9a-f]{3,8})$/i.test(v)) return v;
  if (/^(rgb|rgba|hsl|hsla)\([^)]*\)$/i.test(v)) return v;
  if (/^[a-z]{3,20}$/i.test(v)) return v;
  return fallback;
}

Object.assign(PetSprite.prototype, {
  setTip(on) {
    const next = !!on;
    if (next === this.tipOn) return;
    this.tipOn = next;
    this.renderTip();
  },

  /**
   * 悬浮提示渲染：文本 = 宠物显示名（宿主已保证非空）；位置 = 光标右下（装不下自动翻边），
   * 摆位判据与浏览器端共用 shared 的 tooltipPlacement。
   *
   * 光标存的是视口坐标，而提示是 position:fixed、DOM 坐标相对窗口，所以这里减掉窗口原点。
   * 存视口坐标而非窗口坐标，是因为窗口会随宠物移动：只在这里按当前原点重算一次，贴光标不会漂。
   */
  renderTip() {
    if (this.tip.textContent !== this.pet.name) this.tip.textContent = this.pet.name;
    const cursor = this.tipCursor;
    if (cursor) {
      const winX = this.pos.x - this.margin.l;
      const winY = this.pos.y - this.margin.t;
      const winW = this.size + this.margin.l + this.margin.r;
      const winH = this.winH + this.margin.t + this.margin.b;
      // 允许显示范围 = 窗口 ∩ 光标所在屏的工作区：只按窗口夹会画到屏幕外，只按工作区夹会落到
      // 窗口外（html/body 是 overflow:hidden，一样看不见）。
      const area = S.resolveRect(WORK_AREAS, cursor.x, cursor.y) ?? { x: winX, y: winY, width: winW, height: winH };
      const x0 = Math.max(winX, area.x);
      const y0 = Math.max(winY, area.y);
      const x1 = Math.min(winX + winW, area.x + area.width);
      const y1 = Math.min(winY + winH, area.y + area.height);
      const p = S.tooltipPlacement(
        { x: cursor.x - winX, y: cursor.y - winY },
        { w: this.tip.offsetWidth, h: this.tip.offsetHeight },
        { x: x0 - winX, y: y0 - winY, w: Math.max(0, x1 - x0), h: Math.max(0, y1 - y0) },
        this.size * S.TOOLTIP_GAP_RATIO,
      );
      this.tip.style.left = p.x + 'px';
      this.tip.style.top = p.y + 'px';
    }
    this.tip.classList.toggle('is-on', this.tipOn);
    window.__dshPetDebug.tooltipOn = this.tipOn;
  },
  /**
   * 主气泡背景板 / 壁纸：纯色、渐变、图片壁纸三种底，外加玻璃 / 纸张 / 暗夜三套预设。
   *
   * 默认关闭（bubbleBoardEnabled !== true）：此时不挂任何 is-board-* 类、不写 CSS 变量，
   * 观感与旧版逐像素一致。只给说话气泡与非终态工作气泡上底板——余额是纯文字态
   * （is-balance-plain 要求透明）、待办面板是外部节点树，两者一律跳过。
   * 气泡宽高仍由内容决定（.pet-bubble 的 max-content + max-width），底板只换 paint，不参与布局。
   */
  syncBubbleBoard(slot) {
    const el = this.bubble;
    if (!el) return;
    const cfg = config;
    const on = !!(cfg && cfg.bubbleBoardEnabled === true) && (slot === 'whisper' || slot === 'work');
    // 每次重算先摘干净：上一次若是 custom，留下的内联变量会盖住预设自己的颜色
    el.classList.remove(
      'is-board',
      'is-board-glass',
      'is-board-paper',
      'is-board-night',
      'is-board-custom',
      'is-board-solid',
      'is-board-gradient',
      'is-board-image',
      'is-board-text-light',
      'is-board-text-dark',
    );
    for (const name of [
      '--dsh-bub-board-fill',
      '--dsh-bub-board-image',
      '--dsh-bub-board-scrim',
      '--dsh-bub-board-tail',
    ]) {
      el.style.removeProperty(name);
    }
    if (!on) return;

    const preset = BUBBLE_BOARD_PRESETS.includes(cfg.bubbleBoardPreset) ? cfg.bubbleBoardPreset : 'glass';
    const mode = BUBBLE_BOARD_MODES.includes(cfg.bubbleBoardMode) ? cfg.bubbleBoardMode : 'gradient';
    // 只有 custom 会用到图片；空/非法地址按纯色兜底，不留一张加载不出来的底
    const image = preset === 'custom' && mode === 'image' ? bubbleBoardImageUrl(cfg.bubbleBoardImage) : '';
    el.classList.add('is-board', 'is-board-' + preset);

    // 文字明暗：玻璃/纸张用深字，暗夜用亮字；自定义壁纸默认亮字 + 暗遮罩，深色图上也能读清。
    let tone = cfg.bubbleBoardText;
    if (tone !== 'light' && tone !== 'dark') tone = preset === 'night' || image ? 'light' : 'dark';
    el.classList.add('is-board-text-' + tone);

    if (preset !== 'custom') return; // 预设底全写在 CSS 里，变量不用内联

    const effectiveMode = mode === 'image' && !image ? 'solid' : mode;
    el.classList.add('is-board-' + effectiveMode);
    const color = bubbleBoardColor(cfg.bubbleBoardColor, '#ffffff');
    const from = bubbleBoardColor(cfg.bubbleBoardGradientFrom, '#ffffff');
    const to = bubbleBoardColor(cfg.bubbleBoardGradientTo, '#dbe7ff');
    let fill = color;
    let tail = color;
    let imageLayer = 'none';
    let scrim = 'none';
    if (effectiveMode === 'gradient') {
      fill = 'linear-gradient(135deg, ' + from + ', ' + to + ')';
      tail = to;
    }
    if (image) {
      // 壁纸边缘颜色不可预知：尾巴由 CSS 隐藏（.is-board-image::after）
      imageLayer = 'url(' + JSON.stringify(image) + ')';
      const a = Math.min(0.8, Math.max(0, Number(cfg.bubbleBoardScrim) || 0));
      const rgb = tone === 'light' ? '0, 0, 0' : '255, 255, 255';
      scrim = 'linear-gradient(rgba(' + rgb + ', ' + a + '), rgba(' + rgb + ', ' + a + '))';
      tail = 'transparent';
    }
    el.style.setProperty('--dsh-bub-board-fill', fill);
    el.style.setProperty('--dsh-bub-board-image', imageLayer);
    el.style.setProperty('--dsh-bub-board-scrim', scrim);
    el.style.setProperty('--dsh-bub-board-tail', tail);
  },

  renderBubble() {
    // 待办面板占气泡槽且优先级最高：它是用户主动打开的窗口，其余气泡一律让位。
    // 面板是外部塞进来的 DOM，重建 innerHTML 会把它自己抹掉，所以这一段只补显隐与让位。
    this.bubble.classList.toggle('dsh-pet-todo-bubble', this.todoOpen);
    if (this.todoOpen) {
      this.bubble.classList.remove('is-whisper', 'is-clickable', 'is-expanded', S.MEME_BUBBLE_CLASS);
      this.bubble.classList.add('is-on');
      this.syncBubbleBoard('none'); // 待办面板是外部节点树：底板一律撤掉，不参与这套样式
      window.__dshPetDebug.lastBubbleTitle = '待办事项';
      this.syncInputBusy();
      return;
    }
    // 气泡单槽：同一时刻只显示一个（优先级：终态任务 > 余额 > 碎碎念/对话 > 非终态任务）。
    // 终态任务不让位：完成/失败气泡只活 10s，被顶掉就彻底看不到；非终态气泡常驻，让位零损失。
    // 工作气泡与碎碎念同款样式（宽度自适应 + 自动换行）；配图标记交给 CSS（带图时取消 min-width）。
    // 图片 URL 与视频同规则：传 BASE 前缀（桌面是 file:// 页面，必须绝对地址），素材根同理。
    const workOn = !!(this.workOn && this.workText);
    const whisperOn = !!(this.whisperOn && this.whisperView);
    // 说话/字幕正在占用气泡时不显示余额（「别人在用就先让开」），但余额不降级、不叠加：
    // 空下来就整块抢回气泡独占显示。
    const balanceOn = !!(this.bubbleOn && this.balanceView) && !whisperOn;
    const workTerminal = this.workState === 'success' || this.workState === 'error';
    // 优先级从高到低：终态任务 > 余额 > 说话 > 非终态任务。
    const slot =
      workTerminal && workOn ? 'work' : balanceOn ? 'balance' : whisperOn ? 'whisper' : workOn ? 'work' : 'none';
    // 记住当前槽位：点击处理在 init.js，必须知道「现在显示的是哪一种气泡」才能分清翻页与跳会话。
    this.bubbleSlot = slot;
    // 配图两种形态二选一：url 用完整地址，image 按素材根拼表情包路径；样式/class 共用。
    const whisperImg =
      slot === 'whisper'
        ? this.whisperUrl
          ? S.createMemeImageFromUrl(this.whisperUrl)
          : S.createMemeImage(this.whisperImage, BASE, this.assetRoot)
        : null;
    this.bubble.classList.toggle(
      'is-whisper',
      // 余额「文字说明」也套紧凑变体：字号更小、宽度下限更窄，多行说明更紧凑
      slot === 'whisper' || slot === 'work' || (slot === 'balance' && this.balanceWrap),
    );
    this.bubble.classList.toggle(S.MEME_BUBBLE_CLASS, !!whisperImg);
    // 可点/展开两个状态类：可点 → 气泡参与命中区（见 syncInputRegions）；index>0 → 更宽的盒与紧凑字号。
    // 展开态仅内置 windows 的窗口明细与用量页使用；custom 各页是对等页面，翻页不切紧凑字号。
    const isCustom = !!(this.balanceState && S.customViews(this.balanceState));
    const workClickable = this.workClickable();
    const clickable = (slot === 'balance' && this.balanceClickable()) || workClickable;
    const expanded = slot === 'balance' && clickable && !isCustom && this.balanceViewIndex > 0;
    this.bubble.classList.toggle('is-clickable', clickable);
    this.bubble.classList.toggle('is-expanded', expanded);
    // 余额只显示文字：去掉白底/圆角/阴影/尾巴，只留带描边的字（见 index.html 的 .is-balance-plain）
    this.bubble.classList.toggle('is-balance-plain', slot === 'balance');
    // 可选背景板 / 壁纸：默认关闭时这一步什么都不挂，观感与旧版保持一致
    this.syncBubbleBoard(slot);
    if (slot === 'none') {
      // 三者都没有可显示的内容：隐藏（不占位，也就不会挡住任何一层）
      this.bubble.classList.remove('is-on');
      window.__dshPetDebug.lastBubbleTitle = '';
      this.syncInputBusy(); // 气泡没了 → 撤销它的控件矩形与 busy 标记（否则整窗还留着可点区域）
      return;
    }
    this.bubble.innerHTML = '';
    if (slot === 'work') {
      const line = document.createElement('div');
      line.className = 'pet-bub-row';
      line.textContent = this.workText;
      this.bubble.appendChild(line);
    } else if (slot === 'whisper') {
      // 配图（shared 生成的 <img> + 共用样式）：先看图再读话
      if (whisperImg) this.bubble.appendChild(whisperImg);
      // 头像 + 文字：头像在左，随字号缩放到约 22px；没有头像就只渲染文字（不留空位）
      const say = document.createElement('div');
      say.className = 'pet-bub-say';
      if (this.whisperAvatar) {
        const avatar = document.createElement('img');
        avatar.className = 'pet-bub-avatar';
        avatar.src = this.whisperAvatar;
        avatar.alt = '';
        avatar.referrerPolicy = 'no-referrer';
        // 加载失败（失链/非图片）直接移除，避免留下一个空白圆框
        avatar.addEventListener('error', () => avatar.remove());
        say.appendChild(avatar);
      }
      const line = document.createElement('div');
      line.className = 'pet-bub-row';
      const whisperText = this.whisperView[0]?.text ?? '';
      line.textContent = whisperText;
      say.appendChild(line);
      this.bubble.appendChild(say);
      // 连续换句时补一个短促弹跳：气泡已经显示着、文字却变了，说明是新的一句。
      if (this.bubble.classList.contains('is-on') && whisperText !== this._lastWhisperText) {
        this.bubble.classList.remove('is-pop');
        void this.bubble.offsetWidth; // 强制重排，让同一段动画能重新播放
        this.bubble.classList.add('is-pop');
        clearTimeout(this._popTimer);
        this._popTimer = setTimeout(() => this.bubble.classList.remove('is-pop'), 520);
      }
      this._lastWhisperText = whisperText;
      // 语音状态角标：idle 时什么都不加；failed 用静音喇叭，表示这句没念出来
      const voice = S.getVoiceState();
      if (voice !== 'idle') {
        S.injectMemeBubbleCss(); // 角标样式与配图同一份 CSS，两端共用
        const badge = document.createElement('span');
        badge.className =
          'dsh-pet-voice-badge' + (voice === 'loading' ? ' is-loading' : '') + (voice === 'failed' ? ' is-failed' : '');
        badge.setAttribute('aria-hidden', 'true');
        if (voice === 'ready') badge.textContent = '🔊';
        else if (voice === 'failed') badge.textContent = '🔇';
        this.bubble.appendChild(badge);
      }
    } else if (this.balanceState && S.customViews(this.balanceState)) {
      // custom 形态：第三方自己的节点树。页索引取当前页；页数变少时夹到最后一页，不显示残页。
      const views = S.customViews(this.balanceState);
      const page = views[Math.min(this.balanceViewIndex, views.length - 1)];
      if (page) this.bubble.appendChild(this.renderTree(page));
    } else {
      // 展开态的行在这里现算：renderBubble 是只读渲染路径，不缓存（余额每轮都在变，缓存反而要同步失效）
      // 每页内容与页数同源（S.balancePageView / S.balancePageCount）：不再按「index>0 即窗口明细」二分化，
      // 否则新增的用量页会和明细挤在同一页。没有读数时回落到缓存的首屏行。
      const rows = this.balanceState ? S.balancePageView(this.balanceState, this.balanceViewIndex) : this.balanceView;
      const hasTier = rows.some((r) => r.role === 'tier');
      if (hasTier) {
        // deepseek 余额单行：余额（峰/谷）¥x — 档位字着色
        const line = document.createElement('div');
        line.className = 'pet-bub-row';
        for (const r of rows) {
          const span = document.createElement('span');
          if (r.role === 'tier') span.className = 'pet-bub-tier pet-bub-tier-' + r.tier;
          span.textContent = r.text;
          line.appendChild(span);
        }
        this.bubble.appendChild(line);
      } else {
        for (const r of rows) {
          // 展开态一行 = 一个窗口：窗口名 + 进度条 + 已用百分比 + 重置倒计时，四段 flex 同行，条吃剩余宽度。
          if (r.role === 'quota') {
            const row = document.createElement('div');
            row.className = 'pet-bub-quota';
            const label = document.createElement('span');
            label.className = 'pet-bub-quota-label';
            label.textContent = r.label;
            const bar = document.createElement('span');
            bar.className = 'pet-bub-quota-bar';
            const fill = document.createElement('i');
            fill.className = 'pet-bub-quota-fill pet-bub-quota-' + r.tone;
            fill.style.width = Math.max(0, Math.min(100, r.percent)) + '%';
            bar.appendChild(fill);
            const used = document.createElement('span');
            used.className = 'pet-bub-quota-used';
            used.textContent = r.usedText;
            const reset = document.createElement('span');
            reset.className = 'pet-bub-quota-reset';
            reset.textContent = r.resetText;
            row.append(label, bar, used, reset);
            this.bubble.appendChild(row);
            continue;
          }
          // 用量页：三行两列，总量是唯一大字；进/出各一处强调色，其余次级灰。
          if (r.role === 'usage') {
            const box = document.createElement('div');
            box.className = 'pet-bub-usage';
            const line = (cells) => {
              const row = document.createElement('div');
              row.className = 'pet-bub-usage-line';
              for (const cell of cells) {
                const span = document.createElement('span');
                span.className = cell.cls;
                span.textContent = cell.text;
                row.appendChild(span);
              }
              return row;
            };
            // 进出比例条：宽度即真实占比，输入通常压倒性占多数，条偏单色属正常。
            const bar = document.createElement('div');
            bar.className = 'pet-bub-usage-bar';
            if (r.inputPercent !== undefined) {
              const inBar = document.createElement('i');
              inBar.className = 'pet-bub-usage-bar-in';
              inBar.style.width = r.inputPercent + '%';
              const outBar = document.createElement('i');
              outBar.className = 'pet-bub-usage-bar-out';
              outBar.style.width = 100 - r.inputPercent + '%';
              bar.append(inBar, outBar);
            }
            box.append(
              line([
                { cls: 'pet-bub-usage-title', text: r.title },
                { cls: 'pet-bub-usage-total', text: r.total },
              ]),
            );
            if (r.inputPercent !== undefined) box.appendChild(bar);
            box.append(
              line([
                { cls: 'pet-bub-usage-in', text: r.input },
                { cls: 'pet-bub-usage-out', text: r.output },
              ]),
              line([
                { cls: 'pet-bub-usage-period', text: r.period },
                { cls: 'pet-bub-usage-spend', text: r.spend },
              ]),
            );
            this.bubble.appendChild(box);
            continue;
          }
          const div = document.createElement('div');
          if (r.role === 'error') div.className = 'pet-bub-err';
          else if (r.role === 'sub') div.className = 'pet-bub-row pet-bub-sub';
          else div.className = 'pet-bub-row';
          div.textContent = r.text;
          this.bubble.appendChild(div);
        }
      }
    }
    this.bubble.classList.add('is-on');
    window.__dshPetDebug.lastBubbleTitle = this.bubble.textContent.slice(0, 60);
    // 气泡内容/尺寸变了 → 重报控件矩形并同步 busy 标记。这里是所有显隐与换内容的汇聚点。
    this.syncInputBusy();
  },
});
