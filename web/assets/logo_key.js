// 上传标志弹窗的本机预览：检测白底、四邻域边缘去白底（与 finish/logo_store.py 的 key_white 逐行一致）、裁墨迹框与棋盘格绘制。

export function sniffImageMagic(bytes) {
  if (!bytes || bytes.length < 12) return null;
  if (bytes[0] === 0x89 && bytes[1] === 0x50 && bytes[2] === 0x4e && bytes[3] === 0x47
    && bytes[4] === 0x0d && bytes[5] === 0x0a && bytes[6] === 0x1a && bytes[7] === 0x0a) return 'PNG';
  if (bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) return 'JPEG';
  if (bytes[0] === 0x52 && bytes[1] === 0x49 && bytes[2] === 0x46 && bytes[3] === 0x46
    && bytes[8] === 0x57 && bytes[9] === 0x45 && bytes[10] === 0x42 && bytes[11] === 0x50) return 'WEBP';
  return null;
}

export function isOpaqueImageData(imgData) {
  const d = imgData.data;
  for (let i = 3; i < d.length; i += 4) if (d[i] < 250) return false;
  return true;
}

export function hasWhiteBackground(imgData) {
  if (!isOpaqueImageData(imgData)) return false;
  const { width: w, height: h, data: d } = imgData;
  if (w < 1 || h < 1) return false;
  const coords = [[0, 0], [w - 1, 0], [0, h - 1], [w - 1, h - 1]];
  return coords.every(([x, y]) => {
    const o = (y * w + x) * 4;
    return Math.min(d[o], d[o + 1], d[o + 2]) >= 238;
  });
}

export function keyWhiteImageData(imgData, tol = 30) {
  const clamped = Math.max(0, Math.min(60, Math.trunc(tol)));
  const { width: w, height: h, data: src } = imgData;
  const n = w * h;
  const near = new Uint8Array(n);
  for (let i = 0; i < n; i++) {
    const o = i * 4;
    if (255 - Math.min(src[o], src[o + 1], src[o + 2]) <= clamped) near[i] = 1;
  }
  const seen = new Uint8Array(n);
  const queue = new Int32Array(n);
  let head = 0, tail = 0;
  const push = (idx) => {
    if (near[idx] && !seen[idx]) {
      seen[idx] = 1;
      queue[tail++] = idx;
    }
  };
  for (let x = 0; x < w; x++) {
    push(x);
    push((h - 1) * w + x);
  }
  for (let y = 0; y < h; y++) {
    push(y * w);
    push(y * w + (w - 1));
  }
  while (head < tail) {
    const idx = queue[head++];
    const x = idx % w, y = (idx - x) / w;
    if (x > 0) push(idx - 1);
    if (x + 1 < w) push(idx + 1);
    if (y > 0) push(idx - w);
    if (y + 1 < h) push(idx + w);
  }
  const out = new Uint8ClampedArray(n * 4);
  for (let y = 0; y < h; y++) {
    const y0 = Math.max(0, y - 1), y1 = Math.min(h - 1, y + 1);
    for (let x = 0; x < w; x++) {
      const idx = y * w + x;
      const o = idx * 4;
      out[o] = src[o];
      out[o + 1] = src[o + 1];
      out[o + 2] = src[o + 2];
      if (seen[idx]) {
        out[o + 3] = 0;
        continue;
      }
      if (x === 0 || x === w - 1 || y === 0 || y === h - 1) {
        out[o + 3] = 255;
        continue;
      }
      const x0 = x - 1, x1 = x + 1;
      let hasBg = false;
      for (let yy = y0; yy <= y1 && !hasBg; yy++) {
        const row = yy * w;
        for (let xx = x0; xx <= x1; xx++) {
          if (seen[row + xx]) { hasBg = true; break; }
        }
      }
      if (hasBg) {
        const whiteFrac = Math.max(0, Math.min(255, 255 - Math.min(src[o], src[o + 1], src[o + 2])));
        const prod16 = ((whiteFrac * 255) << 16) >> 16;
        out[o + 3] = Math.max(0, Math.min(255, Math.floor(prod16 / 160)));
      } else {
        out[o + 3] = 255;
      }
    }
  }
  return { width: w, height: h, data: out };
}

export function trimBounds(imgData, thresh = 8) {
  const { width: w, height: h, data: d } = imgData;
  let minX = w, minY = h, maxX = -1, maxY = -1;
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      if (d[(y * w + x) * 4 + 3] > thresh) {
        if (x < minX) minX = x;
        if (x > maxX) maxX = x;
        if (y < minY) minY = y;
        if (y > maxY) maxY = y;
      }
    }
  }
  if (maxX < minX || maxY < minY) return null;
  return [minX, minY, maxX - minX + 1, maxY - minY + 1];
}

export function drawCheckerPreview(canvas, imgData, { cropToInk = false } = {}) {
  const ctx = canvas.getContext('2d');
  const cw = canvas.width, ch = canvas.height;
  const cell = 12;
  for (let y = 0; y < ch; y += cell) {
    for (let x = 0; x < cw; x += cell) {
      ctx.fillStyle = ((x / cell + y / cell) & 1) ? '#2a2a2e' : '#36363c';
      ctx.fillRect(x, y, cell, cell);
    }
  }
  if (!imgData) return;
  const box = cropToInk ? trimBounds(imgData) : [0, 0, imgData.width, imgData.height];
  if (!box) return;
  const [bx, by, bw, bh] = box;
  const tmp = document.createElement('canvas');
  tmp.width = imgData.width;
  tmp.height = imgData.height;
  tmp.getContext('2d').putImageData(new ImageData(imgData.data, imgData.width, imgData.height), 0, 0);
  const pad = 20;
  const availW = Math.max(1, cw - 2 * pad), availH = Math.max(1, ch - 2 * pad);
  const scale = Math.min(availW / bw, availH / bh);
  const dw = bw * scale, dh = bh * scale;
  ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(tmp, bx, by, bw, bh, (cw - dw) / 2, (ch - dh) / 2, dw, dh);
}
