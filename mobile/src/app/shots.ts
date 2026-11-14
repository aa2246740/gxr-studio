// 只在开发服务器里给模拟器截图用。样本走开发端点，不进正式包。

export async function sampleFiles(limit = 6): Promise<File[]> {
  const listed = await fetch('/__dev/samples');
  if (!listed.ok) return [];
  const names = (await listed.json()) as string[];
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
