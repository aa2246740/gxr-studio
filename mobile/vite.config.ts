// 开发服务器只绑 127.0.0.1。样本和测试回执只在 dev 里存在，打进生产包的检查会搜 __dev。
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Connect, Plugin, ViteDevServer } from 'vite';
import { defineConfig } from 'vite';
import { DEFAULT_CALIB_PATH } from './src/lib/default_calib.ts';

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
  const isBin = String(req.headers['content-type'] || '').startsWith('application/octet-stream');
  const body = await readBody(req, isBin ? 200_000_000 : 1_000_000);
  if (!isBin) JSON.parse(body.toString('utf8'));
  const dir = path.resolve(root, '.reports');
  fs.mkdirSync(dir, { recursive: true });
  const resolved = path.resolve(dir);
  const realDir = fs.realpathSync(dir);
  if (realDir !== resolved) {
    res.statusCode = 400;
    res.end('report dir escapes');
    return;
  }
  const target = path.join(dir, `${name}.${isBin ? 'bin' : 'json'}`);
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

/**
 * 测试用的开关：`GXR_MOBILE_DEFAULT_CALIB=off` 时开发服务器不发内置的默认校准（404，和第一次打开时没联网一样）；
 * 给一个绝对路径时改发那个文件（核对不过的情形）；`hold:<文件夹>` 时先压着不回，等 `<文件夹>/release` 出现（最多 2 分钟）
 * 再发 public/ 里的那份（默认校准下载得慢的情形）。不设就照常发 public/ 里的那份。只在开发服务器里，不进构建。
 */
function defaultCalibSwitch(): Plugin {
  return {
    name: 'gxr-default-calib-switch',
    apply: 'serve',
    configureServer(server: ViteDevServer) {
      const mode = process.env.GXR_MOBILE_DEFAULT_CALIB;
      if (!mode) return;
      server.middlewares.use((req, res, next) => {
        if (new URL(req.url || '/', 'http://127.0.0.1').pathname !== `/${DEFAULT_CALIB_PATH}`) {
          next();
          return;
        }
        if (mode.startsWith('hold:')) {
          const release = path.join(mode.slice('hold:'.length), 'release');
          const until = Date.now() + 120_000;
          let timer: ReturnType<typeof setTimeout> | null = null;
          // 客户端先断开（页面关了）：不再轮询，也不往关掉的连接里写。
          res.on('close', () => {
            if (timer) clearTimeout(timer);
            timer = null;
          });
          const tick = (): void => {
            timer = null;
            if (res.writableEnded || res.destroyed) return;
            if (!fs.existsSync(release) && Date.now() < until) {
              timer = setTimeout(tick, 100);
              return;
            }
            res.setHeader('content-type', 'application/zip');
            res.end(fs.readFileSync(path.join(root, 'public', DEFAULT_CALIB_PATH)));
          };
          tick();
          return;
        }
        let body: Buffer | null = null;
        try {
          body = path.isAbsolute(mode) ? fs.readFileSync(mode) : null;
        } catch {
          body = null;
        }
        if (!body) {
          res.statusCode = 404;
          res.end('default calibration is off');
          return;
        }
        res.setHeader('content-type', 'application/zip');
        res.end(body);
      });
    },
  };
}

/** 构建号：这次构建的短提交号，写进导出 EXIF 描述的 `gxr-core <构建号>`。开发服务器里是 `dev`。 */
function buildId(command: string): string {
  if (command !== 'build') return 'dev';
  try {
    return execFileSync('git', ['rev-parse', '--short', 'HEAD'], { cwd: root, encoding: 'utf8' }).trim() || 'unknown';
  } catch {
    return 'unknown';
  }
}

/** 生产构建把 main.ts 对 ./app/boot 的动态导入改成静态导入：首屏少一次 main -> boot 的串行往返。开发服务器不变（dev.html 走 devshell）。 */
function prodStaticBoot(): Plugin {
  const mainFile = path.resolve(root, 'src', 'main.ts');
  return {
    name: 'gxr-prod-static-boot',
    apply: 'build',
    enforce: 'pre',
    transform(code, id) {
      if (path.resolve(id.split('?')[0]) !== mainFile) return null;
      const needle = "const { bootApp } = await import('./app/boot');";
      if (!code.includes(needle)) throw new Error('gxr-prod-static-boot: main.ts no longer imports ./app/boot the expected way');
      return `import { bootApp } from './app/boot';\n${code.replace(needle, '')}`;
    },
  };
}

/** 首屏之后只懒加载这两个分包（见 src/app/boot_lazy.ts）。 */
const LAZY_ENTRIES: Record<string, string> = {
  grid: path.resolve(root, 'src', 'app', 'grid.ts'),
  editor: path.resolve(root, 'src', 'app', 'editor_chunk.ts'),
};

/**
 * 生产构建：把两个懒加载分包的文件名写进主包（boot_lazy.ts 的占位串），加载失败后换查询串重新请求要用。
 * 同时检查分包只有 JS、只静态依赖主包：这样重新请求一个文件就能整份拿到，不会卡在某个记住了失败的共享依赖上。
 */
function lazyChunkNames(): Plugin {
  return {
    name: 'gxr-lazy-chunk-names',
    apply: 'build',
    generateBundle(_options, bundle) {
      const chunks = Object.values(bundle).filter((item) => item.type === 'chunk');
      const mainFile = path.resolve(root, 'src', 'main.ts');
      // 主包：装着 main.ts 和首屏代码的那一块（两个 HTML 入口各有一个小门面，都静态导入它）。
      const entry = chunks.find((item) => item.moduleIds.includes(mainFile));
      if (!entry) throw new Error('gxr-lazy-chunk-names: main chunk not found');
      const files: Record<string, string> = {};
      for (const [key, file] of Object.entries(LAZY_ENTRIES)) {
        const chunk = chunks.find((item) => item.facadeModuleId === file);
        if (!chunk || !chunk.isDynamicEntry) throw new Error(`gxr-lazy-chunk-names: ${key} is not a lazy chunk`);
        const extra = chunk.imports.filter((dep) => dep !== entry.fileName);
        if (extra.length) throw new Error(`gxr-lazy-chunk-names: ${key} chunk also depends on ${extra.join(', ')}`);
        const css = (chunk as { viteMetadata?: { importedCss?: Set<string> } }).viteMetadata?.importedCss;
        if (css?.size) throw new Error(`gxr-lazy-chunk-names: ${key} chunk carries CSS ${[...css].join(', ')}`);
        files[key] = path.posix.relative(path.posix.dirname(entry.fileName), chunk.fileName);
      }
      const lazy = chunks.filter((item) => item.isDynamicEntry && !item.isEntry);
      const unexpected = lazy.filter((item) => !Object.values(LAZY_ENTRIES).includes(item.facadeModuleId ?? ''));
      if (unexpected.length) throw new Error(`gxr-lazy-chunk-names: unexpected lazy chunks ${unexpected.map((item) => item.fileName).join(', ')}`);
      if (!entry.code.includes('__GXR_LAZY_CHUNKS__')) throw new Error('gxr-lazy-chunk-names: placeholder missing from the main chunk');
      entry.code = entry.code.replace(/(['"`])__GXR_LAZY_CHUNKS__\1/g, JSON.stringify(JSON.stringify(files)));
    },
  };
}

export default defineConfig(({ command }) => ({
  root,
  define: { __GXR_BUILD__: JSON.stringify(buildId(command)) },
  server: {
    host: '127.0.0.1',
    port: 5199,
    strictPort: true,
    fs: { allow: [root, path.resolve(root, '../frontend'), path.resolve(root, '../src/gxr_studio/assets/fonts')] },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
    rolldownOptions: {
      input: {
        main: path.resolve(root, 'index.html'),
        dev: path.resolve(root, 'dev.html'),
      },
      output: {
        // 首屏静态依赖的模块、以及网格和编辑器两个分包都用到的模块，全部放进主包。
        // 两个分包因此只依赖主包（lazyChunkNames 在构建时核对）：失败后重新请求一个文件就能整份拿到。
        codeSplitting: { groups: [{ name: 'main', minShareCount: 2 }] },
      },
    },
  },
  plugins: [prodStaticBoot(), lazyChunkNames(), devSamples(), defaultCalibSwitch()],
}));
