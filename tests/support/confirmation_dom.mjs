// 最小 DOM 契约用于组件测试；浏览器里的原生回车另由界面检查核对。
import assert from 'node:assert/strict';

class TestNode extends EventTarget {
  constructor(tag = '', text = '') {
    super(); this.tagName = tag.toUpperCase(); this.text = text; this.children = []; this.parentNode = null;
    this.style = {}; this.dataset = {}; this.className = ''; this.disabled = false;
  }
  get textContent() { return this.text + this.children.map((c) => c.textContent).join(''); }
  get isConnected() { return this === document.body || !!this.parentNode?.isConnected; }
  append(...children) { for (const child of children) { child.parentNode = this; this.children.push(child); } }
  setAttribute(key, value) { this[key] = value; }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((c) => c !== this); this.parentNode = null; }
  focus() { document.activeElement = this; }
  click() { if (!this.disabled) this.dispatchEvent(new Event('click', { bubbles: true, cancelable: true })); }
  dispatchEvent(event) {
    const result = super.dispatchEvent(event);
    if (event.bubbles && !event.cancelBubble && this.parentNode) this.parentNode.dispatchEvent(event);
    return result;
  }
}
globalThis.Node = globalThis.HTMLElement = TestNode;
const modalRoot = new TestNode('div');
const toastRoot = new TestNode('div');
globalThis.document = {
  body: new TestNode('body'), activeElement: null,
  createElement: (tag) => new TestNode(tag), createTextNode: (text) => new TestNode('', text),
  getElementById: (id) => id === 'modal-root' ? modalRoot : toastRoot,
};
document.body.append(modalRoot, toastRoot);
const find = (root, test) => test(root) ? root : root.children.map((c) => find(c, test)).find(Boolean);
const button = (text) => find(modalRoot, (el) => el.tagName === 'BUTTON' && el.textContent === text);
const enter = (target) => {
  const event = new Event('keydown', { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'key', { value: 'Enter' });
  target.dispatchEvent(event);
  if (!event.defaultPrevented) target.click();
  return event.defaultPrevented;
};
const { confirmAction } = await import(process.argv[2]);
let deleted = 0;
confirmAction('删除「测试」？', '删除', async () => { deleted++; });
const focused = document.activeElement.textContent;
assert.equal(focused, '取消');
assert.equal(enter(document.activeElement), true);
assert.equal(deleted, 0);
assert.equal(modalRoot.children.length, 0);
confirmAction('删除「测试」？', '删除', async () => { deleted++; });
const confirm = button('删除');
confirm.focus();
assert.equal(enter(confirm), true);
assert.equal(deleted, 0);
assert.equal(modalRoot.children.length, 1);
confirm.click();
await Promise.resolve();
assert.equal(deleted, 1);
assert.equal(modalRoot.children.length, 0);
let replaced = 0;
confirmAction('替换文件？', '替换', async () => { replaced++; });
const overwriteLabel = button('替换').textContent;
assert.equal(document.activeElement.textContent, '取消');
button('替换').focus();
assert.equal(enter(button('替换')), true);
assert.equal(replaced, 0);
console.log(JSON.stringify({ default_focus: focused, enter_cancelled: true, enter_on_confirm_blocked: true,
  confirmed_by_click: deleted, overwrite_label: overwriteLabel, overwrite_by_enter: replaced }));
