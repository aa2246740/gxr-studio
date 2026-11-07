// 导入结果怎么跟用户说：纯函数，不碰页面，方便单独测试（tests/test_import_summary.py 用 node 直接跑它）。
// 铁律：只要有任何文件没读进来（读不出来、校验不一致、文件夹读不了），或者导入结束的核对证明不了全部已在图库，
// 就绝不说"可以安全弹出卡"。永远不说"可以格式化"。

export const EJECT_OK = '可以推出存储卡';
export const KEEP_CARD = '请勿格式化存储卡';

// 没读进来的文件/文件夹（相对卡根的路径；文件夹以 / 结尾）、图库位置不对没有写的文件，
// 加上导入结束核对时证明不了已在图库的文件
export function problemFiles(p) {
  const uncovered = (p.coverage && p.coverage.uncovered) || [];
  const refused = p.write_refused || [];
  return [...new Set([...(p.unreadable || []), ...(p.verify_failed || []), ...(p.scan_errors || []), ...refused, ...uncovered])];
}

// 导入结束时没能核对整张卡（扫描不完整、DCIM 不是真目录、没有核对结果）
export function checkFailed(p) {
  if (!p || p.state !== 'done') return false;
  const c = p.coverage;
  return !c || !c.complete || c.dcim !== 'present';
}

// 卡上有、但工作室不会导入的文件（DCIM 里的非照片文件等）
export function notImportable(p) {
  return (p && p.coverage && p.coverage.not_importable) || [];
}

export function hasProblems(p) {
  return problemFiles(p).length > 0 || checkFailed(p);
}

// 只有"导入完成"、核对通过、也没有不会被导入的文件，才可以说推出
export function canEject(p) {
  return !!p && p.state === 'done' && !hasProblems(p) && notImportable(p).length === 0;
}

function shortName(rel) {
  const isDir = rel.endsWith('/');
  const parts = rel.replace(/\/$/, '').split('/');
  const name = parts[parts.length - 1] || rel;
  return isDir ? `文件夹 ${name}` : name;
}

// "A.JPG、B.DNG 等共 N 个"
function listFiles(files, max) {
  const shown = files.slice(0, max).map(shortName).join('、');
  return files.length > max ? `${shown} 等共 ${files.length} 个` : shown;
}

export function listProblems(p, max = 6) {
  return listFiles(problemFiles(p), max);
}

function gotText(p) {
  const photos = (p.imported_shots || 0) + (p.completed_shots || 0);
  const videos = p.imported_videos || 0;
  if (videos) return photos ? `已导入 ${photos} 张照片和 ${videos} 个视频` : `已导入 ${videos} 个视频`;
  return photos ? `已导入 ${photos} 张照片` : '';
}

// 导入进行中的一行字和进度（0–1；还不知道总数时是 null，显示成转圈）。顶栏面板和相机页共用。
export function progressLine(p) {
  if (p.state === 'scanning' || !p.total_shots) return { text: '正在读取存储卡…', fraction: null };
  if (p.state === 'checking') return { text: '正在核对存储卡上的照片…', fraction: null };
  return { text: `正在导入 ${p.done_shots}/${p.total_shots}`, fraction: p.done_shots / p.total_shots };
}

// 卡上有工作室不会导入的文件时怎么说（和 cards/messages.py 的 not_importable_message 同一句话）。
// 「照片都已导入」只在后端核对证明了每张照片都在图库里（coverage.photos_imported）时才说；没有这个证明就只说个数。
// 永远不说「可以格式化」，也不说「可以推出」。
function notImportableText(got, extra, photosImported) {
  if (photosImported) {
    const note = `另有 ${extra} 个其他文件不会导入`;
    return got ? `${got}，卡上的照片都已在图库里；${note}` : `照片都已导入；${note}`;
  }
  return `${got ? `${got}。` : ''}存储卡上有 ${extra} 个文件不会被导入`;
}

// 手动点「导入照片」后弹的提示：-> { kind: 'ok' | 'error' | 'info', text }
// 没有新照片时明说「已在图库里」，不让人以为按钮没反应。
export function importToast(p) {
  const r = summarizeImport(p);
  const kind = r.kind === 'bad' ? 'error' : r.kind === 'ok' ? 'ok' : 'info';
  if (p.state !== 'done' || hasProblems(p)) return { kind, text: r.text };
  const got = gotText(p);
  if (got) return { kind, text: r.text };
  const extra = notImportable(p);
  if (!extra.length) return { kind, text: '这张存储卡上的照片都已在图库里，没有新的可导入' };
  if (p.coverage.photos_imported === true) return { kind: 'info', text: `这张存储卡上的照片都已在图库里，没有新的可导入；另有 ${extra.length} 个其他文件不会导入` };
  return { kind, text: r.text };
}

// -> { kind: 'ok' | 'bad' | '', text, detail, retry: bool }
export function summarizeImport(p) {
  const skipped = (p.seen_shots || 0) + (p.duplicate_shots || 0);
  const problems = hasProblems(p);
  const detail = problems ? `未导入：${listProblems(p)}` : '';
  switch (p.state) {
    case 'done': {
      if (problems) {
        const files = problemFiles(p);
        return {
          kind: 'bad',
          text: files.length
            ? `有 ${files.length} 个文件未能导入。请勿格式化存储卡，重新插入后再试一次。`
            : '无法核对存储卡上的照片，请勿格式化存储卡。',
          detail,
          retry: true,
        };
      }
      const got = gotText(p);
      const extra = notImportable(p);
      if (extra.length) {
        return {
          kind: '',
          text: notImportableText(got, extra.length, p.coverage.photos_imported === true),
          detail: `不会被导入：${listFiles(extra, 6)}`,
          retry: false,
        };
      }
      return {
        kind: 'ok',
        text: got || '这张存储卡上的照片都已导入',
        detail: '',
        retry: false,
        eject: true,
      };
    }
    case 'interrupted':
      return { kind: 'bad', text: '导入已中断。重新插入存储卡后可以继续。', detail, retry: true };
    case 'paused_no_space':
      return { kind: 'bad', text: '磁盘空间不足。请清理后重试。', detail, retry: true };
    case 'cancelled':
      return { kind: '', text: `已取消，已导入的 ${p.imported_shots} 张已保留`, detail, retry: true };
    case 'failed':
      return { kind: 'bad', text: '导入失败。请重试。', detail, retry: true };
    default:
      return { kind: '', text: p.message || '导入完成', detail: '', retry: false };
  }
}

// 「推出」是 macOS 的说法；Windows 上叫「弹出」（ORCH_DECISIONS：Windows 用「可以弹出」）。
// 后端的结果句子和这里的文字都写「推出」，显示前按系统换掉。
export function ejectWords(text, os) {
  return os === 'win' && typeof text === 'string' ? text.replaceAll('推出', '弹出') : text;
}

// 状态栏里卡那一栏的文字（导入结束后）
export function doneStatusText(p) {
  if (canEject(p)) return '存储卡：可以推出';
  // 状态行只用 COPY 5 的四种说法；不会被导入的文件由横幅说明
  return !hasProblems(p) && notImportable(p).length ? '存储卡：已插入' : '存储卡：有未导入的照片';
}

// 同时插着不止一张卡（COPY 3.10：名单不再放进括号）
export function multiCardNote(cards) {
  if (!cards || cards.length < 2) return null;
  return `已插入 ${cards.length} 张存储卡，先导入「${cards[0].label}」`;
}
