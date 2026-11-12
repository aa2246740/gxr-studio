// 开发壳，不是正式界面。选一张 DNG，WASM 读布局，已有的显卡引擎画中性预览，拖动滑块只改 uniform。

import { Engine, type FrameInfo, type RawLayout, type Scene } from '../../../frontend/src/index';
import { describeDng } from '../core/api';
import { NEUTRAL_BAKE_KEY, neutralBake } from '../dev/neutral_bake';
import { formatStats, measureInterval, watchFrames, type PerfStats } from './perf';

export interface ShellReport {
  userAgent: string;
  webgpu: boolean;
  layout: RawLayout | null;
  reason: string | null;
  scene: Scene | null;
  error: string | null;
  frame: FrameInfo | null;
  readback: { w: number; h: number; min: number; max: number } | null;
  perf: PerfStats | null;
  pageErrors: string[];
}

interface Adjust {
  exposure: number;
  contrast: number;
  saturation: number;
}

function el<K extends keyof HTMLElementTagNameMap>(tag: K, className: string, text?: string): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function slider(label: string, min: string, max: string, step: string, value: string): { row: HTMLElement; input: HTMLInputElement } {
  const row = el('label', 'row');
  row.append(el('span', 'name', label));
  const input = document.createElement('input');
  input.type = 'range';
  input.min = min;
  input.max = max;
  input.step = step;
  input.value = value;
  row.append(input);
  return { row, input };
}

function spread(data: Uint8Array): { min: number; max: number } {
  let min = 255;
  let max = 0;
  for (let i = 0; i < data.length; i += 4) {
    min = Math.min(min, data[i], data[i + 1], data[i + 2]);
    max = Math.max(max, data[i], data[i + 1], data[i + 2]);
  }
  return { min, max };
}

export async function boot(root: HTMLElement, pageErrors: string[]): Promise<void> {
  const file = document.createElement('input');
  file.type = 'file';
  file.accept = '.dng,.DNG';
  file.multiple = true;
  const summary = el('p', 'summary', '选一张 GXR 的 DNG。');
  const note = el('p', 'note', '开发用中性预览，不是配方');
  const reason = el('p', 'reason', '');
  const perf = el('p', 'perf', '');
  const canvas = document.createElement('canvas');
  const exposure = slider('曝光', '-2', '2', '0.01', '0');
  const contrast = slider('对比度', '-100', '100', '1', '0');
  const saturation = slider('饱和度', '-100', '100', '1', '0');
  const auto = el('button', 'auto', '自动拖动测试');
  auto.type = 'button';
  root.append(file, note, summary, reason, canvas, exposure.row, contrast.row, saturation.row, auto, perf);

  const interval = await measureInterval();
  perf.textContent = `刷新间隔 ${interval.toFixed(1)} ms`;
  let engine: Engine | null = null;
  let gpuNote = '';
  if (!navigator.gpu) gpuNote = '这个浏览器现在用不了显卡预览';
  else {
    engine = await Engine.create();
    if (!engine) gpuNote = '这个浏览器现在用不了显卡预览';
  }
  if (gpuNote) reason.textContent = gpuNote;

  const bake = neutralBake();
  if (engine) {
    engine.addBake(NEUTRAL_BAKE_KEY, bake.meta, bake.data);
    engine.attach(canvas);
  }

  let dragging: { stop: () => PerfStats } | null = null;
  const readAdjust = (): Adjust => ({
    exposure: Number(exposure.input.value),
    contrast: Number(contrast.input.value),
    saturation: Number(saturation.input.value),
  });
  const pushLook = () => {
    const adjust = readAdjust();
    engine?.setLook({
      recipe: NEUTRAL_BAKE_KEY,
      lut: null,
      lutStrength: 1,
      curve: null,
      adjust: { exposure_ev: adjust.exposure, contrast: adjust.contrast, saturation: adjust.saturation },
    });
  };
  for (const input of [exposure.input, contrast.input, saturation.input]) {
    input.addEventListener('pointerdown', () => {
      dragging ??= watchFrames(interval);
    });
    input.addEventListener('input', () => pushLook());
    const end = () => {
      if (!dragging) return;
      perf.textContent = formatStats(dragging.stop());
      dragging = null;
    };
    input.addEventListener('pointerup', end);
    input.addEventListener('pointercancel', end);
  }

  async function showBytes(bytes: ArrayBuffer, name: string): Promise<void> {
    summary.textContent = `${name} · 正在读取`;
    reason.textContent = gpuNote;
    const described = await describeDng(bytes);
    if (described.reason || !described.layout) {
      const why = described.reason || '没有可用的布局';
      reason.textContent = `这张照片暂时不支持显卡预览：${why}`;
      summary.textContent = name;
      return;
    }
    const layout = described.layout;
    summary.textContent = `${name} · ${layout.width}×${layout.height} · ${layout.bps} 位`;
    if (!engine) return;
    const key = `dng-${name}`;
    await engine.addRaw(key, layout, bytes, described.scene);
    engine.show(key);
    pushLook();
  }

  file.addEventListener('change', () => {
    const picked = file.files?.[0];
    if (!picked) return;
    picked.arrayBuffer().then(
      (bytes) => showBytes(bytes, picked.name),
      (err: unknown) => {
        reason.textContent = err instanceof Error ? err.message : String(err);
      },
    );
  });

  async function runAuto(): Promise<PerfStats> {
    const watch = watchFrames(interval);
    const started = performance.now();
    await new Promise<void>((resolve) => {
      const tick = (now: number) => {
        const t = (now - started) / 1000;
        if (t >= 5) {
          resolve();
          return;
        }
        const span = Number(exposure.input.max) - Number(exposure.input.min);
        exposure.input.value = String(Number(exposure.input.min) + span * (t / 5));
        exposure.input.dispatchEvent(new Event('input'));
        requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    });
    return watch.stop();
  }

  auto.addEventListener('click', () => {
    runAuto().then((stats) => {
      perf.textContent = formatStats(stats);
    });
  });

  if (import.meta.env.DEV && new URLSearchParams(location.search).get('autotest') === '1') {
    await runAutotest({ engine, gpuNote, pageErrors, showBytes, runAuto, frameOf: () => engine?.frame() ?? null, readbackOf: () => engine });
  }
}

async function runAutotest(ctx: {
  engine: Engine | null;
  gpuNote: string;
  pageErrors: string[];
  showBytes: (bytes: ArrayBuffer, name: string) => Promise<void>;
  runAuto: () => Promise<PerfStats>;
  frameOf: () => FrameInfo | null;
  readbackOf: () => Engine | null;
}): Promise<void> {
  const report: ShellReport = {
    userAgent: navigator.userAgent,
    webgpu: ctx.engine !== null,
    layout: null,
    reason: ctx.gpuNote || null,
    scene: null,
    error: null,
    frame: null,
    readback: null,
    perf: null,
    pageErrors: ctx.pageErrors,
  };
  try {
    const listed = await fetch('/__dev/samples');
    if (!listed.ok) throw new Error(`样本列表 ${listed.status}`);
    const names = (await listed.json()) as string[];
    if (!names.length) throw new Error('样本目录是空的');
    const name = names[0];
    const body = await fetch(`/__dev/sample/${encodeURIComponent(name)}`);
    if (!body.ok) throw new Error(`样本 ${body.status}`);
    const bytes = await body.arrayBuffer();
    await ctx.showBytes(bytes, name);
    const described = await describeDng(bytes.slice(0));
    report.layout = described.layout;
    report.reason = described.reason || ctx.gpuNote || null;
    report.scene = described.scene;
    if (ctx.engine && described.layout && !described.reason) {
      report.frame = ctx.frameOf();
      const shot = await ctx.readbackOf()?.readback();
      if (shot) report.readback = { w: shot.w, h: shot.h, ...spread(shot.data) };
      report.perf = await ctx.runAuto();
    }
  } catch (err) {
    report.error = err instanceof Error ? err.message : String(err);
    report.pageErrors.push(report.error);
  }
  const label = new URLSearchParams(location.search).get('report') || 'autotest';
  await fetch(`/__dev/report/${encodeURIComponent(label)}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(report),
  });
}
