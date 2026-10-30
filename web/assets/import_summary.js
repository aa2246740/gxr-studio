// 导入结果怎么跟用户说：纯函数，不碰页面，方便单独测试（tests/test_import_summary.py 用 node 直接跑它）。
// 铁律：只要有任何文件没读进来（读不出来、校验不一致、文件夹读不了），就绝不说"可以安全弹出卡"。

export const EJECT_OK = '可以推出存储卡';
export const KEEP_CARD = '请勿格式化存储卡';

// 没读进来的文件/文件夹（相对卡根的路径；文件夹以 / 结尾）
export function problemFiles(p) {
  return [...(p.unreadable || []), ...(p.verify_failed || []), ...(p.scan_errors || [])];
}

export function hasProblems(p) {
  return problemFiles(p).length > 0;
}

// 只有"导入完成"且一个问题都没有，才可以说安全弹出
export function canEject(p) {
  return !!p && p.state === 'done' && !hasProblems(p);
}

function shortName(rel) {
  const isDir = rel.endsWith('/');
  const parts = rel.replace(/\/$/, '').split('/');
  const name = parts[parts.length - 1] || rel;
  return isDir ? `文件夹 ${name}` : name;
}

// "A.JPG、B.DNG 等共 N 个"
export function listProblems(p, max = 6) {
  const files = problemFiles(p);
  const shown = files.slice(0, max).map(shortName).join('、');
  return files.length > max ? `${shown} 等共 ${files.length} 个` : shown;
}

// -> { kind: 'ok' | 'bad' | '', text, detail, retry: bool }
export function summarizeImport(p) {
  const skipped = (p.seen_shots || 0) + (p.duplicate_shots || 0);
  const problems = hasProblems(p);
  const detail = problems ? `未导入：${listProblems(p)}` : '';
  switch (p.state) {
    case 'done': {
      if (problems) {
        const got = `已导入 ${p.imported_shots} 张`;
        const lost = p.failed_shots ? `${p.failed_shots} 张未完整导入，` : '';
        return {
          kind: 'bad',
          text: `有 ${problemFiles(p).length} 个文件未能导入。请勿格式化存储卡，重新插入后再试一次。`,
          detail,
          retry: true,
        };
      }
      const got = `已导入 ${p.imported_shots} 张`;
      return {
        kind: 'ok',
        text: got,
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

// 状态栏里卡那一栏的文字（导入结束后）
export function doneStatusText(p) {
  return canEject(p) ? '存储卡：可以推出' : '存储卡：有未导入的照片';
}

// 同时插着不止一张卡（COPY 3.10：名单不再放进括号）
export function multiCardNote(cards) {
  if (!cards || cards.length < 2) return null;
  return `已插入 ${cards.length} 张存储卡，先导入「${cards[0].label}」`;
}
