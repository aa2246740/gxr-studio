// 只在开发服务器里给模拟器截图用。样本走开发端点，不进正式包。

export async function sampleFiles(limit = 6): Promise<File[]> {
  const listed = await fetch('/__dev/samples');
  if (!listed.ok) return [];
  const names = ((await listed.json()) as string[]).filter((name) => !name.startsWith('zz-'));
  const files: File[] = [];
  for (const name of names.slice(0, limit)) {
    const response = await fetch(`/__dev/sample/${encodeURIComponent(name)}`);
    if (!response.ok) continue;
    const bytes = await response.arrayBuffer();
    files.push(new File([bytes], name));
  }
  return files;
}

/** 截图脚本等这个文件出现再截。开发服务器把它写进 mobile/.reports（不进仓库）。 */
export async function reportShot(name: string, data: unknown): Promise<void> {
  try {
    await fetch(`/__dev/report/shot-${encodeURIComponent(name)}`, { method: 'POST', body: JSON.stringify(data) });
  } catch {
    /* 截图脚本会超时并报出来。 */
  }
}

/** 截图用：样本目录里 zz- 开头的别名文件（端点只认 .dng），换成真正的文件名。 */
export async function aliasFile(alias: string, name: string): Promise<File | null> {
  try {
    const response = await fetch(`/__dev/sample/${encodeURIComponent(alias)}`);
    if (!response.ok) return null;
    return new File([await response.arrayBuffer()], name, { lastModified: Date.now() });
  } catch {
    return null;
  }
}
