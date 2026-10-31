// "Modify": change a brand or product graph from a typed request.
// Type what to add or change → the server looks for it on the brand's site → you see each proposed
// change with the page and quote it came from → tick the ones to keep (values can be edited) → saved.
// Used by the product and brand cards (index.html) and the graph page (graph.html).
(() => {
  const css = `
  .gm { margin-top: 10px; padding: 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); font-size: 13px; color: var(--ink); }
  .gm textarea, .gm input { width: 100%; padding: 6px 8px; border: 1px solid var(--line); border-radius: 6px; background: var(--bg); color: var(--ink); font: inherit; }
  .gm .hint { color: var(--muted); font-size: 12px; margin: 4px 0 8px; }
  .gm .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-top: 8px; }
  .gm button { width: auto; padding: 6px 12px; border-radius: 6px; border: 1px solid var(--line); background: var(--panel); color: var(--ink); font: inherit; font-weight: 600; cursor: pointer; }
  .gm button.primary { background: var(--accent); border-color: var(--accent); color: var(--accent-ink, var(--bg)); }
  .gm button:disabled { opacity: .5; cursor: default; }
  .gm .chg { display: grid; grid-template-columns: 20px 1fr; gap: 8px; padding: 10px 0; align-items: start; border-top: 1px solid var(--line); }
  .gm .chg input[type=checkbox] { width: auto; margin-top: 3px; }
  .gm .op { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .04em; padding: 1px 6px; border-radius: 4px; border: 1px solid currentColor; margin-right: 6px; }
  .gm .op.add { color: var(--ok, #1e7a3e); } .gm .op.update { color: var(--accent); } .gm .op.delete { color: var(--err, #c0392b); }
  .gm .new { color: var(--accent); font-size: 12px; font-weight: 600; }
  .gm .f { display: grid; grid-template-columns: 70px 1fr; gap: 6px; align-items: center; margin-top: 6px; }
  .gm .f span { color: var(--muted); font-size: 12px; }
  .gm .was { color: var(--muted); font-size: 12px; margin-top: 4px; }
  .gm blockquote { margin: 6px 0 0; padding: 4px 8px; border-left: 3px solid var(--line); color: var(--muted); font-size: 12px; }
  .gm .warn { color: var(--warn, #a15c00); font-size: 12px; margin-top: 4px; }
  .gm .err { color: var(--err, #c0392b); }
  .gm a { color: var(--accent); }
  .gm details { margin-top: 8px; color: var(--muted); font-size: 12px; }
  .gm details li { word-break: break-all; }
  .gm .web { color: var(--warn, #a15c00); font-size: 11px; font-weight: 700; margin-left: 6px; }
  .gm label.chk { display: flex; gap: 6px; align-items: center; color: var(--muted); font-size: 12px; margin-top: 6px; }
  .gm label.chk input { width: auto; margin: 0; }`;
  document.head.append(Object.assign(document.createElement('style'), { textContent: css }));

  const esc = (t) => String(t ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const short = (u) => String(u || '').replace(/^https?:\/\/(www\.)?/, '');
  const LONG = new Set(['text', 'description']);
  const OPS = { add: 'Add', update: 'Change', delete: 'Remove' };

  const KIND = { DesignToken: 'Design token', StyleNote: 'Style note', Preference: 'Pattern preference', Component: 'Brand section',
    Logo: 'Brand logo', BrandColor: 'Brand colour', ProductImage: 'Product photo' };
  const READONLY = { DesignToken: ['name'], StyleNote: ['topic'], Preference: ['pattern', 'count', 'of'], Component: ['pattern', 'slug', 'thumbnail'],
    Logo: ['file', 'from'], BrandColor: ['role'], ProductImage: ['file'] };

  function describe(c, topics) {
    if (c.type === 'Fact') {
      const topic = (c.fields || {}).topic || (c.current || {}).topic || '';
      const isNew = c.op === 'add' && !topics.some((t) => t.toLowerCase() === topic.toLowerCase());
      return `Fact under <b>${esc(topic)}</b>${isNew ? ' <span class="new">· new branch</span>' : ''}`;
    }
    if (KIND[c.type]) {
      const f = c.fields || {};
      return `<b>${KIND[c.type]}</b> · ${esc((f.name || f.topic || f.pattern || f.role || (c.type === 'Logo' ? 'replaces the current logo' : '')).replace(/_/g, ' '))}`;
    }
    if (c.op === 'add') return `<b>${esc(c.type)}</b>`;
    const cur = c.current || {};
    return `<b>${esc(c.type)}</b> · ${esc(cur.name || cur.text || cur.network || cur.kind || cur.role || c.node_id)}`;
  }

  function fieldInputs(c, i) {
    if (c.op === 'delete') return c.reason ? `<div class="was">${esc(c.reason)}</div>` : '';
    const fixed = READONLY[c.type] || [];
    const tile = (inner) => `<span style="display:inline-flex;align-items:center;justify-content:center;padding:10px;border:1px solid var(--line);border-radius:6px;background:#fff;margin:6px 6px 0 0">${inner}</span>`;
    const swatch = (hex) => `<span style="display:inline-block;width:36px;height:22px;border-radius:4px;border:1px solid var(--line);background:${esc(hex)};vertical-align:middle;margin-right:6px"></span>`;
    let pic = '';
    if (c.type === 'ProductImage' && c.fields && c.fields.file) pic = `<img src="/files/${esc(c.fields.file)}" alt="" style="display:block;max-width:100%;max-height:240px;margin:6px 0;border:1px solid var(--line);border-radius:4px;background:#fff">`;
    if (c.type === 'Component' && c.fields && c.fields.thumbnail) pic = `<img src="/files/${esc(c.fields.thumbnail)}" alt="" style="display:block;max-width:100%;max-height:220px;margin:6px 0;border:1px solid var(--line);border-radius:4px">`;
    if (c.type === 'Logo' && c.fields) {
      pic = `<div>${tile(`<img src="/files/${esc(c.fields.file)}" alt="logo from the emails" style="max-height:48px;max-width:220px">`)}${c.current && c.current.file
        ? `<span class="was">instead of</span> ${tile(`<img src="/files/${esc(c.current.file)}" alt="current logo" style="max-height:48px;max-width:220px">`)}` : ''}</div>`;
    }
    if (c.type === 'BrandColor' && c.fields) pic = `<div class="was">${swatch(c.fields.hex)}${c.current && c.current.hex ? `instead of ${swatch(c.current.hex)}${esc(c.current.hex)}` : ''}</div>`;
    return Object.entries(c.fields || {}).filter(([k]) => !fixed.includes(k)).map(([k, v]) => {
      const input = LONG.has(k) ? `<textarea data-i="${i}" data-k="${esc(k)}" rows="2">${esc(v)}</textarea>` : `<input data-i="${i}" data-k="${esc(k)}" value="${esc(v)}">`;
      const cur = c.current && c.current[k] !== undefined ? c.current[k] : undefined;
      const shown = c.type === 'Logo' || c.type === 'BrandColor'; // their preview already shows what they replace
      const was = c.op === 'update' && cur !== undefined && !shown ? `<div class="was">was: ${esc(cur)}${c.current.origin ? ` (from ${esc(c.current.origin)})` : ''}</div>` : '';
      return `<label class="f"><span>${esc(k.replace('_', ' '))}</span>${input}</label>${was}`;
    }).join('') + pic;
  }

  function evidence(c) {
    if (c.basis === 'request') return '<div class="was">From your request.</div>';
    const src = c.source_url ? ` — ${/^https?:/.test(c.source_url) ? `<a href="${esc(c.source_url)}" target="_blank" rel="noopener">${esc(short(c.source_url))}</a>` : esc(c.source_url)}` : '';
    // Design imports carry measurements ("18 buttons on the site…"), not quotes.
    const quote = !c.evidence ? (src ? `<div class="was">Source${src}</div>` : '')
      : c.origin && !/^“/.test(c.evidence) ? `<div class="was">${esc(c.evidence)}${src}</div>`
      : `<blockquote>${/^“/.test(c.evidence) ? esc(c.evidence) : `“${esc(c.evidence)}”`}${src}</blockquote>`;
    const warn = c.note ? `<div class="warn">${esc(c.note)}</div>`
      : (c.verified ? '' : '<div class="warn">⚠ This quote wasn’t found on the page, so it isn’t ticked. Check it before adding.</div>');
    return quote + warn + (c.from_web ? '<div class="was">From another website, not the brand’s own. Worth a second look.</div>' : '');
  }

  const isChecked = (c) => (c.checked !== undefined ? c.checked : c.verified);

  // The review screen: every proposed change with its source, ticked or not, editable; Save sends the ticked ones.
  function review(box, p, { base, onApplied = () => {}, onAgain, onWeb, onMore, savedText } = {}) {
    const msg = (t, err) => { const m = box.querySelector('.msg'); if (m) { m.textContent = t; m.className = `msg ${err ? 'err' : 'hint'}`; } };
    const changes = p.changes || [];
    const topics = p.topics || [];
    box.innerHTML = `<b>Found for “${esc(p.request)}”</b>
      <div class="hint">${esc(p.summary || '')}</div>
      ${p.not_found ? `<div class="warn">Not found: ${esc(p.not_found)}</div>` : ''}
      ${changes.length ? changes.map((c, i) => `<div class="chg"><input type="checkbox" data-pick="${i}" ${isChecked(c) ? 'checked' : ''} aria-label="Keep this change">
        <div><span class="op ${esc(c.op)}">${OPS[c.op] || esc(c.op)}</span>${describe(c, topics)}${c.from_web ? '<span class="web">OTHER SITE</span>' : ''}${fieldInputs(c, i)}${evidence(c)}</div></div>`).join('')
        : `<div class="warn" style="margin-top:8px">Nothing new to save.${onWeb ? ' Search the web too, name a page, or paste its link into the request.' : ''}</div>`}
      <details><summary>${p.pages.length} source${p.pages.length === 1 ? '' : 's'} read${p.web_pages && p.web_pages.length ? ` (${p.web_pages.length} from a web search)` : ''}</summary><ul>${p.pages.map((u) => `<li>${/^https?:/.test(u) ? `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(short(u))}</a>` : esc(u)}${(p.web_pages || []).includes(u) ? ' · web' : ''}</li>`).join('')}</ul></details>
      <div class="row">${changes.length ? '<button type="button" class="primary" data-act="apply"></button>' : ''}${onWeb ? '<button type="button" data-act="web">Search the web too</button>' : ''}${onAgain ? '<button type="button" data-act="again">Ask again</button>' : ''}<button type="button" data-act="close">Close</button><span class="msg"></span></div>`;
    const applyBtn = box.querySelector('[data-act=apply]');
    const count = () => {
      const n = box.querySelectorAll('[data-pick]:checked').length;
      if (applyBtn) { applyBtn.textContent = n ? `Save ${n} change${n > 1 ? 's' : ''}` : 'Tick the changes to keep'; applyBtn.disabled = !n; }
    };
    box.querySelectorAll('[data-pick]').forEach((cb) => { cb.onchange = count; });
    count();
    if (onAgain) box.querySelector('[data-act=again]').onclick = onAgain;
    if (onWeb) box.querySelector('[data-act=web]').onclick = onWeb;
    box.querySelector('[data-act=close]').onclick = () => box.remove();
    if (applyBtn) applyBtn.onclick = async () => {
      const picks = [...box.querySelectorAll('[data-pick]:checked')].map((cb) => {
        const i = Number(cb.dataset.pick);
        const fields = Object.fromEntries([...box.querySelectorAll(`[data-i="${i}"]`)].map((el) => [el.dataset.k, el.value]));
        return { index: i, fields };
      });
      applyBtn.disabled = true;
      msg('Saving…');
      const r = await fetch(`${base}/modify/${p.id}/apply`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ picks }) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) { applyBtn.disabled = false; return msg(j.detail || 'Could not save', true); }
      const { done = [], failed = [] } = j.applied || {};
      box.innerHTML = `<b>Saved ${done.length} change${done.length === 1 ? '' : 's'}</b> ${savedText || 'to the graph, marked as yours (a rebuild keeps them). Emails use them from the next run.'}
        ${failed.map((f) => `<div class="warn">Change ${f.index + 1} wasn’t saved: ${esc(f.error)}</div>`).join('')}
        <div class="row">${onMore ? '<button type="button" data-act="more">Modify something else</button>' : ''}<button type="button" data-act="close">Close</button></div>`;
      if (onMore) box.querySelector('[data-act=more]').onclick = onMore;
      box.querySelector('[data-act=close]').onclick = () => box.remove();
      onApplied(j);
    };
  }

  // Wait for a brand-worker job, showing its stage; resolves with the job's proposal.
  async function awaitProposal(jobId, onStage) {
    for (;;) {
      const s = await fetch(`/api/brand-jobs/${jobId}`).then((x) => x.json());
      if (s.state === 'done' && s.proposal) return s.proposal;
      if (s.state === 'error') throw new Error(s.error);
      onStage(`${s.stage} · ${Math.round(s.elapsed_s)}s`);
      await new Promise((res) => setTimeout(res, 1500));
    }
  }

  // Show a proposal that came from somewhere else (e.g. a design import) in its own panel.
  function show(host, proposal, opts = {}) {
    host.querySelectorAll(':scope > .gm').forEach((b) => b.remove());
    const box = Object.assign(document.createElement('div'), { className: 'gm' });
    host.append(box);
    review(box, proposal, opts);
    box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    return box;
  }

  function open(host, { base, backend = () => '', onApplied = () => {}, placeholder } = {}) {
    let box = host.querySelector(':scope > .gm');
    if (box) { box.remove(); return; } // the link toggles the panel
    box = Object.assign(document.createElement('div'), { className: 'gm' });
    host.append(box);
    const ask = (text = '', web = false) => {
      box.innerHTML = `<b>Modify this graph</b>
        <div class="hint">Say what to add, change or remove. It looks for it on the brand's website and shows you what it found before anything is saved.
          Things that fit no existing node start a new branch (e.g. “Sustainability”).</div>
        <textarea rows="3" class="req" placeholder="${esc(placeholder || 'e.g. add their sustainability facts (carbon footprint, materials) · add care instructions · remove the TikTok link · the accent color is #0A7B55')}">${esc(text)}</textarea>
        <label class="chk"><input type="checkbox" class="web" ${web ? 'checked' : ''}> Also search the web (results from other sites are marked; it also searches when you write “do a web search”)</label>
        <div class="row"><button type="button" class="primary" data-act="go">Look it up</button><button type="button" data-act="close">Cancel</button><span class="msg"></span></div>`;
      const ta = box.querySelector('.req');
      ta.focus();
      box.querySelector('[data-act=close]').onclick = () => box.remove();
      const go = () => lookUp(ta.value, box.querySelector('input.web').checked || null);
      box.querySelector('[data-act=go]').onclick = go;
      ta.onkeydown = (e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) go(); };
    };
    const msg = (t, err) => { const m = box.querySelector('.msg'); if (m) { m.textContent = t; m.className = `msg ${err ? 'err' : 'hint'}`; } };

    async function lookUp(text, web = null) {
      if (!text.trim()) return msg('Type what to add or change first.', true);
      box.querySelectorAll('button').forEach((b) => { b.disabled = true; });
      msg('Starting…');
      try {
        const r = await fetch(`${base}/modify`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ request: text, backend: backend(), web }) });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(j.detail || 'Could not start');
        const p = await awaitProposal(j.job_id, msg);
        review(box, p, {
          base, onApplied, onMore: () => ask(),
          onAgain: () => ask(p.request, p.searched_web),
          onWeb: p.searched_web ? null : () => { ask(p.request, true); lookUp(p.request, true); },
        });
      } catch (err) {
        box.querySelectorAll('button').forEach((b) => { b.disabled = false; });
        msg(err.message, true);
      }
    }
    ask();
    box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  window.GraphModify = { open, show, awaitProposal };
})();
