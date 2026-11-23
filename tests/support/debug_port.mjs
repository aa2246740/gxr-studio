// Chrome 的调试端口。并行分片会同时开多个无头 Chrome，固定端口段会撞车。
// --remote-debugging-port=0 让 Chrome 自己挑空闲端口，并把端口写进用户目录的 DevToolsActivePort。
import { readFileSync } from 'node:fs';

export function readDebugPort(profile) {
  try {
    const port = readFileSync(`${profile}/DevToolsActivePort`, 'utf8').split('\n')[0].trim();
    return /^[0-9]+$/.test(port) ? port : '';
  } catch {
    return '';
  }
}

// 让 Chrome 正常退出：先发 DevTools 的 Browser.close，不退再 SIGTERM，两步都等够 graceMs，最后才 SIGKILL。
// 以前 SIGTERM 后只等 1.2 秒就 SIGKILL。macOS 上的 Google Chrome 启动时会把应用复制到系统临时目录
// （code_sign_clone），被强杀时副本留在内置盘上（2026-11 积了 432 份）。
// getWs 是取 WebSocket 的函数：驱动的 `let ws` 可能还没初始化，直接读会抛错。返回 Chrome 是怎么退的。
export async function closeChrome(chrome, getWs, { graceMs = 10000 } = {}) {
  const gone = () => chrome.exitCode !== null || chrome.signalCode !== null;
  if (gone()) return 'exited';
  const exited = new Promise((resolve) => { chrome.once('exit', () => resolve(true)); });
  const wait = (ms) => Promise.race([
    exited,
    new Promise((resolve) => { setTimeout(() => resolve(false), ms).unref(); }),
  ]);
  let ws;
  try { ws = getWs?.(); } catch { ws = undefined; }
  try {
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify({ id: 2147483647, method: 'Browser.close' }));
      if (await wait(graceMs)) return 'closed';
    }
  } catch { /* 连接已经断了，下面用信号 */ }
  if (gone()) return 'closed';
  chrome.kill('SIGTERM');
  if (await wait(graceMs)) return 'terminated';
  chrome.kill('SIGKILL');
  await wait(5000);
  return 'killed';
}
