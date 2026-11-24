// 开发服务器只绑 127.0.0.1。样本和测试回执只在 dev 里存在，打进生产包的检查会搜 __dev。
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Connect, Plugin, ViteDevServer } from 'vite';
import { defineConfig } from 'vite';
import { DEFAULT_CALIB_PATH, DEFAULT_CALIB_SHA256 } from './src/lib/default_calib.ts';

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

/** 生产构建时生成带内容哈希与精确白名单的 dist/sw.js；开发服务器不启用。 */
function serviceWorkerBuild(): Plugin {
  const PRECACHE_PREFIXES = [
    'assets/main-',
    'assets/boot-',
    'assets/rolldown-runtime-',
    'assets/worker-',
    'assets/gxr_wasm_bg-',
    'assets/engine-',
  ];
  return {
    name: 'gxr-service-worker',
    apply: 'build',
    writeBundle(options) {
      const outDir = options.dir ? path.resolve(options.dir) : path.resolve(root, 'dist');
      const assetsDir = path.join(outDir, 'assets');
      const template = fs.readFileSync(path.join(root, 'src', 'sw.js'), 'utf8');
      const indexPath = path.join(outDir, 'index.html');
      let indexHtml = fs.readFileSync(indexPath, 'utf8');

      const allowed = fs
        .readdirSync(assetsDir)
        .filter((name) => !name.startsWith('dev-'))
        .map((name) => `assets/${name}`)
        .sort();

      // 给生产入口页补上开始页核心分包的并行预加载，避免 main -> boot -> css 串行往返。
      const bootJs = allowed.find((name) => /^assets\/boot-[^/]+\.js$/.test(name));
      const bootCss = allowed.find((name) => /^assets\/boot-[^/]+\.css$/.test(name));
      const preloadTags: string[] = [];
      if (bootJs && !indexHtml.includes(bootJs)) {
        preloadTags.push(`    <link rel="modulepreload" crossorigin href="/${bootJs}">`);
      }
      if (bootCss && !indexHtml.includes(bootCss)) {
        preloadTags.push(`    <link rel="stylesheet" crossorigin href="/${bootCss}">`);
      }
      if (preloadTags.length > 0 && indexHtml.includes('</head>')) {
        indexHtml = indexHtml.replace('  </head>', `${preloadTags.join('\n')}\n  </head>`);
        fs.writeFileSync(indexPath, indexHtml, 'utf8');
      }

      const entryMatch = indexHtml.match(/assets\/main-[^"']+\.js/);
      const entryScript = entryMatch ? entryMatch[0] : '';

      const precache = [
        'index.html',
        ...allowed.filter((name) => PRECACHE_PREFIXES.some((prefix) => name.startsWith(prefix))),
        DEFAULT_CALIB_PATH,
      ];

      const digest = crypto
        .createHash('sha256')
        .update(template)
        .update('\0')
        .update(indexHtml)
        .update('\0')
        .update(DEFAULT_CALIB_SHA256)
        .update('\0')
        .update(allowed.join('\n'))
        .digest('hex')
        .slice(0, 12);
      const cacheName = `gxr-studio-v1-${digest}`;

      const source = template
        .replace('__GXR_SW_CACHE__', cacheName)
        .replace('__GXR_SW_ENTRY__', entryScript)
        .replace('__GXR_SW_CALIB__', DEFAULT_CALIB_PATH)
        .replace('__GXR_SW_PRECACHE__', JSON.stringify(precache))
        .replace('__GXR_SW_ALLOWED__', JSON.stringify(allowed));

      fs.writeFileSync(path.join(outDir, 'sw.js'), source, 'utf8');
    },
  };
}

const BOOT_CHUNK_RE =
  /(?:^|[\\/])(?:mobile[\\/]src[\\/](?:app[\\/](?:boot(?:_lazy|_shots)?|dom|export_pause|start|book|lut_book|lut_gpu|calib_ui|looks|model|queue|layout|shell\.css)|core[\\/](?:api|pool|protocol)|ui[\\/]perf|wasm-pkg[\\/]|lib[\\/](?!lut_table\.ts$|frame_render\.ts$|font_loader\.ts$))|src[\\/]gxr_studio[\\/]assets[\\/]fonts[\\/])/;
const ENGINE_CHUNK_RE = /(?:^|[\\/])frontend[\\/]src[\\/]/;
const ROLLDOWN_RUNTIME_RE = /rolldown[:/]runtime/;

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
        codeSplitting: {
          groups: [
            {
              name: 'boot',
              priority: 30,
              includeDependenciesRecursively: false,
              test: (id) => BOOT_CHUNK_RE.test(id) || ROLLDOWN_RUNTIME_RE.test(id),
            },
            {
              name: 'engine',
              priority: 20,
              test: ENGINE_CHUNK_RE,
            },
          ],
        },
      },
    },
  },
  plugins: [devSamples(), defaultCalibSwitch(), serviceWorkerBuild()],
}));
