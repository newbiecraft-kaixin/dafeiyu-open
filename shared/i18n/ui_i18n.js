/* 大肥鱼界面多语言：按中文原文替换成英文，纯浏览器端实现，不需要构建工具。
 * 用法：页面里加 <script src="/i18n.js" defer></script>
 * 语言优先级：?lang=zh|en > localStorage > navigator.language（en 开头即英文）。
 * 切换语言时刷新页面，保证动态渲染出来的内容也从中文初始态重新翻译。 */
(function () {
  'use strict';

  var STORE = 'dafeiyu.lang';
  var dict = window.__DAFEIYU_I18N__ || {};
  if (!Object.keys(dict).length) return;
  var HAN = /[\u4e00-\u9fff]/;

  var query = null;
  try { query = new URLSearchParams(location.search).get('lang'); } catch (e) { /* 老浏览器忽略 */ }
  if (query === 'zh' || query === 'en') {
    try { localStorage.setItem(STORE, query); } catch (e) { /* 隐私模式忽略 */ }
  }
  var stored = null;
  try { stored = localStorage.getItem(STORE); } catch (e) { /* 隐私模式忽略 */ }
  var lang = stored || ((navigator.language || 'zh').toLowerCase().indexOf('en') === 0 ? 'en' : 'zh');

  function tr(text) {
    if (typeof text !== 'string' || !text) return text;
    var key = text.trim();
    if (!key) return text;
    var hit = dict[key];
    if (hit) return text.replace(key, hit);
    // 兜底：JS 里拼出来的文本（如「依赖：/usr/bin/x」「共 3 条」）没有整句 key，
    // 按词片段替换；只在片段两侧都不是中文时替换，避免把词拆坏。
    if (!HAN.test(key)) return text;
    return fragmentize(text, key);
  }

  // 把 text 中 key 的每次出现都按 dict 替换，两侧边界必须是数字/符号/英文/行首行尾
  function replaceBounded(text, k, value) {
    var out = '';
    var start = 0;
    var from = 0;
    var changed = false;
    while (from <= text.length - k.length) {
      var idx = text.indexOf(k, from);
      if (idx < 0) break;
      var before = idx === 0 ? '' : text.charAt(idx - 1);
      var after = idx + k.length >= text.length ? '' : text.charAt(idx + k.length);
      if (!HAN.test(before) && !HAN.test(after)) {
        out += text.slice(start, idx) + value;
        start = idx + k.length;
        changed = true;
      }
      from = idx + 1;
    }
    return changed ? out + text.slice(start) : null;
  }

  function fragmentize(text, key) {
    var lead = text.indexOf(key);
    var body = key;
    var changed = false;
    var keys = fragKeys();
    for (var pass = 0; pass < 3; pass++) {
      var did = false;
      for (var i = 0; i < keys.length; i++) {
        var k = keys[i];
        if (k.length >= body.length || body.indexOf(k) < 0) continue;
        var next = replaceBounded(body, k, dict[k]);
        if (next !== null) {
          body = next;
          did = true;
        }
      }
      if (!did) break;
      changed = true;
    }
    return changed ? text.slice(0, lead) + body + text.slice(lead + key.length) : text;
  }

  var _frag = null;
  function fragKeys() {
    if (_frag) return _frag;
    var list = [];
    for (var k in dict) if (dict.hasOwnProperty(k) && k.length > 0) list.push(k);
    list.sort(function (a, b) { return b.length - a.length; });
    _frag = list;
    return _frag;
  }

  var SKIP = { SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, TEXTAREA: 1, CODE: 1, PRE: 1 };
  var ATTRS = ['placeholder', 'title', 'aria-label', 'alt'];

  function walkAttrs(el) {
    for (var i = 0; i < ATTRS.length; i++) {
      var name = ATTRS[i];
      var value = el.getAttribute && el.getAttribute(name);
      if (value) {
        var next = tr(value);
        if (next !== value) el.setAttribute(name, next);
      }
    }
  }

  function walk(root) {
    if (!root) return;
    if (root.nodeType === 3) {
      var changed = tr(root.nodeValue);
      if (changed !== root.nodeValue) root.nodeValue = changed;
      return;
    }
    if (root.nodeType !== 1 || SKIP[root.tagName]) return;
    if (root.nodeType === 1) walkAttrs(root);
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    var node;
    while ((node = walker.nextNode())) {
      var parent = node.parentNode;
      if (!parent || SKIP[parent.tagName]) continue;
      var next = tr(node.nodeValue);
      if (next !== node.nodeValue) node.nodeValue = next;
    }
    if (root.querySelectorAll) {
      var els = root.querySelectorAll('[placeholder],[title],[aria-label],[alt]');
      for (var i = 0; i < els.length; i++) walkAttrs(els[i]);
    }
  }

  function observe() {
    if (!window.MutationObserver) return;
    var observer = new MutationObserver(function (list) {
      // 先断开，避免自己写入的内容再次触发翻译，造成死循环
      observer.disconnect();
      for (var i = 0; i < list.length; i++) {
        var record = list[i];
        if (record.type === 'characterData') walk(record.target);
        for (var j = 0; j < record.addedNodes.length; j++) walk(record.addedNodes[j]);
      }
      observer.observe(document.body, { childList: true, subtree: true, characterData: true });
    });
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
  }

  function mountToggle() {
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = lang === 'en' ? '中文' : 'EN';
    btn.title = lang === 'en' ? 'Switch to Chinese' : '切换到英文界面';
    btn.setAttribute('aria-label', btn.title);
    btn.style.cssText = [
      'position:fixed', 'right:14px', 'bottom:14px', 'z-index:2147483647',
      'padding:6px 12px', 'border-radius:999px', 'cursor:pointer',
      'border:1px solid rgba(120,150,200,.5)', 'background:rgba(20,26,40,.88)',
      'color:#cfe0ff', 'font:12px/1.4 system-ui,sans-serif', 'backdrop-filter:blur(6px)',
      'opacity:.75'
    ].join(';');
    btn.onmouseenter = function () { btn.style.opacity = '1'; };
    btn.onmouseleave = function () { btn.style.opacity = '.75'; };
    btn.onclick = function () {
      try { localStorage.setItem(STORE, lang === 'en' ? 'zh' : 'en'); } catch (e) { /* 忽略 */ }
      location.reload();
    };
    document.body.appendChild(btn);
  }

  function boot() {
    document.documentElement.lang = lang === 'en' ? 'en' : 'zh-CN';
    mountToggle();
    if (lang !== 'en') return;
    if (document.title) document.title = tr(document.title);
    walk(document.body);
    observe();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
