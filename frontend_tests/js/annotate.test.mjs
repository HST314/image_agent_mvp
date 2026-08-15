/* T34 验收核心：显示坐标与原图坐标一致（object-fit:contain 留白映射）。 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  containRect, toNormalized, clamp01, isInsideContent, thinStroke, createMarksModel,
  normalizeAnnotationDraft, MAX_STROKE_POINTS,
} from '../../frontend/static/js/annotate.js';

test('containRect：横图在方形容器上下留白', () => {
  const r = containRect(2000, 1000, 500, 500);
  assert.deepEqual(r, { x: 0, y: 125, w: 500, h: 250 });
});

test('containRect：竖图左右留白', () => {
  const r = containRect(1000, 2000, 500, 500);
  assert.deepEqual(r, { x: 125, y: 0, w: 250, h: 500 });
});

test('containRect：超长图（如 4:1 横幅）内容不被裁切', () => {
  const r = containRect(4000, 1000, 800, 600);
  assert.equal(r.w, 800);
  assert.equal(r.h, 200);
  assert.equal(r.y, 200); // 上下各 200 留白，完整可见
});

test('toNormalized：内容盒角点与中心精确映射到原图归一化坐标', () => {
  const content = containRect(2000, 1000, 500, 500); // {x:0,y:125,w:500,h:250}
  assert.deepEqual(toNormalized({ x: 0, y: 125 }, content), { x: 0, y: 0 });
  assert.deepEqual(toNormalized({ x: 500, y: 375 }, content), { x: 1, y: 1 });
  assert.deepEqual(toNormalized({ x: 250, y: 250 }, content), { x: 0.5, y: 0.5 });
});

test('toNormalized：落在留白内的点被裁剪到 [0,1] 边界', () => {
  const content = containRect(2000, 1000, 500, 500);
  assert.deepEqual(toNormalized({ x: 250, y: 10 }, content), { x: 0.5, y: 0 });
  assert.deepEqual(toNormalized({ x: 250, y: 490 }, content), { x: 0.5, y: 1 });
  assert.equal(clamp01(1.4), 1);
  assert.equal(clamp01(-0.2), 0);
});

test('isInsideContent 正确识别留白', () => {
  const content = containRect(2000, 1000, 500, 500);
  assert.equal(isInsideContent({ x: 250, y: 250 }, content), true);
  assert.equal(isInsideContent({ x: 250, y: 100 }, content), false);
});

test('thinStroke：抽稀并限制在后端点约上', () => {
  const dense = Array.from({ length: 9000 }, (_, i) => ({ x: i / 9000, y: (i % 7) / 1000 }));
  const thinned = thinStroke(dense);
  assert.ok(thinned.length <= MAX_STROKE_POINTS);
  assert.deepEqual(thinned[0], dense[0]);
  assert.ok(thinned.length >= 2);
});

test('MarksModel：矩形规范化（反向拖拽）与序列化契约', () => {
  const model = createMarksModel();
  const mark = model.addRectangle({ x0: 0.8, y0: 0.7, x1: 0.2, y1: 0.3, color: '#00ff00', width: 4 });
  assert.deepEqual(mark, { kind: 'rectangle', x: 0.2, y: 0.3, w: 0.6000000000000001, h: 0.39999999999999997, color: '#00ff00', width: 4 });
  const serialized = model.serialize();
  assert.equal(serialized[0].kind, 'rectangle');
  assert.ok(serialized[0].x >= 0 && serialized[0].x + serialized[0].w <= 1);
});

test('MarksModel：零面积矩形被拒绝', () => {
  const model = createMarksModel();
  assert.equal(model.addRectangle({ x0: 0.5, y0: 0.5, x1: 0.5, y1: 0.9, color: '#f00', width: 3 }), null);
  assert.equal(model.isEmpty(), true);
});

test('MarksModel：画笔序列化为 [x,y] 数组；撤销/清空语义正确', () => {
  const model = createMarksModel();
  model.addStroke([{ x: 0.1, y: 0.1 }, { x: 0.2, y: 0.2 }, { x: 0.3, y: 0.1 }], '#ff0000', 6);
  const [stroke] = model.serialize();
  assert.equal(stroke.kind, 'stroke');
  assert.deepEqual(stroke.points[0], [0.1, 0.1]);
  assert.equal(stroke.points.length, 3);
  model.undo();
  assert.equal(model.isEmpty(), true);
  model.addStroke([{ x: 0.1, y: 0.1 }, { x: 0.5, y: 0.5 }], '#ff0000', 6);
  model.clear();
  assert.equal(model.isEmpty(), true);
});

test('标注草稿恢复矩形、笔迹、工具、颜色、粗细与说明文字', () => {
  const restored = normalizeAnnotationDraft({
    marks: [
      { kind: 'rectangle', x: .1, y: .2, w: .3, h: .4, color: '#AABBCC', width: 9 },
      { kind: 'stroke', points: [[.2, .3], [.5, .7]], color: '#00ff00', width: 17 },
    ],
    tool: 'stroke', color: '#123456', width: 17, prompt: '保留主体，调暖框选区域',
  });
  assert.equal(restored.tool, 'stroke');
  assert.equal(restored.color, '#123456');
  assert.equal(restored.width, 17);
  assert.equal(restored.prompt, '保留主体，调暖框选区域');
  const model = createMarksModel(restored.marks);
  assert.deepEqual(model.serialize(), restored.marks);
});

test('损坏的标注草稿被裁剪和过滤，不污染运行态', () => {
  const restored = normalizeAnnotationDraft({
    marks: [
      { kind: 'rectangle', x: -1, y: .2, w: 3, h: .4, color: 'bad', width: 999 },
      { kind: 'stroke', points: [[.1, .1], ['bad', .2]] },
      { kind: 'unknown' },
    ],
    tool: 'eraser', color: 'red', width: -5,
  });
  assert.equal(restored.tool, 'rectangle');
  assert.equal(restored.color, '#ff0000');
  assert.equal(restored.width, 1);
  assert.deepEqual(restored.marks, [
    { kind: 'rectangle', x: 0, y: .2, w: 1, h: .4000000000000001, color: '#ff0000', width: 64 },
  ]);
});
