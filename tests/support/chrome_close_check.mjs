// closeChrome 的四种情况，用假的 Chrome（node 子进程）跑：Browser.close 退、SIGTERM 慢退、不理 SIGTERM 才 SIGKILL、已经退了。
// 用法：node chrome_close_check.mjs，输出一行 JSON，断言在 tests/test_chrome_close.py。
import { spawn } from 'node:child_process';
import { closeChrome } from './debug_port.mjs';

// 子进程装好信号处理后才在 stdout 写 ready，父进程等到它再动手，负载高时也不会抢在前面。
const fake = (body) => spawn(process.execPath, ['-e', `${body} process.stdout.write('ready\\n');`], { stdio: ['pipe', 'pipe', 'ignore'] });
const ready = (child) => new Promise((resolve) => { child.stdout.once('data', resolve); });
const timed = async (fn) => { const t = Date.now(); const how = await fn(); return { how, ms: Date.now() - t }; };
const keepAlive = 'setInterval(() => {}, 1000);';

const out = {};

// 1. Browser.close：假的 WebSocket 把消息写进子进程的 stdin，子进程读到就退出。
{
  const child = fake(`${keepAlive} process.stdin.on('data', (d) => { if (String(d).includes('Browser.close')) process.exit(0); });`);
  await ready(child);
  const sent = [];
  const ws = { readyState: 1, send: (m) => { sent.push(JSON.parse(m).method); child.stdin.write(`${m}\n`); } };
  out.browserClose = { ...(await timed(() => closeChrome(child, () => ws, { graceMs: 5000 }))), sent, signal: child.signalCode };
}

// 2. WebSocket 没有（还没连上）：SIGTERM 后 2 秒才退，旧代码 1.2 秒就会强杀；现在要等到它自己退。
{
  const child = fake(`${keepAlive} process.on('SIGTERM', () => setTimeout(() => process.exit(0), 2000));`);
  await ready(child);
  out.slowTerm = { ...(await timed(() => closeChrome(child, () => { throw new ReferenceError('ws 未初始化'); }, { graceMs: 6000 }))), signal: child.signalCode, code: child.exitCode };
}

// 3. 不理 SIGTERM：等够 graceMs 之后才 SIGKILL。
{
  const child = fake(`${keepAlive} process.on('SIGTERM', () => {});`);
  await ready(child);
  out.ignoresTerm = { ...(await timed(() => closeChrome(child, () => undefined, { graceMs: 800 }))), signal: child.signalCode };
}

// 4. 已经退了：什么都不发。
{
  const child = fake('process.exit(0);');
  await new Promise((resolve) => child.once('exit', resolve));
  out.alreadyExited = await closeChrome(child, () => undefined, { graceMs: 100 });
}

console.log(JSON.stringify(out));
