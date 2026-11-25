// 编辑器分包的入口。主包只 import() 这一个文件：编辑器、导出（选项、就绪条、共享）、⋯ 菜单、复制 / 粘贴面板、
// 关机图和显卡引擎都由这里静态带进来。打开编辑器以后，里面的面板彼此都是静态导入，不再有懒加载。

import { Engine } from '../../../frontend/src/index';
import { neutralBake, NEUTRAL_BAKE_KEY } from '../lib/neutral';

export { EditorView } from './editor';
export { ExportFlow, syncExportDock } from './export_ui';

/** 显卡引擎（WebGPU）。建不起来就是 null，编辑器只显示相机 JPG；出错交给 `report`。 */
export async function createEngine(shell: HTMLElement, report: (err: unknown) => void): Promise<Engine | null> {
  if (!navigator.gpu) return null;
  let engine: Engine | null;
  try {
    engine = await Engine.create();
  } catch (err) {
    report(err);
    return null;
  }
  if (!engine) return null;
  const bake = neutralBake();
  engine.addBake(NEUTRAL_BAKE_KEY, bake.meta, bake.data);
  engine.onFrame = () => {
    shell.dataset.frame = '1';
  };
  return engine;
}
