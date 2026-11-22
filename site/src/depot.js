// The cover is an enhancement: reading and downloading never wait for motion.
// CD bonus track. Keep normal clicks inert; assistive activation has no click count.
(() => {
  const trigger = document.querySelector('.disc-trigger');
  const dialog = document.querySelector('#bonus-track');
  if (!trigger || !dialog?.showModal) return;
  const pictures = dialog.querySelectorAll('.bonus-screen img');
  const letter = dialog.querySelector('.bonus-letter');
  const count = dialog.querySelector('.bonus-count');
  const next = dialog.querySelector('.bonus-next');
  let timer, track = 0, lastTap = null;

  function show(index) {
    clearTimeout(timer);
    track = index;
    pictures[0].hidden = index !== 0;
    pictures[1].hidden = index === 0;
    letter.hidden = index !== 2;
    count.textContent = ['01 / 03', '02 / 03', '03 / 03'][index];
    next.textContent = index === 2 ? '重播 ↺' : '下一轨 ▸';
    if (index < 2) timer = setTimeout(() => { if (dialog.open) show(index + 1); }, index === 0 ? 1800 : 2600);
  }
  function playBonus() {
    if (dialog.open) return;
    lastTap = null;
    dialog.showModal();
    dialog.dispatchEvent(new Event('bonusplay'));
    show(0);
  }
  trigger.disabled = false;
  trigger.addEventListener('click', event => {
    const tap = { time: event.timeStamp, x: event.clientX, y: event.clientY };
    const nearby = lastTap && tap.time - lastTap.time <= 750 &&
      Math.hypot(tap.x - lastTap.x, tap.y - lastTap.y) <= 32;
    if (event.detail === 0 || event.detail === 2 || nearby) playBonus();
    else lastTap = tap;
  });
  trigger.addEventListener('dblclick', playBonus);
  trigger.addEventListener('pointercancel', () => { lastTap = null; });
  next.addEventListener('click', () => show((track + 1) % 3));
  dialog.addEventListener('close', () => { clearTimeout(timer); if (!dialog.open) trigger.focus({ preventScroll: true }); });
})();

(() => {
  const button = document.querySelector('#fx-switch');
  if (!button) return;
  const motion = matchMedia('(prefers-reduced-motion: reduce)');
  let off = false;
  try { off = localStorage.getItem('ferry-fx') === 'off'; } catch {}
  function sync() {
    const on = !off && !motion.matches;
    document.documentElement.classList.toggle('fx-on', on);
    button.hidden = false;
    button.disabled = motion.matches;
    button.setAttribute('aria-pressed', String(on));
    button.textContent = motion.matches ? '系统已减弱动态' : `特效：${on ? '开' : '关'}`;
  }
  button.addEventListener('click', () => {
    off = !off;
    try { localStorage.setItem('ferry-fx', off ? 'off' : 'on'); } catch {}
    sync();
  });
  motion.addEventListener('change', sync);
  sync();
})();

(() => {
  const C = FerryCatalog;
  const $ = s => document.querySelector(s);
  const el = (name, value, cls) => {
    const n = document.createElement(name);
    if (value !== undefined && value !== null) n.textContent = value;
    if (cls) n.className = cls;
    return n;
  };

  const snapshotEl = $('#ferry-snapshot');
  const snapshot = snapshotEl ? JSON.parse(snapshotEl.textContent) : { when: '', rows: [], notes: {}, labels: {} };

  // --- Release Filter & Broadcast Ticker ---
  let pick = 'all';
  const filters = $('#filters');
  if (filters) {
    filters.hidden = false;
    filters.addEventListener('click', e => {
      const b = e.target.closest('button');
      if (b) {
        pick = b.dataset.pick;
        filter();
      }
    });
  }

  function filter() {
    let count = 0;
    for (const row of document.querySelectorAll('#chain li')) {
      row.hidden = pick !== 'all' && row.dataset.kind !== pick;
      if (!row.hidden) count++;
    }
    const empty = $('#archive-empty');
    if (empty) empty.hidden = count > 0;
    filters?.querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.pick === pick)));
  }

  function syncTickerSpeed() {
    const track = $('#broadcast-track');
    if (!track) return;
    const first = track.querySelector('.ticker-group');
    if (!first) return;
    const w = first.getBoundingClientRect().width || 900;
    const secs = Math.max(20, Math.min(85, Math.round(w / 52)));
    track.style.setProperty('--ticker-dur', `${secs}s`);
  }

  function display(rows, notes, labels = {}) {
    const track = $('#broadcast-track');
    if (track) track.innerHTML = C.broadcast(rows, notes, labels);
    const headEl = $('#release-head');
    if (headEl) headEl.innerHTML = C.head(rows, labels);
    const chainEl = $('#chain');
    if (chainEl) chainEl.innerHTML = C.chain(rows, notes, labels);
    const notesEl = $('#readme-notes');
    if (notesEl) notesEl.innerHTML = C.noteHTML(notes, rows.find(x => x.kind === 'app')?.tag);
    filter();
    requestAnimationFrame(syncTickerSpeed);
  }
  syncTickerSpeed();

  // --- GitHub Identity & Forum Avatar System ---
  const USER_KEY = 'ferry-gh-user-v1';
  const TOKEN_KEY = 'ferry-gh-token-v1';

  let ghUser = null;
  let ghToken = '';

  function safeAvatarUrl(url) {
    if (typeof url !== 'string') return '';
    try {
      const u = new URL(url);
      if (u.protocol !== 'https:' || u.hostname !== 'avatars.githubusercontent.com') return '';
      u.searchParams.set('s', '96');
      return u.toString();
    } catch {
      return '';
    }
  }

  function profileUrl(login) {
    return `https://github.com/${encodeURIComponent(login)}`;
  }

  function loadAuth() {
    try {
      const raw = sessionStorage.getItem(USER_KEY) || localStorage.getItem(USER_KEY);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed.login === 'string' && parsed.login) {
          ghUser = {
            login: parsed.login,
            name: parsed.name || '',
            avatar_url: safeAvatarUrl(parsed.avatar_url),
            mode: parsed.mode === 'token' ? 'token' : 'handle'
          };
        }
      }
      ghToken = sessionStorage.getItem(TOKEN_KEY) || localStorage.getItem(TOKEN_KEY) || '';
      if (ghUser && ghUser.mode === 'token' && !ghToken) {
        ghUser.mode = 'handle';
      }
    } catch {
      ghUser = null;
      ghToken = '';
    }
  }

  function saveAuth(user, token, sessionOnly) {
    ghUser = user;
    ghToken = token || '';
    try {
      sessionStorage.removeItem(USER_KEY);
      sessionStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(USER_KEY);
      localStorage.removeItem(TOKEN_KEY);
      if (user) {
        const store = (token && sessionOnly) ? sessionStorage : localStorage;
        store.setItem(USER_KEY, JSON.stringify(user));
        if (token) store.setItem(TOKEN_KEY, token);
      }
    } catch {}
  }

  function clearAuth() {
    saveAuth(null, '', true);
    renderUserState();
  }

  function makeAvatar(user, sizeClass = '') {
    const box = el('span', '', `avatar-box${sizeClass ? ' ' + sizeClass : ''}${user ? '' : ' avatar-guest'}`);
    if (!user) {
      box.textContent = '?';
      box.setAttribute('aria-hidden', 'true');
      return box;
    }
    const src = safeAvatarUrl(user.avatar_url);
    const initial = (user.login || '?').slice(0, 1).toUpperCase();
    if (src) {
      const img = document.createElement('img');
      img.src = src;
      img.alt = `${user.login} 的 GitHub 头像`;
      img.loading = 'lazy';
      img.decoding = 'async';
      img.referrerPolicy = 'no-referrer';
      img.addEventListener('error', () => {
        img.remove();
        box.textContent = initial;
      }, { once: true });
      box.append(img);
    } else {
      box.textContent = initial;
    }
    return box;
  }

  function makeAuthorCol(user, opts = {}) {
    const col = el('div', '', 'post-author');
    col.append(makeAvatar(user, 'avatar-lg'));
    if (user && user.login) {
      const link = el('a', user.login, 'post-author-name');
      link.href = profileUrl(user.login);
      link.target = '_blank';
      link.rel = 'noopener';
      col.append(link);
    } else {
      col.append(el('span', '游客（未登录）', 'post-author-name'));
    }

    const badges = el('div', '', 'post-badges');
    if (opts.isOwner) badges.append(el('span', '站长', 'user-badge badge-owner'));
    if (opts.isOp) badges.append(el('span', '楼主', 'user-badge badge-op'));
    if (opts.isMe) badges.append(el('span', '我', 'user-badge badge-me'));
    if (opts.modeBadge) badges.append(el('span', opts.modeBadge, 'user-badge badge-mode'));
    if (badges.childElementCount) col.append(badges);

    if (opts.showLoginBtn && !user) {
      const btn = el('button', '登录 GitHub', 'win-btn win-btn-sm');
      btn.type = 'button';
      btn.addEventListener('click', openLoginDialog);
      col.append(btn);
    }
    return col;
  }

  const req = async (url, opts = {}) => {
    const { raw = false, method = 'GET', body, token } = opts;
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 14000);
    const headers = {
      Accept: raw ? 'text/plain' : 'application/vnd.github+json'
    };
    const activeToken = token !== undefined ? token : (url.startsWith('https://api.github.com/') ? ghToken : '');
    if (activeToken) headers.Authorization = `Bearer ${activeToken}`;
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    try {
      const res = await fetch(url, {
        method,
        credentials: 'omit',
        signal: ctrl.signal,
        headers,
        body: body !== undefined ? JSON.stringify(body) : undefined
      });
      if (!res.ok) {
        const err = Error(`HTTP ${res.status}`);
        err.status = res.status;
        throw err;
      }
      return raw ? await res.text() : await res.json();
    } finally {
      clearTimeout(timer);
    }
  };

  function renderComposerAuthor(target) {
    if (!target) return;
    const col = makeAuthorCol(ghUser, {
      isMe: !!ghUser,
      modeBadge: ghUser ? (ghUser.mode === 'token' ? '直发' : 'GitHub') : ''
    });
    if (!ghUser) {
      const btn = el('button', '登录 GitHub', 'win-btn win-btn-sm');
      btn.type = 'button';
      btn.addEventListener('click', openLoginDialog);
      col.append(btn);
    }
    target.replaceChildren(...col.childNodes);
  }

  function renderUserState() {
    const bar = $('#bbs-userbar');
    if (bar) {
      bar.replaceChildren();
      const card = el('div', '', 'bbs-user-card');
      card.append(makeAvatar(ghUser));
      const meta = el('div', '', 'bbs-user-meta');
      const ops = el('div', '', 'bbs-user-ops');

      if (ghUser) {
        const title = el('strong', '');
        const link = el('a', `@${ghUser.login}`);
        link.href = profileUrl(ghUser.login);
        link.target = '_blank';
        link.rel = 'noopener';
        title.append(link);
        if (ghUser.name && ghUser.name !== ghUser.login) {
          title.append(document.createTextNode(` (${ghUser.name})`));
        }
        title.append(el('span', ghUser.mode === 'token' ? '站内直发已就绪' : 'GitHub 身份已绑定', 'user-badge badge-mode'));
        meta.append(title);
        meta.append(el('small', ghUser.mode === 'token'
          ? '已通过 GitHub Token 鉴权，可直接在下方讨论串发表主题或楼层回复。'
          : '回帖栏与发帖栏已挂载你的 GitHub 头像与用户名；提交时将跳转 GitHub 完成发布。'));

        const writeBtn = el('button', '写新反馈', 'win-btn win-btn-sm');
        writeBtn.type = 'button';
        writeBtn.addEventListener('click', () => toggleNewTopic(true));
        const switchBtn = el('button', '切换账号…', 'win-btn win-btn-sm');
        switchBtn.type = 'button';
        switchBtn.addEventListener('click', openLoginDialog);
        const outBtn = el('button', '退出', 'win-btn win-btn-sm');
        outBtn.type = 'button';
        outBtn.addEventListener('click', clearAuth);
        ops.append(writeBtn, switchBtn, outBtn);
      } else {
        meta.append(el('strong', '游客（未登录 GitHub）'));
        meta.append(el('small', '登录 GitHub 进行 Issue 反馈后，讨论串与回帖栏将显示你的 GitHub 头像与用户名。'));
        const loginBtn = el('button', '登录 GitHub…', 'win-btn');
        loginBtn.type = 'button';
        loginBtn.addEventListener('click', openLoginDialog);
        ops.append(loginBtn);
      }

      card.append(meta);
      bar.append(card, ops);
    }

    renderComposerAuthor($('#new-topic-author'));
    const newSubmit = $('#new-topic-submit');
    if (newSubmit) {
      newSubmit.textContent = (ghUser && ghUser.mode === 'token') ? '直接发表到 GitHub Issues' : '前往 GitHub 预填发表 ↗';
    }

    document.querySelectorAll('.reply-compose-author').forEach(node => renderComposerAuthor(node));
    document.querySelectorAll('.reply-submit-btn').forEach(btn => {
      btn.textContent = (ghUser && ghUser.mode === 'token') ? '直接回复此帖' : '复制内容并去 GitHub 回复 ↗';
    });
  }

  // --- Win9x GitHub Login Dialog ---
  const dlg = $('#gh-login-dialog');
  const dlgClose = $('#gh-dlg-close');
  const dlgStatus = $('#gh-dlg-status');
  const handleForm = $('#gh-handle-form');
  const tokenForm = $('#gh-token-form');
  const handleInput = $('#gh-handle-input');
  const tokenInput = $('#gh-token-input');
  const tokenSession = $('#gh-token-session');
  const tabs = document.querySelectorAll('.dlg-tabs [role="tab"]');

  function setDlgStatus(msg, isErr = false) {
    if (!dlgStatus) return;
    dlgStatus.textContent = msg || '';
    dlgStatus.classList.toggle('is-err', isErr);
  }

  function switchTab(mode) {
    tabs.forEach(t => t.setAttribute('aria-selected', String(t.dataset.mode === mode)));
    if (handleForm) handleForm.hidden = mode !== 'handle';
    if (tokenForm) tokenForm.hidden = mode !== 'token';
    setDlgStatus('');
  }

  tabs.forEach(t => t.addEventListener('click', () => switchTab(t.dataset.mode)));

  function openLoginDialog() {
    if (!dlg) return;
    setDlgStatus('');
    if (handleInput && ghUser?.login) handleInput.value = ghUser.login;
    if (tokenInput) tokenInput.value = '';
    if (typeof dlg.showModal === 'function') {
      if (!dlg.open) dlg.showModal();
    } else {
      dlg.setAttribute('open', '');
    }
    const activeInput = handleForm?.hidden ? tokenInput : handleInput;
    activeInput?.focus();
  }

  function closeLoginDialog() {
    if (!dlg) return;
    if (typeof dlg.close === 'function' && dlg.open) dlg.close();
    else dlg.removeAttribute('open');
  }

  dlgClose?.addEventListener('click', closeLoginDialog);
  dlg?.addEventListener('click', e => {
    if (e.target === dlg) closeLoginDialog();
  });

  handleForm?.addEventListener('submit', async e => {
    e.preventDefault();
    const raw = (handleInput?.value || '').trim().replace(/^@/, '');
    if (!/^[a-z\d](?:[a-z\d]|-(?=[a-z\d])){0,38}$/i.test(raw)) {
      setDlgStatus('请输入有效的 GitHub 用户名（字母、数字或连字符）。', true);
      return;
    }
    setDlgStatus(`正在向 GitHub 查询 @${raw} 的头像与资料……`);
    try {
      const u = await req(`https://api.github.com/users/${encodeURIComponent(raw)}`, { token: '' });
      const user = {
        login: String(u.login || raw),
        name: String(u.name || ''),
        avatar_url: safeAvatarUrl(u.avatar_url),
        mode: 'handle'
      };
      saveAuth(user, '', false);
      renderUserState();
      setDlgStatus(`已绑定 @${user.login}！`);
      setTimeout(closeLoginDialog, 260);
    } catch (err) {
      setDlgStatus(err.status === 404
        ? `未找到 GitHub 用户 @${raw}，请检查拼写。`
        : '暂时无法连接 GitHub 接口，请稍后重试。', true);
    }
  });

  tokenForm?.addEventListener('submit', async e => {
    e.preventDefault();
    const tok = (tokenInput?.value || '').trim();
    if (!tok) {
      setDlgStatus('请输入 GitHub Token。', true);
      return;
    }
    setDlgStatus('正在向 GitHub 验证令牌并读取头像……');
    try {
      const u = await req('https://api.github.com/user', { token: tok });
      const user = {
        login: String(u.login || ''),
        name: String(u.name || ''),
        avatar_url: safeAvatarUrl(u.avatar_url),
        mode: 'token'
      };
      if (!user.login) throw Error('invalid user');
      saveAuth(user, tok, !!tokenSession?.checked);
      renderUserState();
      setDlgStatus(`登录成功：@${user.login}（站内直发已启用）`);
      setTimeout(() => {
        closeLoginDialog();
        forum();
      }, 280);
    } catch (err) {
      setDlgStatus(err.status === 401
        ? 'Token 无效或已过期，请重新生成后再试。'
        : '验证失败，请检查网络或令牌权限。', true);
    }
  });

  // --- New Topic Composer ---
  const newTopicBox = $('#new-topic-box');
  const newTopicBtn = $('#bbs-new-btn');
  const newTopicForm = $('#new-topic-form');
  const newTopicCancel = $('#new-topic-cancel');
  const newTopicTitle = $('#new-topic-title');
  const newTopicBody = $('#new-topic-body');
  const newTopicStatus = $('#new-topic-status');

  function toggleNewTopic(forceOpen) {
    if (!newTopicBox) return;
    const next = forceOpen === undefined ? newTopicBox.hidden : !!forceOpen;
    newTopicBox.hidden = !next;
    if (next) {
      renderComposerAuthor($('#new-topic-author'));
      newTopicTitle?.focus();
    }
  }

  newTopicBtn?.addEventListener('click', () => toggleNewTopic());
  newTopicCancel?.addEventListener('click', () => {
    toggleNewTopic(false);
    if (newTopicStatus) newTopicStatus.textContent = '';
  });

  newTopicForm?.addEventListener('submit', async e => {
    e.preventDefault();
    const title = (newTopicTitle?.value || '').trim();
    const body = (newTopicBody?.value || '').trim();
    if (!title || !body) return;

    if (ghUser && ghUser.mode === 'token' && ghToken) {
      if (newTopicStatus) newTopicStatus.textContent = '正在提交到 GitHub Issues……';
      try {
        const created = await req(`${C.api}/issues`, {
          method: 'POST',
          body: { title, body }
        });
        if (newTopicTitle) newTopicTitle.value = '';
        if (newTopicBody) newTopicBody.value = '';
        if (newTopicStatus) newTopicStatus.textContent = `已发表 #${created.number}！`;
        setTimeout(() => {
          toggleNewTopic(false);
          if (newTopicStatus) newTopicStatus.textContent = '';
          forum();
        }, 500);
      } catch (err) {
        if (newTopicStatus) {
          newTopicStatus.textContent = err.status === 403 || err.status === 404
            ? 'Token 缺少写入 Issues 权限，请检查 public_repo / Issues 写权限。'
            : '提交失败，可点右上角「GitHub 发帖 ↗」手动发表。';
        }
      }
      return;
    }

    const url = `${C.repo}/issues/new?title=${encodeURIComponent(title)}&body=${encodeURIComponent(body)}`;
    window.open(url, '_blank', 'noopener');
    if (newTopicStatus) {
      newTopicStatus.textContent = '已在新标签页打开 GitHub 发帖页，提交后点上方「刷新」即可在讨论串看到你的头像与主题。';
    }
  });

  // --- Classic Forum Post Floor Builder ---
  function buildPostRow(post, { floor, opLogin, issueUrl }) {
    const u = post.user ? {
      login: String(post.user.login || 'GitHub 用户'),
      avatar_url: safeAvatarUrl(post.user.avatar_url)
    } : { login: 'GitHub 用户', avatar_url: '' };

    const isOwner = post.author_association === 'OWNER' || u.login.toLowerCase() === 'redrea1ity';
    const isOp = !!opLogin && u.login === opLogin;
    const isMe = !!ghUser && u.login.toLowerCase() === ghUser.login.toLowerCase();

    const row = el('article', '', `post-row${floor === 1 ? ' post-op' : ''}`);
    row.append(makeAuthorCol(u, { isOwner, isOp, isMe }));

    const main = el('div', '', 'post-main');
    const head = el('div', '', 'post-head');
    const metaLeft = el('span', `发表于 ${C.date(post.created_at)} UTC+8`);
    const metaRight = el('span', '');
    metaRight.append(el('span', `${floor}F`, 'post-floor'));
    if (issueUrl) {
      const ext = el('a', ' 原帖 ↗');
      ext.href = issueUrl;
      ext.target = '_blank';
      ext.rel = 'noopener';
      metaRight.append(ext);
    }
    head.append(metaLeft, metaRight);

    const bodyText = (post.body || '').trim();
    const p = el('p', bodyText ? bodyText.slice(0, 1800) + (bodyText.length > 1800 ? '\n……（正文较长，请点右上角查看完整原文）' : '') : '（没有正文内容）', 'post-text');

    main.append(head, p);
    row.append(main);
    return row;
  }

  function buildReplyComposer(topic, repliesBox, metaEl) {
    const row = el('div', '', 'post-row post-compose');
    const authorCol = el('div', '', 'post-author reply-compose-author');
    renderComposerAuthor(authorCol);

    const form = el('form', '', 'post-main');
    const head = el('div', '', 'post-head');
    head.append(el('strong', `回复 #${topic.number}`), el('span', 'REPLY', 'post-floor'));

    const field = el('label', '', 'win-field');
    field.append(el('span', '快速回复 / 补充反馈：'));
    const ta = document.createElement('textarea');
    ta.rows = 3;
    ta.placeholder = '输入你的回复或问题补充…';
    ta.required = true;
    field.append(ta);

    const actions = el('div', '', 'compose-actions');
    const submitBtn = el('button', (ghUser && ghUser.mode === 'token') ? '直接回复此帖' : '复制内容并去 GitHub 回复 ↗', 'win-btn win-btn-primary reply-submit-btn');
    submitBtn.type = 'submit';
    const fullLink = el('a', '在 GitHub 查看完整讨论 ↗', 'win-btn win-btn-sm');
    fullLink.href = `${C.repo}/issues/${topic.number}`;
    fullLink.target = '_blank';
    fullLink.rel = 'noopener';
    const status = el('span', '', 'compose-status');
    status.setAttribute('role', 'status');
    actions.append(submitBtn, fullLink, status);

    form.append(head, field, actions);
    row.append(authorCol, form);

    form.addEventListener('submit', async e => {
      e.preventDefault();
      const text = ta.value.trim();
      if (!text) return;

      if (ghUser && ghUser.mode === 'token' && ghToken) {
        status.textContent = '正在发送回复……';
        submitBtn.disabled = true;
        try {
          const created = await req(`${C.api}/issues/${topic.number}/comments`, {
            method: 'POST',
            body: { body: text }
          });
          ta.value = '';
          repliesBox.querySelector('.replies-note')?.remove();
          const nextFloor = repliesBox.querySelectorAll('.post-row').length + 2;
          repliesBox.append(buildPostRow(created, {
            floor: nextFloor,
            opLogin: topic.user?.login,
            issueUrl: created.html_url || `${C.repo}/issues/${topic.number}`
          }));
          topic.comments = (topic.comments || 0) + 1;
          if (metaEl) metaEl.textContent = `${topic.state === 'closed' ? '已结帖' : '讨论中'} · ${topic.comments} 回复`;
          status.textContent = `已回复至 ${nextFloor}F！`;
        } catch (err) {
          status.textContent = err.status === 403 || err.status === 404
            ? 'Token 缺少写权限，无法直接回复。'
            : '回复失败，请点旁边的链接前往 GitHub 提交。';
        } finally {
          submitBtn.disabled = false;
        }
        return;
      }

      try {
        await navigator.clipboard?.writeText(text);
        status.textContent = '已复制回复内容并打开 GitHub 页面，粘贴提交后点「刷新」即可同步显示你的头像与楼层。';
      } catch {
        status.textContent = '已打开 GitHub 页面，提交后点「刷新」即可同步显示你的头像与楼层。';
      }
      window.open(`${C.repo}/issues/${topic.number}#new_comment_field`, '_blank', 'noopener');
    });

    return row;
  }

  async function loadComments(topic, repliesBox) {
    if (repliesBox.dataset.loaded === '1') return;
    repliesBox.dataset.loaded = '1';
    if (!topic.comments) {
      repliesBox.replaceChildren(el('div', '目前还没有跟帖回复，欢迎抢沙发。', 'replies-note'));
      return;
    }
    repliesBox.replaceChildren(el('div', `正在加载 ${topic.comments} 条跟帖回复……`, 'replies-note'));
    try {
      const rows = await req(`${C.api}/issues/${topic.number}/comments?per_page=30`);
      if (!Array.isArray(rows) || !rows.length) {
        repliesBox.replaceChildren(el('div', '暂无公开跟帖回复。', 'replies-note'));
        return;
      }
      const frag = document.createDocumentFragment();
      rows.forEach((c, idx) => {
        frag.append(buildPostRow(c, {
          floor: idx + 2,
          opLogin: topic.user?.login,
          issueUrl: c.html_url || `${C.repo}/issues/${topic.number}`
        }));
      });
      if (topic.comments > rows.length) {
        const more = el('div', `仅展示前 ${rows.length} 条回复，共 ${topic.comments} 条。`, 'replies-note');
        const link = el('a', ' 查看全部回复 ↗');
        link.href = `${C.repo}/issues/${topic.number}`;
        link.target = '_blank';
        link.rel = 'noopener';
        more.append(link);
        frag.append(more);
      }
      repliesBox.replaceChildren(frag);
    } catch {
      repliesBox.dataset.loaded = '0';
      repliesBox.replaceChildren(el('div', '回复加载失败，可直接点击下方按钮前往 GitHub 查看完整讨论。', 'replies-note'));
    }
  }

  async function forum() {
    const state = $('#forum-state');
    const box = $('#threads');
    if (!state || !box) return;
    state.textContent = '正在接收渡口揭示板主题……';
    try {
      let topics = [];
      for (let page = 1; topics.length < 8; page++) {
        const rows = await req(`${C.api}/issues?state=all&sort=updated&direction=desc&per_page=30&page=${page}`);
        if (!Array.isArray(rows)) throw Error('issue list');
        topics.push(...rows.filter(x => !x.pull_request && Number.isSafeInteger(x.number)));
        if (rows.length < 30) break;
      }
      const frag = document.createDocumentFragment();
      for (const t of topics.slice(0, 8)) {
        const d = el('details', '');
        const s = el('summary', '');
        const num = el('span', `#${t.number}`, 'sum-num');

        const center = el('div', '', 'sum-center');
        center.append(el('strong', t.title, 'sum-title'));
        const by = el('div', '', 'sum-by');
        const opUser = t.user ? {
          login: String(t.user.login || 'GitHub 用户'),
          avatar_url: safeAvatarUrl(t.user.avatar_url)
        } : null;
        by.append(makeAvatar(opUser, 'avatar-sm'));
        by.append(el('span', `${opUser?.login || 'GitHub 用户'} · ${C.date(t.created_at)}`, 'sum-user'));
        center.append(by);

        const meta = el('small', `${t.state === 'closed' ? '已结帖' : '讨论中'} · ${t.comments} 回复`, 'sum-meta');
        s.append(num, center, meta);
        d.append(s);

        const inner = el('div', '', 'thread-body');
        inner.append(buildPostRow(t, {
          floor: 1,
          opLogin: t.user?.login,
          issueUrl: `${C.repo}/issues/${t.number}`
        }));
        const repliesBox = el('div', '', 'thread-replies');
        inner.append(repliesBox);
        inner.append(buildReplyComposer(t, repliesBox, meta));
        d.append(inner);

        d.addEventListener('toggle', () => {
          if (d.open) loadComments(t, repliesBox);
        });
        frag.append(d);
      }
      box.replaceChildren(frag);
      state.textContent = topics.length
        ? `已同步最近更新的 ${Math.min(topics.length, 8)} 个主题（点击展开查看楼层与头像回复）`
        : '目前还没有主题，可以点击上方「＋ 发新帖」发第一帖。';
    } catch {
      state.textContent = '暂时读不到帖子（可能是网络受限或接口限流）。可直接前往 GitHub Issues 查看与反馈。';
    }
  }

  $('#bbs-refresh')?.addEventListener('click', () => forum());

  loadAuth();
  renderUserState();

  if (!/^https?:$/.test(location.protocol)) return;

  async function releases() {
    const out = [];
    for (let page = 1; ; page++) {
      const part = await req(`${C.api}/releases?per_page=100&page=${page}`);
      if (!Array.isArray(part)) throw Error('invalid');
      out.push(...part);
      if (part.length < 100) return C.normalize(out);
    }
  }

  async function refresh() {
    const [rs, ms] = await Promise.allSettled([
      releases(),
      req('https://raw.githubusercontent.com/RedRea1ity/ProjectFerry/main/README.md', { raw: true, token: '' })
    ]);
    const rows = rs.status === 'fulfilled' ? rs.value : snapshot.rows;
    const notes = ms.status === 'fulfilled' ? C.readNotes(ms.value) : snapshot.notes;
    const labels = ms.status === 'fulfilled' ? C.readLabels(ms.value) : (snapshot.labels || {});
    display(rows, notes, labels);
    const statusEl = $('#catalog-status');
    if (statusEl) {
      statusEl.textContent =
        (rs.status === 'fulfilled' ? '已与 GitHub 发行目录同步' : `发行目录暂未连通，显示 ${snapshot.when} 随盘存档`) +
        (ms.status === 'fulfilled' ? '' : '；README 使用随盘摘录');
    }
  }

  refresh().catch(() => {
    const statusEl = $('#catalog-status');
    if (statusEl) statusEl.textContent = '暂未完成同步，显示随盘存档';
  });

  // --- Retro Win9x Visitor Counter (Vercount JSON API, zero third-party script) ---
  async function loadVisitCounter() {
    const uvEl = $('#visit-uv');
    const pvEl = $('#visit-pv');
    const box = $('#visit-counter');
    if (!uvEl || !pvEl) return;

    const CACHE_KEY = 'ferry-visit-cache-v1';
    const SEEN_KEY = 'ferry-uv-seen-v1';
    const fmt = n => String(Math.max(0, Math.floor(Number(n) || 0))).padStart(6, '0');
    const render = (uv, pv) => {
      uvEl.textContent = fmt(uv);
      pvEl.textContent = fmt(pv);
      if (box) box.title = `累计访客 ${Math.floor(uv).toLocaleString()} 人 · 总访问 ${Math.floor(pv).toLocaleString()} 次`;
    };

    try {
      const cached = JSON.parse(localStorage.getItem(CACHE_KEY) || 'null');
      if (cached && Number.isFinite(cached.uv) && Number.isFinite(cached.pv)) {
        render(cached.uv, cached.pv);
      }
    } catch {}

    if (/^(localhost|127\.0\.0\.1|\[::1\])$/i.test(location.hostname)) return;

    let isNewUv = true;
    try { isNewUv = !localStorage.getItem(SEEN_KEY); } catch {}

    const payload = JSON.stringify({
      url: `${location.origin}${location.pathname}`,
      isNewUv
    });
    const endpoints = [
      'https://cn.vercount.one/api/v2/log',
      'https://events.vercount.one/api/v2/log'
    ];

    for (const ep of endpoints) {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 8000);
      try {
        const res = await fetch(ep, {
          method: 'POST',
          credentials: 'omit',
          signal: ctrl.signal,
          headers: { 'Content-Type': 'application/json' },
          body: payload
        });
        if (!res.ok) continue;
        const json = await res.json();
        const data = json?.data || json;
        const uv = Number(data?.site_uv);
        const pv = Number(data?.site_pv);
        if (Number.isFinite(uv) && Number.isFinite(pv)) {
          const safeUv = Math.max(uv, pv > 0 ? 1 : 0);
          render(safeUv, pv);
          try {
            localStorage.setItem(SEEN_KEY, '1');
            localStorage.setItem(CACHE_KEY, JSON.stringify({ uv: safeUv, pv }));
          } catch {}
          return;
        }
      } catch {
        // try next endpoint
      } finally {
        clearTimeout(timer);
      }
    }
  }
  loadVisitCounter();

  if ('IntersectionObserver' in window) {
    const seen = new IntersectionObserver(entries => {
      if (entries.some(e => e.isIntersecting)) {
        seen.disconnect();
        forum();
      }
    }, { rootMargin: '480px' });
    const forumEl = $('#forum');
    if (forumEl) seen.observe(forumEl);
  } else {
    forum();
  }
})();
