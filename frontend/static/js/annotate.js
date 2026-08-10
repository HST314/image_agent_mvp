/* 圈画微调前端（T34）：矩形、自由画笔、颜色/粗细、撤销/清空、自由文本。
 *
 * 坐标契约（对齐 agent_core.annotation）：所有标注以"原图归一化坐标"提交
 * （x/y/w/h ∈ 0..1，相对原图宽高）。显示层为 object-fit:contain，因此指针坐标
 * 必须先映射到图片内容盒（去除留白），再归一化——保证显示坐标与原图坐标一致。
 * 提交前用离屏画布按原图分辨率合成指导图预览，与后端 PIL 合成逐笔对应。
 */

import { el, toast } from './dom.js';
import { assetUrl, assetIdOf, submitAnnotation } from './api.js';

export const MAX_STROKE_POINTS = 5000;
const MIN_POINT_DIST = 0.004; // 归一化坐标下的最小采样间距

/* ---------- 纯函数：几何与标注模型（Node 可测） ---------- */

/** object-fit:contain 的内容盒：图片在容器中的实际显示区域。 */
export function containRect(naturalW, naturalH, boxW, boxH) {
  if (!naturalW || !naturalH || !boxW || !boxH) return { x: 0, y: 0, w: 0, h: 0 };
  const scale = Math.min(boxW / naturalW, boxH / naturalH);
  const w = naturalW * scale;
  const h = naturalH * scale;
  return { x: (boxW - w) / 2, y: (boxH - h) / 2, w, h };
}

/** 显示坐标（相对容器）→ 原图归一化坐标；越界裁剪到 [0,1]。 */
export function toNormalized(point, content) {
  if (!content.w || !content.h) return null;
  const x = (point.x - content.x) / content.w;
  const y = (point.y - content.y) / content.h;
  return { x: clamp01(x), y: clamp01(y) };
}

export function clamp01(v) { return Math.min(1, Math.max(0, v)); }

export function isInsideContent(point, content) {
  return point.x >= content.x && point.x <= content.x + content.w
    && point.y >= content.y && point.y <= content.y + content.h;
}

/** 画笔抽稀：去除过密点并限制最大点数（后端上限 5000）。 */
export function thinStroke(points, minDist = MIN_POINT_DIST, maxPoints = MAX_STROKE_POINTS) {
  if (points.length <= 2) return points.slice(0, maxPoints);
  const out = [points[0]];
  for (let i = 1; i < points.length - 1; i += 1) {
    const prev = out[out.length - 1];
    const dx = points[i].x - prev.x;
    const dy = points[i].y - prev.y;
    if (Math.hypot(dx, dy) >= minDist) out.push(points[i]);
  }
  out.push(points[points.length - 1]);
  if (out.length > maxPoints) {
    const stride = out.length / maxPoints;
    const capped = [];
    for (let i = 0; i < maxPoints; i += 1) capped.push(out[Math.floor(i * stride)]);
    return capped;
  }
  return out;
}

export function createMarksModel() {
  const marks = [];
  return {
    list: () => marks.slice(),
    isEmpty: () => marks.length === 0,
    addRectangle(rect) {
      const x = clamp01(Math.min(rect.x0, rect.x1));
      const y = clamp01(Math.min(rect.y0, rect.y1));
      const x2 = clamp01(Math.max(rect.x0, rect.x1));
      const y2 = clamp01(Math.max(rect.y0, rect.y1));
      if (x2 - x <= 0 || y2 - y <= 0) return null;
      const mark = { kind: 'rectangle', x, y, w: x2 - x, h: y2 - y, color: rect.color, width: rect.width };
      marks.push(mark);
      return mark;
    },
    addStroke(points, color, width) {
      const thinned = thinStroke(points);
      if (thinned.length < 2) return null;
      const mark = { kind: 'stroke', points: thinned.map((p) => [p.x, p.y]), color, width };
      marks.push(mark);
      return mark;
    },
    undo: () => marks.pop() || null,
    clear: () => { marks.length = 0; },
    /** 序列化为后端 AnnotationRequest.marks 契约。 */
    serialize() {
      return marks.map((m) => (m.kind === 'rectangle'
        ? { kind: 'rectangle', x: m.x, y: m.y, w: m.w, h: m.h, color: m.color, width: m.width }
        : { kind: 'stroke', points: m.points.map(([x, y]) => [x, y]), color: m.color, width: m.width }));
    },
  };
}

/* ---------- DOM：编辑器 ---------- */

function drawMarks(ctx, marks, w, h) {
  for (const mark of marks) {
    ctx.strokeStyle = mark.color;
    ctx.lineWidth = mark.width;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    if (mark.kind === 'rectangle') {
      ctx.strokeRect(mark.x * w, mark.y * h, mark.w * w, mark.h * h);
    } else {
      ctx.beginPath();
      mark.points.forEach(([x, y], i) => { if (i === 0) ctx.moveTo(x * w, y * h); else ctx.lineTo(x * w, y * h); });
      ctx.stroke();
    }
  }
}

export function createAnnotator(container, { projectId, asset, history = [], onSubmitted, onBusy }) {
  const model = createMarksModel();
  let tool = 'rectangle';
  let color = '#ff0000';
  let width = 6;
  let drawing = null; // {kind, start, points}

  const stage = el('div', { class: 'annotate-stage' });
  const img = el('img', { alt: '待微调图像', draggable: 'false' });
  const url = assetUrl(projectId, asset);
  if (!url) { container.append(el('p', { text: '当前资产没有可标注的图片。' })); return null; }
  img.src = url;
  if (/^https?:/.test(url)) img.setAttribute('referrerpolicy', 'no-referrer');
  const canvas = el('canvas', { 'aria-label': '圈画标注画布：支持矩形与自由画笔' });
  stage.append(img, canvas);

  const ctx = canvas.getContext('2d');

  function layout() {
    const box = stage.getBoundingClientRect();
    const content = containRect(img.naturalWidth, img.naturalHeight, box.width, box.height);
    const dpr = window.devicePixelRatio || 1;
    canvas.style.left = `${content.x}px`;
    canvas.style.top = `${content.y}px`;
    canvas.style.width = `${content.w}px`;
    canvas.style.height = `${content.h}px`;
    canvas.width = Math.max(1, Math.round(content.w * dpr));
    canvas.height = Math.max(1, Math.round(content.h * dpr));
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    redraw();
  }

  function contentBox() {
    const box = stage.getBoundingClientRect();
    return containRect(img.naturalWidth, img.naturalHeight, box.width, box.height);
  }

  function redraw(previewStroke = null) {
    const box = canvas.getBoundingClientRect();
    ctx.clearRect(0, 0, box.width, box.height);
    drawMarks(ctx, model.list(), box.width, box.height);
    if (previewStroke) drawMarks(ctx, [previewStroke], box.width, box.height);
  }

  function pointFromEvent(event) {
    const rect = canvas.getBoundingClientRect();
    const stageRect = stage.getBoundingClientRect();
    return {
      x: event.clientX - stageRect.left,
      y: event.clientY - stageRect.top,
      onImage: isInsideContent({ x: event.clientX - stageRect.left, y: event.clientY - stageRect.top }, contentBox()),
      rect,
    };
  }

  canvas.addEventListener('pointerdown', (event) => {
    const p = pointFromEvent(event);
    if (!p.onImage) return;
    canvas.setPointerCapture(event.pointerId);
    const norm = toNormalized({ x: p.x, y: p.y }, contentBox());
    drawing = tool === 'rectangle' ? { kind: 'rectangle', start: norm, current: norm } : { kind: 'stroke', points: [norm] };
  });
  canvas.addEventListener('pointermove', (event) => {
    if (!drawing) return;
    const p = pointFromEvent(event);
    const norm = toNormalized({ x: p.x, y: p.y }, contentBox());
    if (drawing.kind === 'rectangle') {
      drawing.current = norm;
      redraw({ kind: 'rectangle', x: Math.min(drawing.start.x, norm.x), y: Math.min(drawing.start.y, norm.y), w: Math.abs(norm.x - drawing.start.x), h: Math.abs(norm.y - drawing.start.y), color, width });
    } else {
      drawing.points.push(norm);
      const preview = { kind: 'stroke', points: drawing.points.map((q) => [q.x, q.y]), color, width };
      redraw(preview);
    }
  });
  canvas.addEventListener('pointerup', () => {
    if (!drawing) return;
    if (drawing.kind === 'rectangle') {
      model.addRectangle({ x0: drawing.start.x, y0: drawing.start.y, x1: drawing.current.x, y1: drawing.current.y, color, width });
    } else {
      model.addStroke(drawing.points, color, width);
    }
    drawing = null;
    redraw();
    syncToolState();
  });
  canvas.addEventListener('pointercancel', () => { drawing = null; redraw(); });

  /* ---- 工具栏 ---- */
  const rectBtn = el('button', { type: 'button', class: 'tool-btn', text: '矩形框', 'aria-pressed': 'true' });
  const brushBtn = el('button', { type: 'button', class: 'tool-btn', text: '自由画笔', 'aria-pressed': 'false' });
  rectBtn.addEventListener('click', () => { tool = 'rectangle'; syncToolState(); });
  brushBtn.addEventListener('click', () => { tool = 'stroke'; syncToolState(); });
  const colorInput = el('input', { type: 'color', class: 'input', value: color, 'aria-label': '标注颜色' });
  colorInput.addEventListener('input', () => { color = colorInput.value; });
  const widthInput = el('input', { type: 'range', class: 'input', min: '1', max: '64', value: String(width), 'aria-label': '笔触粗细' });
  const widthValue = el('small', { text: `${width}px` });
  widthInput.addEventListener('input', () => { width = Number(widthInput.value); widthValue.textContent = `${width}px`; });
  const undoBtn = el('button', { type: 'button', class: 'btn btn--secondary', text: '撤销', disabled: 'disabled' });
  undoBtn.addEventListener('click', () => { model.undo(); redraw(); syncToolState(); });
  const clearBtn = el('button', { type: 'button', class: 'btn btn--secondary', text: '清空', disabled: 'disabled' });
  clearBtn.addEventListener('click', () => { model.clear(); redraw(); syncToolState(); });

  function syncToolState() {
    rectBtn.setAttribute('aria-pressed', String(tool === 'rectangle'));
    brushBtn.setAttribute('aria-pressed', String(tool === 'stroke'));
    const empty = model.isEmpty();
    if (empty) { undoBtn.setAttribute('disabled', 'disabled'); clearBtn.setAttribute('disabled', 'disabled'); }
    else { undoBtn.removeAttribute('disabled'); clearBtn.removeAttribute('disabled'); }
  }

  const promptInput = el('textarea', { class: 'input', id: 'annotate-prompt', 'aria-describedby': 'annotate-prompt-help', placeholder: '例如：把框选区域的颜色调暖，保持构图不变' });
  const promptError = el('div', { class: 'field-error', role: 'alert' });

  /* ---- 提交前指导图预览（验收：显示坐标与原图坐标一致） ---- */
  async function buildGuidePreview() {
    const guide = document.createElement('canvas');
    guide.width = img.naturalWidth;
    guide.height = img.naturalHeight;
    const gctx = guide.getContext('2d');
    gctx.drawImage(img, 0, 0, guide.width, guide.height);
    drawMarks(gctx, model.serialize(), guide.width, guide.height);
    return guide.toDataURL('image/png');
  }

  async function confirmAndSubmit() {
    promptError.textContent = '';
    const marks = model.serialize();
    if (!marks.length) { promptError.textContent = '请先在图上圈画至少一处标注。'; return; }
    const prompt = promptInput.value.trim();
    if (!prompt) { promptError.textContent = '请填写微调说明。'; promptInput.focus(); return; }
    const previewUrl = await buildGuidePreview();
    const dialog = el('dialog', { class: 'dialog', 'aria-label': '指导图预览确认' });
    dialog.append(
      el('div', { class: 'dialog__head' }, [el('h2', { text: '确认提交微调' })]),
      el('div', { class: 'dialog__body' }, [
        el('p', { text: '以下为将与原图合成的指导图预览（坐标按原图计算）：' }),
        el('img', { src: previewUrl, alt: '指导图预览', style: 'max-width:100%;border-radius:10px' }),
      ]),
    );
    const foot = el('div', { class: 'dialog__foot' });
    const cancel = el('button', { type: 'button', class: 'btn btn--secondary', text: '返回修改' });
    const ok = el('button', { type: 'button', class: 'btn btn--primary', text: '确认提交' });
    foot.append(cancel, ok);
    dialog.append(foot);
    cancel.addEventListener('click', () => dialog.close());
    ok.addEventListener('click', async () => {
      dialog.close();
      onBusy?.(true);
      try {
        const artifactId = assetIdOf(asset);
        await submitAnnotation(projectId, { artifact_id: artifactId, marks, prompt });
        toast('微调已提交，等待重新质检。');
        onSubmitted?.();
      } catch (error) {
        toast(error.message, 'error');
      } finally {
        onBusy?.(false);
      }
    });
    dialog.addEventListener('close', () => dialog.remove());
    document.body.append(dialog);
    dialog.showModal();
  }

  const submitBtn = el('button', { type: 'button', class: 'btn btn--primary', text: '预览并提交微调' });
  submitBtn.addEventListener('click', confirmAndSubmit);

  const tools = el('div', { class: 'annotate-tools' }, [
    el('div', { class: 'tool-row' }, [el('span', { text: '工具' }), el('div', { class: 'tool-group' }, [rectBtn, brushBtn])]),
    el('div', { class: 'tool-row' }, [el('span', { text: '颜色' }), colorInput]),
    el('div', { class: 'tool-row' }, [el('span', { text: '粗细' }), widthInput, widthValue]),
    el('div', { class: 'tool-group' }, [undoBtn, clearBtn]),
  ]);

  const promptField = el('div', { class: 'field' }, [
    el('label', { text: '微调说明', for: 'annotate-prompt' }),
    promptInput,
    el('small', { id: 'annotate-prompt-help', text: '说明将连同指导图一起交给编辑模型；原图不会被覆盖。' }),
    promptError,
  ]);

  /* ---- 多轮历史回看 ---- */
  const rounds = history.filter((e) => e.type === 'human_annotation_rework');
  let historyBlock = null;
  if (rounds.length || asset) {
    historyBlock = el('div', { class: 'annotate-history' });
    rounds.forEach((round, i) => {
      const row = el('div', { class: 'annotate-round' });
      const guideUrl = assetUrl(projectId, round.guide_asset);
      if (guideUrl) row.append(el('img', { src: guideUrl, alt: `第 ${i + 1} 轮指导图`, loading: 'lazy' }));
      row.append(el('p', { text: `第 ${i + 1} 轮 · ${round.prompt || '无说明'}` }));
      historyBlock.append(row);
    });
  }

  const layoutGrid = el('div', { class: 'annotate-layout' }, [stage, tools]);
  container.append(layoutGrid, promptField, submitBtn);
  if (historyBlock) container.append(el('h3', { text: '微调历史' }), historyBlock);

  img.addEventListener('load', layout);
  if (typeof ResizeObserver !== 'undefined') new ResizeObserver(layout).observe(stage);
  layout();

  return { model, layout };
}
