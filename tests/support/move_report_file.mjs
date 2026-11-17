// 把测试报告从 mobile/.reports 挪到 pytest 的临时目录。两处不在同一块盘时 rename 会 EXDEV。
import { copyFileSync, renameSync, rmSync } from 'node:fs';

export function moveReportFile(src, dst) {
  try {
    renameSync(src, dst);
  } catch (err) {
    if (!err || err.code !== 'EXDEV') throw err;
    copyFileSync(src, dst);
    rmSync(src);
  }
}
