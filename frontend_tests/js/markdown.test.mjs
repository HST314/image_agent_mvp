/* T32 验收：Markdown 清洗阻止 XSS。 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseMarkdown } from '../../frontend/static/js/markdown.js';

function collectText(blocks) {
  const parts = [];
  const walk = (node) => {
    if (node.v) parts.push(node.v);
    if (node.children) node.children.forEach(walk);
    if (node.items) node.items.forEach((item) => item.forEach(walk));
  };
  blocks.forEach(walk);
  return parts.join(' ');
}

test('script 标签与事件处理器按纯文本处理，不产生元素节点', () => {
  const blocks = parseMarkdown('<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>');
  const text = collectText(blocks);
  assert.ok(text.includes('<script>alert(1)</script>'));
  // 解析树只允许固定标签集合，HTML 永远不会被解释
  const tags = new Set(blocks.map((b) => b.t));
  for (const tag of tags) assert.ok(['p', 'h', 'pre', 'ul', 'ol', 'quote'].includes(tag));
});

test('javascript: 与 data: 链接降级为纯文本', () => {
  const blocks = parseMarkdown('[点我](javascript:alert(1)) 和 [x](data:text/html;base64,abc)');
  const links = [];
  const walk = (n) => { if (n.t === 'link') links.push(n); if (n.children) n.children.forEach(walk); };
  blocks.forEach(walk);
  assert.equal(links.length, 0);
  assert.ok(collectText(blocks).includes('javascript:alert(1)'));
});

test('安全链接保留并带 href', () => {
  const blocks = parseMarkdown('[官网](https://example.com/page)');
  const links = [];
  const walk = (n) => { if (n.t === 'link') links.push(n); if (n.children) n.children.forEach(walk); };
  blocks.forEach(walk);
  assert.equal(links.length, 1);
  assert.equal(links[0].href, 'https://example.com/page');
  assert.equal(links[0].v, '官网');
});

test('标题/加粗/斜体/行内代码/列表/代码块结构正确', () => {
  const md = '# 标题\n\n**加粗** 和 *斜体* 和 `code`\n\n- 甲\n- 乙\n\n```\nraw <b>\n```\n\n1. 一\n2. 二';
  const blocks = parseMarkdown(md);
  assert.equal(blocks[0].t, 'h');
  assert.equal(blocks[0].level, 1);
  const p = blocks.find((b) => b.t === 'p');
  assert.deepEqual(p.children.map((c) => c.t), ['bold', 'text', 'italic', 'text', 'code']);
  const ul = blocks.find((b) => b.t === 'ul');
  assert.equal(ul.items.length, 2);
  const pre = blocks.find((b) => b.t === 'pre');
  assert.equal(pre.v, 'raw <b>');
  const ol = blocks.find((b) => b.t === 'ol');
  assert.equal(ol.items.length, 2);
});

test('空输入与不闭合标记不抛异常', () => {
  assert.deepEqual(parseMarkdown(''), []);
  assert.doesNotThrow(() => parseMarkdown('**未闭合\n`code\n```未闭合代码块'));
});
