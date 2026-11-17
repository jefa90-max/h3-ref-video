"use strict";
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const directory = path.join(__dirname, '../workbench/static');

function harness(language) {
  const nodes = [], ids = new Map();
  const document = { activeElement: null, addEventListener() {}, querySelectorAll(selector) {
    return nodes.filter(node => selector.split(',').some(part => node.className.split(' ').includes(part.trim().slice(1))));
  }};
  function element(tag, className = '', textContent = '') {
    const node = { tag, className, textContent, children: [], dataset: {}, attributes: {}, events: {}, value: '', hidden: false,
      disabled: false, open: false, selectionStart: 0, selectionEnd: 0, paused: true, currentTime: 4,
      append(...children) { for (const child of children) { child.remove(); child.parent = this; this.children.push(child); } },
      replaceChildren(...children) { for (const child of this.children) child.parent = null; this.children = []; this.append(...children); },
      remove() { if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; },
      insertBefore(child, reference) { child.remove(); child.parent = this; const index = this.children.indexOf(reference); this.children.splice(index < 0 ? this.children.length : index, 0, child); },
      addEventListener(name, callback) { this.events[name] = callback; },
      setAttribute(name, value) { this.attributes[name] = value; }, removeAttribute(name) { delete this.attributes[name]; },
      focus() { document.activeElement = this; }, pause() { this.paused = true; }, scrollIntoView() {},
      closest(selector) { return selector.split(',').map(s => s.trim()).includes(tag) ? this : this.parent?.closest(selector); },
      setCustomValidity() {}, reportValidity() {},
      async fire(name, extras = {}) { return this.events[name]?.({ target: this, preventDefault() {}, ...extras }); },
    };
    nodes.push(node); return node;
  }
  const select = id => { if (!ids.has(id)) ids.set(id, element('div')); return ids.get(id); };
  select('results-page').dataset.jobId = 'job-1';
  const state = { language, messages: JSON.parse(fs.readFileSync(path.join(directory, 'messages', language + '.json'), 'utf8')) };
  const posted = [], errors = [];
  let context;
  const job = { id: 'job-1', kind: 'film', demo: false, status: 'succeeded', operation: 'pipeline', operation_id: 'initial',
    created_at: 1, artifacts: [], storyboard: [], progress: {}, review: { kind: 'script', complete: true },
    assembly: { ready: true, version: 'assembly' }, can_assemble: true,
    segments: Array.from({ length: 8 }, (_, i) => ({ number: i + 1, start: i * 5, end: (i + 1) * 5, seconds: 5, shots: 1,
      url: `/seg${i + 1}.mp4?v=1`, version: 'clip-v1', prompt_version: 'prompt-v2', prompt: 'original source', prompt_format: 'source',
      source_prompt: 'original source', h3_prompt: 'actual H3', automatic_source_prompt: 'automatic new source',
      editable: true, can_regenerate: true, reference_images: [{ number: 1, kind: 'cast', name: 'Actor', available: true }], reference_history_known: false,
    })),
  };
  context = vm.createContext({ state, $: select, document, element,
    t: (key, values = {}) => { assert.ok(Object.hasOwn(state.messages, key), `missing locale ${language}:${key}`); return state.messages[key].replace(/\{(\w+)\}/g, (_, name) => String(values[name] ?? '')); },
    running: job => ['running','queued'].includes(job?.status), dateLabel: () => '',
    renderConfig() {}, renderPage: null, loadLanguage: () => new Promise(() => {}),
    showError: error => errors.push(error), clearNotices() {}, showMessage() {},
    request: async () => state.selectedJob,
    post: async (url, body) => {
      posted.push({ url, body: { ...body } });
      const response = structuredClone(state.selectedJob);
      response.status = 'queued'; response.operation = 'segment'; response.operation_id = 'submitted';
      const number = Number(url.split('/').at(-2));
      const segment = response.segments.find(s => s.number === number);
      if (segment) Object.assign(segment, { attempt_revision: 'revision-1', prompt: body.prompt, prompt_format: body.prompt_format, editable: false, task_pending: true });
      return response;
    },
    setTimeout: () => 1, clearTimeout() {},
    Plyr: function() { this.destroy = () => { this.destroyed = true; }; this.pause = () => {}; },
  });
  for (const name of ['pagination.js','video-segments.js','results.js']) vm.runInContext(fs.readFileSync(path.join(directory,name),'utf8'),context);
  state.selectedJob = job; state.resultTab = 'segments';
  function render() { context.renderVideoSegments(state.selectedJob); }
  function card(number = 2) { return state.segmentCards.get(number); }
  async function expand(number = 2) { const c = card(number); assert.ok(c.details, 'prompt details must exist'); c.details.open = true; await c.details.fire('toggle'); return c; }
  async function edit(text, number = 2) { const c = await expand(number); c.editor.value = text; await c.editor.fire('input'); return c; }
  render();
  return { state, job, context, nodes, select, document, render, card, expand, edit, posted, errors, element };
}

for (const language of ['zh-CN','en']) {
  test(`video prompt editor posts current draft (${language})`, async () => {
    const h = harness(language), c = await h.edit('按当前机位，接收者跪地侧身托袋');
    await c.regenerate.fire('click');
    assert.equal(h.posted[0].url,'/api/jobs/job-1/segments/2/regenerate');
    assert.deepEqual(h.posted[0].body,{ prompt: '按当前机位，接收者跪地侧身托袋', prompt_format: 'source', prompt_version: 'prompt-v2', version: 'clip-v1' });
    assert.equal(h.state.segmentSubmissions.get(2).revision,'revision-1');
  });
  test(`polling keeps draft, player, focus, selection and composition (${language})`, async () => {
    const h = harness(language), c = await h.edit('draft');
    const player = c.player, video = c.video, editor = c.editor;
    editor.focus(); editor.selectionStart = 1; editor.selectionEnd = 3;
    await editor.fire('compositionstart'); assert.equal(editor.disabled,false,'IME textarea must remain enabled'); editor.value = 'draft输入中'; await editor.fire('input',{ isComposing:true });
    h.job.segments[1].h3_prompt = 'only saved H3 changed'; h.render();
    assert.strictEqual(h.card().player,player); assert.strictEqual(h.card().video,video);
    assert.strictEqual(h.card().editor,editor); assert.strictEqual(h.document.activeElement,editor);
    assert.equal(editor.value,'draft输入中'); assert.equal(editor.selectionStart,1); assert.equal(editor.selectionEnd,3);
    assert.equal(c.details.open,true); assert.equal(c.regenerate.disabled,true);
    await editor.fire('compositionend'); assert.equal(c.regenerate.disabled,false);
  });
  test(`drafts survive pagination, tabs, language and format switches (${language})`, async () => {
    const h = harness(language); let c = await h.edit('source draft');
    c.format.value = 'h3'; await c.format.fire('change'); c.editor.value = 'H3 draft'; await c.editor.fire('input');
    h.state.segmentPage = 2; h.render(); h.state.segmentPage = 1; h.render(); c = h.card();
    assert.equal(c.editor.value,'H3 draft'); assert.equal(c.format.value,'h3');
    h.context.selectResultTab('storyboard'); h.render(); h.context.selectResultTab('segments'); h.render();
    const other = language === 'en' ? 'zh-CN' : 'en'; h.state.language = other;
    h.state.messages = JSON.parse(fs.readFileSync(path.join(directory,'messages',other+'.json'),'utf8')); h.render();
    assert.equal(c.editor.value,'H3 draft');
    c.format.value = 'source'; await c.format.fire('change'); assert.equal(c.editor.value,'source draft');
    await c.regenerate.fire('click'); assert.equal(h.posted[0].body.prompt_format,'source'); assert.equal(h.posted[0].body.prompt,'source draft');
  });
  test(`success clears only matching submitted format and revision (${language})`, async () => {
    const h = harness(language); let c = await h.edit('source submitted');
    c.format.value='h3'; await c.format.fire('change'); c.editor.value='other H3 draft'; await c.editor.fire('input');
    c.format.value='source'; await c.format.fire('change');
    await h.edit('other segment',1); await c.regenerate.fire('click');
    h.state.selectedJob.status='failed'; h.render(); assert.equal(h.state.segmentDrafts.get(2).drafts.source.text,'source submitted');
    h.state.selectedJob.status='succeeded'; const segment=h.state.selectedJob.segments[1];
    Object.assign(segment,{version:'clip-v2',prompt_version:'prompt-v3',prompt:'source submitted',source_prompt:'source submitted',editable:true,can_regenerate:true,task_pending:false,applied_revision:'unrelated-revision'});
    h.render(); assert.ok(h.state.segmentDrafts.get(2).drafts.source);
    segment.applied_revision='revision-1'; h.render();
    assert.equal(h.state.segmentDrafts.get(2).drafts.source,undefined);
    assert.equal(h.state.segmentDrafts.get(2).drafts.h3.text,'other H3 draft');
    assert.equal(h.state.segmentDrafts.get(1).drafts.source.text,'other segment');
  });
  test(`a newer local edit survives prior submission success (${language})`, async () => {
    const h=harness(language); const c=await h.edit('submitted'); await c.regenerate.fire('click');
    c.editor.value='edited again'; await c.editor.fire('input'); h.state.selectedJob.status='succeeded';
    Object.assign(h.state.selectedJob.segments[1],{version:'new',source_prompt:'submitted',applied_revision:'revision-1'}); h.render();
    assert.equal(h.state.segmentDrafts.get(2).drafts.source.text,'edited again');
  });
  test(`stale draft stays blocked until explicit rebase (${language})`, async () => {
    const h=harness(language), c=await h.edit('stale draft');
    h.job.segments[1].prompt_version='new server prompt';h.job.segments[1].version='new server clip'; h.render();
    assert.equal(c.regenerate.disabled,true); assert.equal(c.rebase.hidden,false);
    assert.equal(h.state.segmentDrafts.get(2).drafts.source.basePromptVersion,'prompt-v2');
    await c.regenerate.fire('click'); assert.equal(h.posted.length,0);
    await c.rebase.fire('click'); await c.regenerate.fire('click');
    assert.equal(h.posted[0].body.prompt_version,'new server prompt'); assert.equal(h.posted[0].body.version,'new server clip');
    assert.equal(h.posted[0].body.prompt,'stale draft');
  });
  test(`locks, empty input and terminal first failure use same controls (${language})`, async () => {
    const h=harness(language), c=await h.edit(''); assert.equal(c.regenerate.disabled,true);
    await h.edit('corrected'); h.job.segments[1].submission_unknown=true; h.render();
    assert.equal(c.regenerate.disabled,true); assert.equal(c.editor.disabled,true); assert.ok(c.video);
    h.job.segments[1].submission_unknown=false;h.job.status='running';h.render();assert.equal(c.regenerate.disabled,true);
    h.job.status='failed';Object.assign(h.job.segments[1],{url:null,version:null,prompt_version:'failure-v',terminal_failed:true,error:'remote failed',source_prompt:'',h3_prompt:''});
    h.state.segmentDrafts.delete(2);h.render();assert.equal(c.regenerate.disabled,false);assert.equal(c.editor.disabled,false);
    assert.equal(c.video,null);await c.regenerate.fire('click');assert.equal(h.posted[0].body.version,null);
  });
  test(`labels, costs, reference order and failures come from both locales (${language})`, async () => {
    const h=harness(language), c=await h.edit('changed source');const m=h.state.messages;
    assert.equal(c.promptLabel.textContent,m.segmentPromptLabelSource);
    assert.equal(c.draftNote.textContent,m.segmentDraftUnapplied);
    assert.equal(c.cost.textContent,m.segmentSourceCost);
    assert.equal(c.format.children[0].textContent,m.segmentFormatSource);
    assert.equal(c.format.children[1].textContent,m.segmentFormatH3);
    assert.ok(c.editor.attributes['aria-describedby']);assert.equal(c.editor.id,c.promptLabel.htmlFor);
    h.job.segments[1].source_changed=true;h.render();assert.equal(c.sourceWarning.textContent,m.segmentSourceChanged);
    assert.equal(c.sourceWarning.hidden,false);
  });
  test(`focused original keeps displayed version as first edit baseline (${language})`, async () => {
    const h=harness(language),c=await h.expand();c.editor.focus();
    h.job.segments[1].prompt_version='server-new';h.job.segments[1].prompt='new server text';h.render();
    c.editor.value='edit based on displayed original';await c.editor.fire('input');
    assert.equal(h.state.segmentDrafts.get(2).drafts.source.basePromptVersion,'prompt-v2');
    assert.equal(c.regenerate.disabled,true);
  });
  test(`switching from H3 to source marks automatic text as a new draft (${language})`, async () => {
    const h=harness(language);Object.assign(h.job.segments[1],{prompt:'actual H3',prompt_format:'h3',source_prompt:''});h.render();
    const c=await h.expand();assert.equal(c.format.value,'h3');c.format.value='source';await c.format.fire('change');
    assert.equal(c.editor.value,'automatic new source');assert.equal(c.draftNote.hidden,false);
  });
  test(`job changes clear drafts, expansions and submissions (${language})`, async () => {
    const h=harness(language);await h.edit('old job draft');h.state.segmentSubmissions.set(2,{revision:'old'});
    h.job.id='job-2';h.render();assert.equal(h.state.segmentDrafts.size,0);assert.equal(h.state.expandedSegments.size,0);assert.equal(h.state.segmentSubmissions.size,0);
  });
}

for (const language of ['zh-CN','en']) {
  test(`reference-driven rewrite cost and pipeline resume use server contract (${language})`, async () => {
    const h=harness(language),c=await h.expand();
    h.job.segments[1].source_rewrite_required=true;
    h.job.can_resume_pipeline=true;h.job.can_resume_video=true;
    h.context.renderJob(h.job);
    assert.equal(c.cost.textContent,h.state.messages.segmentSourceCost);
    assert.equal(h.select('resume-job').hidden,false);
    h.job.segments[1].source_rewrite_required=false;h.job.segments[1].source_changed=true;h.context.renderJob(h.job);
    assert.equal(c.cost.textContent,h.state.messages.segmentVideoCost);
  });

  test(`manual single-segment and batch H3 rewrite buttons convert without generating video and switch to h3 view (${language})`, async () => {
    const h = harness(language);
    h.job.can_rewrite_prompts = true;
    h.job.missing_h3_count = 2;
    h.context.initializeVideoSegments();
    h.context.renderJob(h.job);

    assert.equal(h.select('rewrite-missing-prompts').disabled, false);
    assert.equal(h.select('rewrite-all-prompts').disabled, false);

    // Batch missing rewrite posts scope: "missing"
    await h.select('rewrite-missing-prompts').fire('click');
    assert.equal(h.posted.at(-1).url, '/api/jobs/job-1/segments/rewrite');
    assert.deepEqual(h.posted.at(-1).body, { scope: 'missing' });

    // Restore idle state, edit segment 2 source prompt and click single-segment rewrite button
    h.state.selectedJob.status = 'succeeded';
    h.state.selectedJob.can_rewrite_prompts = true;
    h.render();
    const c = await h.edit('女王轻推婚书望向唐僧');
    assert.ok(c.rewriteH3, 'single-segment rewrite button must exist');
    assert.equal(c.rewriteH3.disabled, false);
    await c.rewriteH3.fire('click');
    assert.equal(h.posted.at(-1).url, '/api/jobs/job-1/segments/2/rewrite');
    assert.deepEqual(h.posted.at(-1).body, {
      prompt: '女王轻推婚书望向唐僧',
      prompt_format: 'source',
      prompt_version: 'prompt-v2',
    });

    // Simulate worker completion with newly rewritten H3 prompt -> editor auto-switches to h3 view!
    const seg2 = h.state.selectedJob.segments[1];
    h.state.selectedJob.status = 'succeeded';
    Object.assign(seg2, {
      editable: true,
      can_rewrite: true,
      can_regenerate: true,
      task_pending: false,
      prompt_version: 'prompt-v-rewritten',
      prompt: '女王轻推婚书望向唐僧',
      source_prompt: '女王轻推婚书望向唐僧',
      h3_prompt: 'subject_definitions:\n<Subject 1> Queen from <Picture 1>.\n\nsummary:\nHall.',
      source_rewrite_required: false,
      applied_revision: 'revision-rw-1',
    });
    h.render();
    assert.equal(c.format.value, 'h3');
    assert.match(c.editor.value, /subject_definitions:/);
    assert.equal(c.draftNote.hidden, true);
  });

  test(`single-segment rewrite trims trailing whitespace and does not switch to stale H3 on failed request (${language})`, async () => {
    const h = harness(language);
    h.job.can_rewrite_prompts = true;
    h.context.initializeVideoSegments();
    h.context.renderJob(h.job);

    // 1. Failed POST (returns null from runJobAction) must NOT leave segmentRewrites armed to switch to old h3_prompt
    const origPost = h.context.post;
    h.context.post = async () => { throw new Error('missing LLM key'); };
    const c = await h.expand(2);
    assert.equal(c.format.value, 'source');
    await c.rewriteH3.fire('click');
    h.render();
    assert.equal(c.format.value, 'source', 'Failed rewrite POST must not switch editor to existing old H3');

    // 2. Trailing newline/spaces in source draft still reconcile cleanly with trimmed backend source_prompt
    h.context.post = origPost;
    await h.edit('带末尾换行的中文词  \n', 2);
    await c.rewriteH3.fire('click');
    assert.equal(h.posted.at(-1).body.prompt, '带末尾换行的中文词');
    const seg2 = h.state.selectedJob.segments[1];
    h.state.selectedJob.status = 'succeeded';
    Object.assign(seg2, {
      editable: true,
      can_rewrite: true,
      can_regenerate: true,
      task_pending: false,
      prompt_version: 'prompt-v-trim',
      prompt: '带末尾换行的中文词',
      source_prompt: '带末尾换行的中文词',
      h3_prompt: 'subject_definitions:\n<Subject 1> Trimmed.\n\nsummary:\nTrimmed.',
      source_rewrite_required: false,
    });
    h.render();
    assert.equal(c.format.value, 'h3');
    assert.equal(c.conflict.hidden, true, 'Trimmed server source_prompt must not trigger false conflict');

    // 3. Clicking Regenerate immediately after manual H3 conversion (without editing H3) submits source format to preserve paired Chinese prompt
    await c.regenerate.fire('click');
    assert.equal(h.posted.at(-1).url, '/api/jobs/job-1/segments/2/regenerate');
    assert.equal(h.posted.at(-1).body.prompt_format, 'source');
    assert.equal(h.posted.at(-1).body.prompt, '带末尾换行的中文词');
  });

  test(`failed single-segment rewrite does not trigger false draft conflict, and demo toolbar uses offline preview copy (${language})`, async () => {
    const h = harness(language);
    h.job.demo = true;
    h.job.can_rewrite_prompts = true;
    h.job.missing_h3_count = 1;
    h.context.initializeVideoSegments();
    h.context.renderJob(h.job);

    // Demo mode toolbar hint must reflect offline demo preview (not paid text model)
    assert.equal(h.select('segments-rewrite-hint').textContent, h.state.messages.segmentsRewriteDemoHint);

    // Editing source draft -> submitting rewrite (which bumps server prompt_version) -> worker fails:
    // must NOT mark user's own source draft as conflicted!
    h.job.demo = false;
    h.context.renderJob(h.job);
    const c = await h.edit('待转化的新中文词', 2);
    const origPost = h.context.post;
    h.context.post = async (url, body) => {
      h.posted.push({ url, body });
      const next = structuredClone(h.state.selectedJob);
      next.status = 'queued';
      next.operation = 'rewrite_segment';
      next.segments[1].prompt_version = 'prompt-v-pending-rw';
      return next;
    };
    await c.rewriteH3.fire('click');
    h.context.post = origPost;

    // Now worker fails with prompt_version still 'prompt-v-pending-rw'
    h.state.selectedJob.status = 'failed';
    h.state.selectedJob.segments[1].error = '401 bad key';
    h.render();
    assert.equal(c.conflict.hidden, true, 'Failed rewrite must not trigger false self-conflict on user draft');
    assert.equal(c.rewriteH3.disabled, false, 'Retry rewrite button must stay enabled after fixing key');
  });

  test(`retrying a server-persisted unapplied H3 edit after reload keeps custom H3 (${language})`, async () => {
    const h = harness(language);
    Object.assign(h.job.segments[1], {
      prompt: 'subject_definitions:\nCustom H3.',
      prompt_format: 'h3',
      source_prompt: 'original source',
      h3_prompt: 'actual H3',
      source_rewrite_required: false,
      error: 'failed',
    });
    h.render();
    const c = await h.expand(2);
    await c.regenerate.fire('click');
    assert.equal(h.posted.at(-1).body.prompt_format, 'h3');
    assert.equal(h.posted.at(-1).body.prompt, 'subject_definitions:\nCustom H3.');
  });

  test(`rewriteH3 from h3 view preserves persisted unapplied source edit on disk and failed rewrite clears pending state (${language})`, async () => {
    const h = harness(language);
    h.job.demo = false;
    Object.assign(h.job.segments[1], {
      prompt: 'UNAPPLIED_NEW_SOURCE',
      prompt_format: 'source',
      source_prompt: 'OLD_ACTIVE_SOURCE',
      h3_prompt: 'subject_definitions:\n<Subject 1> from <Picture 1>.\n\nsummary:\nScene.\n\nretention_analysis:\n<Subject 1>: ok.\n\ndetailed_description:\n[Shot 1] Action.\n\noverall_soundscape:\nQuiet.\n\nnon_diegetic_music:\nN/A',
      source_rewrite_required: true,
      error: 'previous attempt failed',
    });
    h.render();
    const c = await h.expand(2);
    c.format.value = 'h3';
    await c.format.fire('change');

    const origPost = h.context.post;
    h.context.post = async (url, body) => {
      h.posted.push({ url, body });
      const next = structuredClone(h.state.selectedJob);
      next.status = 'queued';
      next.operation = 'rewrite_segment';
      next.segments[1].attempt_revision = 'rw-rev-fail';
      return next;
    };
    await c.rewriteH3.fire('click');
    h.context.post = origPost;
    assert.equal(h.posted.at(-1).body.prompt, 'UNAPPLIED_NEW_SOURCE', 'rewriteH3 from h3 view must submit persisted unapplied source edit, not stale source_prompt');

    // Now the rewrite fails; user types a local H3 draft; later an unrelated job action succeeds
    // with the old applied_revision -> must NOT delete local H3 draft!
    h.state.selectedJob.status = 'failed';
    h.render();
    c.editor.value = 'LOCAL_UNSAVED_H3_DRAFT';
    await c.editor.fire('input');
    h.state.selectedJob.status = 'succeeded';
    h.state.selectedJob.operation = 'assemble';
    h.state.selectedJob.segments[1].source_prompt = 'UNAPPLIED_NEW_SOURCE';
    h.state.selectedJob.segments[1].source_rewrite_required = false;
    h.state.selectedJob.segments[1].applied_revision = 'older-clip-rev';
    h.render();
    assert.equal(h.state.segmentDrafts.get(2).drafts.h3?.text, 'LOCAL_UNSAVED_H3_DRAFT', 'Failed rewrite must clear pending state and never delete newer local H3 drafts');
  });
}
