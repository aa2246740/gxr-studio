// 执行真实信息面板，在异步拍摄信息返回和电脑编辑更新后核对拍摄配方仍正确。
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { installDom, tick } from './mini_dom.mjs';

installDom();
const { infoPanel } = await import(pathToFileURL(`${process.argv[2]}/info_panel.js`).href);
const photos = JSON.parse(readFileSync(process.argv[3], 'utf8'));
const results = [];
for (const photo of photos) {
  const panel = infoPanel({ photo, ed: { adj: {} } });
  document.body.append(panel);
  const recipe = () => panel.querySelectorAll('section')[0].querySelector('.section-body').textContent;
  const before = recipe();
  await tick();
  panel.sync();
  results.push({
    recipe: recipe(),
    shot: panel.querySelectorAll('section')[2].textContent,
    stable: recipe() === before,
    htmlElements: panel.querySelectorAll('b').length,
  });
  panel.remove();
}
console.log(JSON.stringify(results));
