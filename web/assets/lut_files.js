// 文件夹读取会分批返回目录项，必须读到空批次。名称仅作为数据传给服务。
export function skippedPath(path) {
  return path.replaceAll('\\', '/').split('/').some((p) => p === '__MACOSX' || p.startsWith('._'));
}

export function selectedFiles(files) {
  return Array.from(files, (file) => ({ file, path: file.webkitRelativePath || file.name })).filter((f) => !skippedPath(f.path));
}

export async function droppedFiles(transfer) {
  const entries = Array.from(transfer.items || [], (item) => item.webkitGetAsEntry?.()).filter(Boolean);
  if (!entries.length) return selectedFiles(transfer.files || []);
  const result = [];
  async function visit(entry, parent = '') {
    const path = parent + entry.name;
    if (skippedPath(path)) return;
    if (entry.isFile) {
      const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
      result.push({ file, path });
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      while (true) {
        const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
        if (!batch.length) break;
        for (const child of batch) await visit(child, `${path}/`);
      }
    }
  }
  for (const entry of entries) await visit(entry);
  return result;
}

export async function importFiles(files, upload) {
  const results = [];
  for (const { file, path } of files) {
    if (skippedPath(path)) continue;
    try {
      const lut = await upload(file, path);
      results.push({ path, lut, status: lut.duplicate ? 'duplicate' : lut.skipped ? 'skipped' : 'imported' });
    } catch (error) {
      results.push({ path, status: 'error', message: error.message });
    }
  }
  return results;
}
