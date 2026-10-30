// 导入结果怎么跟用户说：纯函数，不碰页面，方便单独测试（tests/test_import_summary.py 用 node 直接跑它）。
// 铁律：只要有任何文件没读进来（读不出来、校验不一致、文件夹读不了），就绝不说"可以安全弹出卡"。

export const EJECT_OK = '现在可以安全弹出卡了。';
export const KEEP_CARD = '请先别格式化或清空这张卡：换个读卡器或重新插拔，再点「继续导入」。';

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
  const detail = problems ? `没导进来的：${listProblems(p)}` : '';
  switch (p.state) {
    case 'done': {
      if (problems) {
        const got = `新增 ${p.imported_shots} 张` + (p.completed_shots ? `、补全 ${p.completed_shots} 张` : '');
        const lost = p.failed_shots ? `有 ${p.failed_shots} 张没有完整导进来，` : '';
        return {
          kind: 'bad',
          text: `导入没有全部完成：${got}，跳过 ${skipped} 张（之前导入过或内容重复）。${lost}共 ${problemFiles(p).length} 个文件没读成功，卡上的文件没有改动。${KEEP_CARD}`,
          detail,
          retry: true,
        };
      }
      const got = `新增 ${p.imported_shots} 张` + (p.completed_shots ? `、补全 ${p.completed_shots} 张` : '');
      return {
        kind: 'ok',
        text: `导入完成：${got}，跳过 ${skipped} 张（之前导入过或内容重复）。${EJECT_OK}`,
        detail: '',
        retry: false,
        eject: true,
      };
    }
    case 'interrupted':
      return { kind: 'bad', text: `导入被打断了：已导入 ${p.imported_shots} 张，剩下的没有导入。插回卡可以继续。`, detail, retry: true };
    case 'paused_no_space':
      return { kind: 'bad', text: '电脑空间不够，导入已暂停。清理后可以继续。', detail, retry: true };
    case 'cancelled':
      return { kind: '', text: `已取消导入。已导入的 ${p.imported_shots} 张照片保留着。`, detail, retry: true };
    case 'failed':
      return { kind: 'bad', text: `导入没有完成：${p.message || '出了一点问题'}`, detail, retry: true };
    default:
      return { kind: '', text: p.message || '导入结束', detail: '', retry: false };
  }
}

// 状态栏里卡那一栏的文字（导入结束后）
export function doneStatusText(p) {
  return canEject(p) ? '卡：导入完成，可以安全弹出卡了（在访达里点弹出）' : '卡：有照片没导进来，先别格式化卡（看上面的提示）';
}

// 同时插着不止一张卡
export function multiCardNote(cards) {
  if (!cards || cards.length < 2) return null;
  const names = cards.map((c) => `「${c.label}」`).join('、');
  return `现在插着 ${cards.length} 张卡（${names}）。工作室一次只处理第一张「${cards[0].label}」；处理完拔掉它，再处理下一张。`;
}
