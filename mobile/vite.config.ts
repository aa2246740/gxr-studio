// 开发服务器只绑 127.0.0.1。样本和测试回执只在 dev 里存在，打进生产包的检查会搜 __dev。
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Connect, Plugin, ViteDevServer } from 'vite';
import { defineConfig } from 'vite';

const root = fileURLToPath(new URL('.', import.meta.url));

function sampleRoot(): string | null {
  const given = process.env.GXR_MOBILE_SAMPLES;
  if (!given || !path.isAbsolute(given)) return null;
  try {
    return fs.realpathSync(given);
  } catch {
    return null;
  }
}

function dngName(name: string): boolean {
  return !!name && !name.includes('/') && !name.includes('\\') && !name.includes('..') && /\.dng$/i.test(name);
}

/** A regular file whose real path stays inside `rootReal`. Symlinks that leave are rejected. */
function containedFile(rootReal: string, name: string): string | null {
  if (!dngName(name)) return null;
  const prefix = rootReal.endsWith(path.sep) ? rootReal : rootReal + path.sep;
  let real: string;
  try {
    real = fs.realpathSync(path.join(rootReal, name));
  } catch {
    return null;
  }
  if (real !== prefix.slice(0, -1) && !real.startsWith(prefix)) return null;
  try {
    if (!fs.statSync(real).isFile()) return null;
  } catch {
    return null;
  }
  return real;
}

function readBody(req: Connect.IncomingMessage, limit: number): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let size = 0;
    req.on('data', (chunk: Buffer) => {
      size += chunk.length;
      if (size > limit) {
        reject(new Error('body too large'));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
  });
}

function devSamples(): Plugin {
  return {
    name: 'gxr-dev-samples',
    apply: 'serve',
    configureServer(server: ViteDevServer) {
      server.middlewares.use((req, res, next) => {
        const url = new URL(req.url || '/', 'http://127.0.0.1');
        const samples = url.pathname === '/__dev/samples';
        const sample = url.pathname.startsWith('/__dev/sample/');
        const report = url.pathname.startsWith('/__dev/report/');
        if (!samples && !sample && !report) {
          next();
          return;
        }
        void handleDev(req, res, url, samples, sample, report).catch((err: unknown) => {
          res.statusCode = 400;
          res.end(err instanceof Error ? err.message : String(err));
        });
      });
    },
  };
}

async function handleDev(
  req: Connect.IncomingMessage,
  res: Connect.ServerResponse,
  url: URL,
  samples: boolean,
  sample: boolean,
  report: boolean,
): Promise<void> {
  if (samples) {
    const dir = sampleRoot();
    const names = dir
      ? fs
          .readdirSync(dir)
          .filter((name) => containedFile(dir, name) !== null)
          .sort()
      : [];
    res.setHeader('content-type', 'application/json');
    res.end(JSON.stringify(names));
    return;
  }
  if (sample) {
    const dir = sampleRoot();
    const name = decodeURIComponent(url.pathname.slice('/__dev/sample/'.length));
    const full = dir ? containedFile(dir, name) : null;
    if (!full) {
      res.statusCode = 404;
      res.end('not found');
      return;
    }
    res.setHeader('content-type', 'application/octet-stream');
    res.end(fs.readFileSync(full));
    return;
  }
  if (!report || req.method !== 'POST') {
    res.statusCode = 405;
    res.end('POST only');
    return;
  }
  const name = decodeURIComponent(url.pathname.slice('/__dev/report/'.length));
  if (!/^[A-Za-z0-9_-]+$/.test(name)) {
    res.statusCode = 400;
    res.end('bad name');
    return;
  }
  const body = await readBody(req, 1_000_000);
  JSON.parse(body.toString('utf8'));
  const dir = path.resolve(root, '.reports');
  fs.mkdirSync(dir, { recursive: true });
  const resolved = path.resolve(dir);
  const realDir = fs.realpathSync(dir);
  if (realDir !== resolved) {
    res.statusCode = 400;
    res.end('report dir escapes');
    return;
  }
  const target = path.join(dir, `${name}.json`);
  try {
    if (fs.lstatSync(target).isSymbolicLink()) {
      res.statusCode = 400;
      res.end('report target escapes');
      return;
    }
  } catch (err) {
    const code = (err as NodeJS.ErrnoException).code;
    if (code !== 'ENOENT') {
      res.statusCode = 400;
      res.end('report target escapes');
      return;
    }
  }
  const flags =
    fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_TRUNC | fs.constants.O_NOFOLLOW;
  const fd = fs.openSync(target, flags);
  try {
    fs.writeFileSync(fd, body);
  } finally {
    fs.closeSync(fd);
  }
  res.statusCode = 204;
  res.end();
}

export default defineConfig({
  root,
  server: {
    host: '127.0.0.1',
    port: 5199,
    strictPort: true,
    fs: { allow: [root, path.resolve(root, '../frontend')] },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      input: {
        main: path.resolve(root, 'index.html'),
        dev: path.resolve(root, 'dev.html'),
      },
    },
  },
  plugins: [devSamples()],
});
