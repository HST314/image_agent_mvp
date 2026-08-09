/* 安全 Markdown 渲染（T32 验收：Markdown 清洗阻止 XSS）。
 *
 * 设计：parseMarkdown 产出与 DOM 无关的节点描述树（纯函数，可在 Node 中测试）；
 * mountMarkdown 只用 createElement/textContent 落 DOM，任何输入都不会作为 HTML 解析。
 * 链接仅放行 http(s)/mailto，其余协议（javascript:、data: 等）降级为纯文本。
 */

const SAFE_LINK = /^(https?:\/\/|mailto:)/i;

function parseInline(text) {
  const nodes = [];
  // 依次识别 `code`、**bold**、*italic*、[text](url)；其余原样为文本。
  const pattern = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\*[^*\n]+\*)|(\[[^\]\n]*\]\([^)\n]*\))/g;
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > last) nodes.push({ t: 'text', v: text.slice(last, match.index) });
    const token = match[0];
    if (token.startsWith('`')) {
      nodes.push({ t: 'code', v: token.slice(1, -1) });
    } else if (token.startsWith('**')) {
      nodes.push({ t: 'bold', v: token.slice(2, -2) });
    } else if (token.startsWith('*')) {
      nodes.push({ t: 'italic', v: token.slice(1, -1) });
    } else {
      const close = token.indexOf('](');
      const label = token.slice(1, close);
      const url = token.slice(close + 2, -1).trim();
      if (SAFE_LINK.test(url)) nodes.push({ t: 'link', v: label, href: url });
      else nodes.push({ t: 'text', v: token }); // 非安全协议：整体按纯文本展示
    }
    last = match.index + token.length;
  }
  if (last < text.length) nodes.push({ t: 'text', v: text.slice(last) });
  return nodes;
}

export function parseMarkdown(source = '') {
  const lines = String(source).replace(/\r\n?/g, '\n').split('\n');
  const blocks = [];
  let paragraph = [];
  let list = null;
  let code = null;

  const flushParagraph = () => {
    if (paragraph.length) {
      blocks.push({ t: 'p', children: parseInline(paragraph.join(' ')) });
      paragraph = [];
    }
  };
  const flushList = () => { if (list) { blocks.push(list); list = null; } };
  const flushCode = () => { if (code) { blocks.push({ t: 'pre', v: code.join('\n') }); code = null; } };

  for (const line of lines) {
    if (code !== null) {
      if (/^```/.test(line)) flushCode(); else code.push(line);
      continue;
    }
    if (/^```/.test(line)) { flushParagraph(); flushList(); code = []; continue; }
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      flushParagraph(); flushList();
      blocks.push({ t: 'h', level: Math.min(heading[1].length, 3), children: parseInline(heading[2].trim()) });
      continue;
    }
    const unordered = /^\s*[-*]\s+(.*)$/.exec(line);
    const ordered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (unordered || ordered) {
      flushParagraph();
      const kind = ordered ? 'ol' : 'ul';
      if (!list || list.t !== kind) { flushList(); list = { t: kind, items: [] }; }
      list.items.push(parseInline((unordered || ordered)[1].trim()));
      continue;
    }
    const quote = /^>\s?(.*)$/.exec(line);
    if (quote) {
      flushParagraph(); flushList();
      blocks.push({ t: 'quote', children: parseInline(quote[1].trim()) });
      continue;
    }
    if (line.trim() === '') { flushParagraph(); flushList(); continue; }
    flushList();
    paragraph.push(line.trim());
  }
  flushParagraph(); flushList(); flushCode();
  return blocks;
}

function mountInline(parent, nodes, doc) {
  for (const node of nodes) {
    if (node.t === 'text') parent.append(doc.createTextNode(node.v));
    else if (node.t === 'code') { const e = doc.createElement('code'); e.textContent = node.v; parent.append(e); }
    else if (node.t === 'bold') { const e = doc.createElement('strong'); e.textContent = node.v; parent.append(e); }
    else if (node.t === 'italic') { const e = doc.createElement('em'); e.textContent = node.v; parent.append(e); }
    else if (node.t === 'link') {
      const a = doc.createElement('a');
      a.textContent = node.v;
      a.setAttribute('href', node.href);
      a.setAttribute('rel', 'noopener noreferrer');
      a.setAttribute('target', '_blank');
      parent.append(a);
    }
  }
}

/** 将解析树挂载到容器。doc 可注入以便在非浏览器环境测试。 */
export function mountBlocks(container, blocks, doc = document) {
  container.textContent = '';
  for (const block of blocks) {
    if (block.t === 'h') {
      const e = doc.createElement(`h${block.level}`);
      mountInline(e, block.children, doc);
      container.append(e);
    } else if (block.t === 'p') {
      const e = doc.createElement('p');
      mountInline(e, block.children, doc);
      container.append(e);
    } else if (block.t === 'pre') {
      const pre = doc.createElement('pre');
      const codeEl = doc.createElement('code');
      codeEl.textContent = block.v;
      pre.append(codeEl);
      container.append(pre);
    } else if (block.t === 'ul' || block.t === 'ol') {
      const list = doc.createElement(block.t);
      for (const item of block.items) {
        const li = doc.createElement('li');
        mountInline(li, item, doc);
        list.append(li);
      }
      container.append(list);
    } else if (block.t === 'quote') {
      const quote = doc.createElement('blockquote');
      mountInline(quote, block.children, doc);
      container.append(quote);
    }
  }
  return container;
}

export function renderMarkdownInto(container, source) {
  container.classList.add('md-preview');
  return mountBlocks(container, parseMarkdown(source));
}
