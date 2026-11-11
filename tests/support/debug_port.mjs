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
